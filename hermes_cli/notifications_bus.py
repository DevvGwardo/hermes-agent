"""Dashboard notification bus: a small local inbox plus a GitHub poller (maiavm fork).

Backs ``GET /api/notifications`` and ``POST /api/notifications/read``
(:mod:`hermes_cli.web_routers.notifications`), the contract the Nub Agent app's
bell consumes. Items have the shape ``{id, source, kind, title, url, repo, at, read}``;
``kind`` is ``pr`` / ``issue`` / ``ci`` for GitHub (other subject types pass through
lowercased). CI pass/fail is carried in the title text, as GitHub words it.

Store: ``<HERMES_HOME>/notifications/inbox.json`` (0600, atomic replace), newest
``MAX_ITEMS`` by ``at``. One item per GitHub thread: new activity on a thread replaces
its item under a NEW id (``github:<thread>:<updated_at>``), because the app keeps an
id it has seen as read forever; a fresh id is what makes the new activity show unread.

GitHub source: when ``GITHUB_TOKEN`` / ``GH_TOKEN`` resolves (``.env`` first, then the
process env, re-read every cycle so adding or rotating the token needs no restart), a
daemon thread polls ``GET https://api.github.com/notifications`` honouring
``X-Poll-Interval`` (never under 60 s) and ``Last-Modified`` (304 costs nothing). A
token GitHub refuses for notifications (fine-grained PATs: 403) disables polling for
that token with one log line until the token changes. No token, no network.
``HERMES_NOTIFICATIONS_GITHUB=0`` turns the poller off. Marking read is local only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

_log = logging.getLogger(__name__)

MAX_ITEMS = 500
MAX_TITLE = 300
GITHUB_API = "https://api.github.com/notifications"
MIN_POLL_S = 60.0
NO_TOKEN_RECHECK_S = 300.0
MAX_BACKOFF_S = 1800.0
FIRST_POLL_DELAY_S = 15.0
# Overlap for ``since`` so a thread updated while a poll was in flight is not skipped
# (items dedup by id, so re-seeing one is harmless).
SINCE_OVERLAP_S = 60.0

_KIND_BY_SUBJECT = {
    "PullRequest": "pr",
    "Issue": "issue",
    "CheckSuite": "ci",
    "WorkflowRun": "ci",
}
_API_SUBJECT_URL = re.compile(
    r"^https://api\.github\.com/repos/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})/(pulls|issues|commits)/([A-Za-z0-9]{1,64})$"
)
_REPO_HTML = re.compile(r"^https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_STORE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _inbox_path() -> Path:
    return _home() / "notifications" / "inbox.json"


def _parse_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sort_key(item: Mapping[str, Any]) -> datetime:
    return _parse_at(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc)


def _load() -> List[Dict[str, Any]]:
    try:
        data = json.loads(_inbox_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("title"), str)]


def _save(items: List[Dict[str, Any]]) -> None:
    from utils import atomic_json_write

    path = _inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    atomic_json_write(path, {"version": 1, "items": items}, mode=0o600)


def _capped(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=_sort_key, reverse=True)[:MAX_ITEMS]


def add_notifications(new_items: Iterable[Dict[str, Any]]) -> int:
    """Upsert items; returns how many were new. An item with a ``thread`` key replaces any
    older item of the same thread (the newest activity wins). Read state survives only for
    the same id."""
    incoming = [i for i in new_items if isinstance(i.get("id"), str) and isinstance(i.get("title"), str)]
    if not incoming:
        return 0
    with _STORE_LOCK:
        items = _load()
        by_id = {i["id"]: i for i in items}
        added = 0
        for item in incoming:
            prev = by_id.get(item["id"])
            if prev is not None:
                by_id[item["id"]] = {**item, "read": bool(prev.get("read")) or bool(item.get("read"))}
                continue
            thread = item.get("thread")
            if thread:
                for old_id in [k for k, v in by_id.items() if v.get("thread") == thread]:
                    old = by_id[old_id]
                    if _sort_key(old) > _sort_key(item):
                        break
                    del by_id[old_id]
                else:
                    by_id[item["id"]] = item
                    added += 1
                continue
            by_id[item["id"]] = item
            added += 1
        _save(_capped(by_id.values()))
        return added


def list_notifications(since: Optional[datetime] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Newest first; ``since`` keeps items strictly newer than it."""
    with _STORE_LOCK:
        items = _load()
    out = []
    for item in sorted(items, key=_sort_key, reverse=True):
        if since is not None and _sort_key(item) <= since:
            continue
        out.append(public_item(item))
        if len(out) >= limit:
            break
    return out


def mark_read(ids: Iterable[str]) -> int:
    wanted = {i for i in ids if isinstance(i, str)}
    if not wanted:
        return 0
    with _STORE_LOCK:
        items = _load()
        changed = 0
        for item in items:
            if item["id"] in wanted and not item.get("read"):
                item["read"] = True
                changed += 1
        if changed:
            _save(items)
        return changed


def public_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": item["id"], "title": item["title"], "read": bool(item.get("read"))}
    for key in ("source", "kind", "url", "repo", "at"):
        value = item.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# GitHub mapping
# ---------------------------------------------------------------------------

def _clean_title(value: Any) -> str:
    text = _CONTROL.sub(" ", value if isinstance(value, str) else "").strip()
    return text[:MAX_TITLE] or "GitHub notification"


def github_html_url(subject_url: Any, repo_html: Optional[str], kind: str) -> Optional[str]:
    """Browser URL for a notification subject. Only github.com URLs are ever returned."""
    if isinstance(subject_url, str):
        m = _API_SUBJECT_URL.match(subject_url)
        if m:
            owner, repo, section, ref = m.groups()
            path = {"pulls": "pull", "issues": "issues", "commits": "commit"}[section]
            return f"https://github.com/{owner}/{repo}/{path}/{ref}"
    if repo_html and _REPO_HTML.match(repo_html):
        return f"{repo_html}/actions" if kind == "ci" else repo_html
    return None


def map_github_notification(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """One GitHub notification thread → one bus item (None for a malformed thread)."""
    thread = raw.get("id")
    subject = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    repository = raw.get("repository") if isinstance(raw.get("repository"), dict) else {}
    updated = _parse_at(raw.get("updated_at"))
    if not isinstance(thread, (str, int)) or not str(thread).isalnum() or updated is None:
        return None
    stype = subject.get("type") if isinstance(subject.get("type"), str) else ""
    kind = _KIND_BY_SUBJECT.get(stype) or re.sub(r"[^a-z0-9_-]", "", stype.lower()) or "other"
    at = _iso(updated)
    repo_name = repository.get("full_name") if isinstance(repository.get("full_name"), str) else None
    repo_html = repository.get("html_url") if isinstance(repository.get("html_url"), str) else None
    item: Dict[str, Any] = {
        "id": f"github:{thread}:{at}",
        "thread": f"github:{thread}",
        "source": "github",
        "kind": kind,
        "title": _clean_title(subject.get("title")),
        "at": at,
        "read": raw.get("unread") is False,
    }
    if repo_name and len(repo_name) <= 140:
        item["repo"] = repo_name
    url = github_html_url(subject.get("url"), repo_html, kind)
    if url:
        item["url"] = url
    return item


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

@dataclass
class HttpResult:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: Any = None


def _urllib_get(url: str, headers: Mapping[str, str], timeout: float = 20.0) -> HttpResult:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            raw = resp.read(4 * 1024 * 1024)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return HttpResult(resp.status, hdrs, json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, None)


def _resolve_token() -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv

        getter: Callable[[str], Optional[str]] = get_env_value_prefer_dotenv
    except Exception:  # noqa: BLE001
        getter = os.environ.get
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        try:
            value = (getter(name) or "").strip()
        except Exception:  # noqa: BLE001
            value = ""
        if value:
            return value
    return None


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


class GitHubPoller:
    """One poll per :meth:`poll_once`; :meth:`run` loops on a stop event."""

    def __init__(
        self,
        *,
        http_get: Callable[[str, Mapping[str, str]], HttpResult] = _urllib_get,
        token_resolver: Callable[[], Optional[str]] = _resolve_token,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http_get = http_get
        self._token_resolver = token_resolver
        self._clock = clock
        self._token_fp: Optional[str] = None
        self._disabled_fp: Optional[str] = None
        self._last_modified: Optional[str] = None
        self._since: Optional[datetime] = None
        self._interval = MIN_POLL_S
        self._failures = 0
        self._logged_auth_fail: Optional[str] = None

    def _backoff(self) -> float:
        self._failures += 1
        return min(MIN_POLL_S * (2 ** min(self._failures, 6)), MAX_BACKOFF_S)

    def poll_once(self) -> float:
        """Poll if there is a usable token; returns seconds until the next attempt."""
        token = self._token_resolver()
        if not token:
            self._token_fp = None
            return NO_TOKEN_RECHECK_S
        fp = _fingerprint(token)
        if fp != self._token_fp:  # new or rotated token: fresh state
            self._token_fp = fp
            self._last_modified = None
            self._since = None
            self._failures = 0
            self._interval = MIN_POLL_S
        if fp == self._disabled_fp:
            return NO_TOKEN_RECHECK_S

        started = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        params = {"all": "false", "participating": "false", "per_page": "50"}
        if self._since is not None:
            params["since"] = _iso(self._since - timedelta(seconds=SINCE_OVERLAP_S))
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-agent-notifications",
        }
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        try:
            res = self._http_get(f"{GITHUB_API}?{urllib.parse.urlencode(params)}", headers)
        except Exception as exc:  # noqa: BLE001 (network, TLS, bad JSON)
            _log.debug("GitHub notifications poll failed: %s", type(exc).__name__)
            return self._backoff()

        poll_hint = res.headers.get("x-poll-interval")
        try:
            self._interval = max(MIN_POLL_S, float(poll_hint)) if poll_hint else self._interval
        except ValueError:
            pass

        if res.status == 304:
            self._failures = 0
            return self._interval
        if res.status == 200 and isinstance(res.body, list):
            self._failures = 0
            self._logged_auth_fail = None
            self._last_modified = res.headers.get("last-modified") or self._last_modified
            self._since = started
            items = [i for i in (map_github_notification(r) for r in res.body if isinstance(r, dict)) if i]
            if items:
                add_notifications(items)
            return self._interval
        if res.status == 403 and res.headers.get("x-ratelimit-remaining") == "0":
            try:
                wait = float(res.headers.get("x-ratelimit-reset", "0")) - self._clock()
            except ValueError:
                wait = 0.0
            self._failures += 1
            return min(max(wait, MIN_POLL_S), 3600.0)
        if res.status == 403:
            # Fine-grained PATs and GitHub App tokens can't read notifications. Retrying won't help.
            self._disabled_fp = fp
            _log.info("GitHub notifications: the configured token can't read notifications (403); polling off until the token changes")
            return NO_TOKEN_RECHECK_S
        if res.status == 401 and self._logged_auth_fail != fp:
            self._logged_auth_fail = fp
            _log.info("GitHub notifications: the configured token was rejected (401); retrying with backoff")
        return self._backoff()

    def run(self, stop: threading.Event, first_delay: float = FIRST_POLL_DELAY_S) -> None:
        if stop.wait(first_delay):
            return
        while not stop.is_set():
            try:
                delay = self.poll_once()
            except Exception:  # noqa: BLE001 (never let the thread die)
                _log.debug("GitHub notifications poller error", exc_info=True)
                delay = self._backoff()
            if stop.wait(delay):
                return


def github_poller_enabled() -> bool:
    return os.environ.get("HERMES_NOTIFICATIONS_GITHUB", "1").strip().lower() not in {"0", "false", "no", "off"}


def start_github_poller() -> Optional[threading.Event]:
    """Start the poller thread (dashboard lifespan); returns its stop event, or None when off."""
    if not github_poller_enabled():
        return None
    stop = threading.Event()
    threading.Thread(target=GitHubPoller().run, args=(stop,), daemon=True, name="notifications-github").start()
    return stop
