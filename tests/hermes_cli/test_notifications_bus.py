"""Notification bus behind ``/api/notifications`` (maiavm fork, maiavm-desktop#17).

Covers the GitHub mapping, the store (dedup, one item per thread, cap, 0600), the routes
(auth, envelope, since, read) and the poller (no token = no network, 304, poll interval,
403 disable, rate limit, backoff, token rotation).
"""

import os
import stat
import threading
from datetime import datetime, timezone

import pytest

from hermes_cli import notifications_bus as bus
from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


def _gh(thread="1001", stype="PullRequest", title="Add the bell", updated="2026-10-07T12:00:00Z",
        url="https://api.github.com/repos/Maia-VM/hermes-deploy/pulls/236", unread=True):
    return {
        "id": thread,
        "unread": unread,
        "updated_at": updated,
        "subject": {"title": title, "url": url, "type": stype},
        "repository": {"full_name": "Maia-VM/hermes-deploy", "html_url": "https://github.com/Maia-VM/hermes-deploy"},
    }


# -- mapping -------------------------------------------------------------------

def test_maps_pull_request_issue_and_ci():
    pr = bus.map_github_notification(_gh())
    assert pr == {
        "id": "github:1001:2026-10-07T12:00:00Z",
        "thread": "github:1001",
        "source": "github",
        "kind": "pr",
        "title": "Add the bell",
        "at": "2026-10-07T12:00:00Z",
        "read": False,
        "repo": "Maia-VM/hermes-deploy",
        "url": "https://github.com/Maia-VM/hermes-deploy/pull/236",
    }
    issue = bus.map_github_notification(_gh(stype="Issue", url="https://api.github.com/repos/Maia-VM/hermes-deploy/issues/240"))
    assert (issue["kind"], issue["url"]) == ("issue", "https://github.com/Maia-VM/hermes-deploy/issues/240")
    # CheckSuite has no subject url; GitHub's own wording carries pass/fail.
    ci = bus.map_github_notification(_gh(stype="CheckSuite", title="CI workflow run failed for main branch", url=None))
    assert ci["kind"] == "ci"
    assert ci["title"] == "CI workflow run failed for main branch"
    assert ci["url"] == "https://github.com/Maia-VM/hermes-deploy/actions"
    assert bus.map_github_notification(_gh(stype="WorkflowRun", url=None))["kind"] == "ci"


def test_mapping_passes_other_types_through_and_never_emits_a_foreign_url():
    rel = bus.map_github_notification(_gh(stype="Release", url="https://api.github.com/repos/o/r/releases/9"))
    assert rel["kind"] == "release"
    assert rel["url"] == "https://github.com/Maia-VM/hermes-deploy"
    evil = _gh(url="https://evil.example/repos/o/r/pulls/1")
    evil["repository"]["html_url"] = "javascript:alert(1)"
    assert "url" not in bus.map_github_notification(evil)
    assert bus.map_github_notification(_gh(title="a\x00b\nc"))["title"] == "a b c"
    assert bus.map_github_notification({"id": "x", "updated_at": "nope"}) is None
    assert bus.map_github_notification(_gh(thread="../1")) is None


# -- store ---------------------------------------------------------------------

def test_store_dedups_by_id_and_keeps_read_state():
    item = bus.map_github_notification(_gh())
    assert bus.add_notifications([item]) == 1
    assert bus.mark_read([item["id"]]) == 1
    assert bus.add_notifications([item]) == 0
    (only,) = bus.list_notifications()
    assert only["read"] is True
    assert "thread" not in only


def test_new_activity_on_a_thread_replaces_it_under_a_new_unread_id():
    first = bus.map_github_notification(_gh(updated="2026-10-07T12:00:00Z"))
    bus.add_notifications([first])
    bus.mark_read([first["id"]])
    later = bus.map_github_notification(_gh(updated="2026-10-07T13:00:00Z"))
    assert bus.add_notifications([later]) == 1
    items = bus.list_notifications()
    assert [i["id"] for i in items] == [later["id"]]
    assert items[0]["read"] is False
    # An older copy of the same thread never replaces the newer one.
    assert bus.add_notifications([first]) == 0
    assert [i["id"] for i in bus.list_notifications()] == [later["id"]]


def test_store_is_capped_newest_first_and_private():
    items = [
        bus.map_github_notification(_gh(thread=str(n), updated=f"2026-10-0{1 + n // 1000}T00:00:{n % 60:02d}Z"))
        for n in range(bus.MAX_ITEMS + 20)
    ]
    bus.add_notifications(items)
    listed = bus.list_notifications(limit=10_000)
    assert len(listed) == bus.MAX_ITEMS
    assert listed == sorted(listed, key=lambda i: i["at"], reverse=True)
    path = bus._inbox_path()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_store_reads_as_empty():
    path = bus._inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert bus.list_notifications() == []


# -- routes --------------------------------------------------------------------

@pytest.fixture
def client():
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    try:
        yield test_client
    finally:
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


def _auth(c):
    c.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return c


def test_routes_require_the_dashboard_session(client):
    assert client.get("/api/notifications").status_code == 401
    assert client.post("/api/notifications/read", json={"ids": ["x"]}).status_code == 401


def test_get_returns_the_envelope_the_app_parses(client):
    _auth(client)
    assert client.get("/api/notifications").json() == {"notifications": []}
    bus.add_notifications([
        bus.map_github_notification(_gh(thread="1", updated="2026-10-07T10:00:00Z")),
        bus.map_github_notification(_gh(thread="2", updated="2026-10-07T11:00:00Z")),
    ])
    body = client.get("/api/notifications").json()
    assert [i["id"] for i in body["notifications"]] == ["github:2:2026-10-07T11:00:00Z", "github:1:2026-10-07T10:00:00Z"]
    assert set(body["notifications"][0]) == {"id", "title", "read", "source", "kind", "url", "repo", "at"}
    newer = client.get("/api/notifications", params={"since": "2026-10-07T10:30:00Z"}).json()
    assert [i["id"] for i in newer["notifications"]] == ["github:2:2026-10-07T11:00:00Z"]
    assert client.get("/api/notifications", params={"since": "yesterday"}).status_code == 400


def test_post_read_marks_local_items(client):
    _auth(client)
    item = bus.map_github_notification(_gh())
    bus.add_notifications([item])
    res = client.post("/api/notifications/read", json={"ids": [item["id"], "unknown"]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "updated": 1}
    assert client.get("/api/notifications").json()["notifications"][0]["read"] is True
    assert client.post("/api/notifications/read", json={"ids": "nope"}).status_code == 422


# -- poller --------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        res = self.responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def _poller(http, token="ghp_test", now=1_760_000_000.0):
    tokens = {"value": token}
    poller = bus.GitHubPoller(http_get=http, token_resolver=lambda: tokens["value"], clock=lambda: now)
    return poller, tokens


def test_no_token_means_no_network():
    http = FakeGitHub()
    poller, _ = _poller(http, token=None)
    assert poller.poll_once() == bus.NO_TOKEN_RECHECK_S
    assert http.calls == []


def test_poll_stores_items_honours_poll_interval_and_last_modified():
    http = FakeGitHub(
        bus.HttpResult(200, {"last-modified": "Tue, 07 Oct 2026 12:00:00 GMT", "x-poll-interval": "120"}, [_gh()]),
        bus.HttpResult(304, {"x-poll-interval": "30"}),
    )
    poller, _ = _poller(http)
    assert poller.poll_once() == 120.0
    assert [i["kind"] for i in bus.list_notifications()] == ["pr"]
    url, headers = http.calls[0]
    assert "participating=false" in url and "all=false" in url and "since=" not in url
    assert headers["Authorization"] == "Bearer ghp_test"
    # 304 costs nothing; the interval never drops under 60s.
    assert poller.poll_once() == bus.MIN_POLL_S
    url2, headers2 = http.calls[1]
    assert headers2["If-Modified-Since"] == "Tue, 07 Oct 2026 12:00:00 GMT"
    assert "since=" in url2


def test_403_disables_the_token_quietly_until_it_changes(caplog):
    http = FakeGitHub(bus.HttpResult(403, {}), bus.HttpResult(200, {}, []))
    poller, tokens = _poller(http, token="github_pat_finegrained")
    with caplog.at_level("INFO"):
        assert poller.poll_once() == bus.NO_TOKEN_RECHECK_S
        assert poller.poll_once() == bus.NO_TOKEN_RECHECK_S
    assert len(http.calls) == 1
    assert sum("can't read notifications" in r.message for r in caplog.records) == 1
    assert all("github_pat_finegrained" not in r.message for r in caplog.records)
    tokens["value"] = "ghp_classic"
    assert poller.poll_once() == bus.MIN_POLL_S
    assert len(http.calls) == 2


def test_rate_limit_waits_for_reset_and_errors_back_off():
    now = 1_760_000_000.0
    http = FakeGitHub(
        bus.HttpResult(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now + 600))}),
        bus.HttpResult(502, {}),
        bus.HttpResult(429, {}),
        OSError("network down"),
        bus.HttpResult(401, {}),
        bus.HttpResult(200, {}, []),
    )
    poller, _ = _poller(http, now=now)
    assert poller.poll_once() == 600.0
    delays = [poller.poll_once() for _ in range(4)]
    assert delays == sorted(delays) and all(d > bus.MIN_POLL_S for d in delays)
    assert max(delays) <= bus.MAX_BACKOFF_S
    assert poller.poll_once() == bus.MIN_POLL_S  # success resets the backoff
    assert poller._failures == 0


def test_run_stops_promptly_and_env_switch_turns_it_off(monkeypatch):
    http = FakeGitHub()
    poller, _ = _poller(http)
    stop = threading.Event()
    stop.set()
    poller.run(stop, first_delay=0.0)
    assert http.calls == []
    monkeypatch.setenv("HERMES_NOTIFICATIONS_GITHUB", "0")
    assert bus.start_github_poller() is None


def test_resolve_token_prefers_github_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert bus._resolve_token() is None
    monkeypatch.setenv("GH_TOKEN", "gh-fallback")
    assert bus._resolve_token() == "gh-fallback"
    monkeypatch.setenv("GITHUB_TOKEN", "gh-primary")
    assert bus._resolve_token() == "gh-primary"


def test_parse_at_accepts_z_and_offsets():
    assert bus._parse_at("2026-10-07T12:00:00Z") == datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    assert bus._parse_at("2026-10-07T14:00:00+02:00") == datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    assert bus._parse_at("") is None
