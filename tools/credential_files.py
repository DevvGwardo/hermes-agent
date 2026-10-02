"""File passthrough registry for remote terminal backends (Docker, Modal, SSH).

Sandboxes start with no host files; this module tells them which credential files
(skill ``required_credential_files`` + ``terminal.credential_files`` config), skill
dirs, and host cache dirs to mount or sync in, at creation and before each command.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_dir, get_hermes_home

from agent.skill_utils import EXCLUDED_SKILL_DIRS

try:  # pragma: no cover - exercised via the fail-closed test below
    from agent.file_safety import get_read_block_error
except ImportError:  # noqa: F401 - sentinel consumed in register_credential_file
    get_read_block_error = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Session-scoped registry; ContextVar prevents cross-session bleed in the gateway.
_registered_files_var: ContextVar[Dict[str, str]] = ContextVar("_registered_files")

# Cache for config-based file list, one entry per profile home (tests reset it).
_config_files: Dict[str, List[Dict[str, str]]] = {}
# Reused across calls so sanitized skill copies don't accumulate.
_safe_skills_tempdir: Path | None = None


def _get_registered() -> Dict[str, str]:
    val = _registered_files_var.get(None)
    if val is None:
        _registered_files_var.set(val := {})
    return val


def _mount(host_path: Path | str, container_path: str) -> Dict[str, str]:
    return {"host_path": str(host_path), "container_path": container_path}


# Hermes-internal state is never a skill's credential, however it is named in frontmatter or
# ``terminal.credential_files`` (an agent can author a skill via skill_manage): config, the
# license / lease key / tool-sandbox SSH key under ``.hermes/``, SQLite stores, logs, other
# profiles, sessions, memories, and every store Hermes or a bundled platform/memory plugin writes
# at the HERMES_HOME root (Maia-VM/hermes-deploy#210, #212). Skill-owned tokens such as
# google-workspace's google_token.json / google_client_secret.json stay mountable.
_INTERNAL_STATE_NAMES = frozenset({
    # Agent identity, managed-scope and launcher state.
    "soul.md", "profile.yaml", "relay-plugins.toml", "install_id", "serve.token", ".hermes_history",
    ".skills_prompt_snapshot.json", "context_length_cache.yaml",
    # Auth, pairing and approval stores outside the master-store read guard.
    "credentials", ".credentials.json", "shell-hooks-allowlist.json", "exec-approvals.json",
    "google_oauth_pending.json", "google_chat_user_token.json", "google_chat_user_client_secret.json",
    "google_chat_user_oauth_pending.json", "google_chat_bot_id.json",
    "feishu_comment_pairing.json", "feishu_comment_rules.json",
    # Platform adapter and memory-provider state.
    "honcho.json", "mem0.json", "slack_tokens.json", "slack-manifest.json",
    "discord_command_sync_state.json", "discord_threads.json", "feishu_seen_message_ids.json",
    "sticker_cache.json", "channel_directory.json", "channel_aliases.json",
    # Gateway, process and update bookkeeping.
    "gateway_state.json", "gateway_voice_mode.json", "gateway.pid", "gateway.sock", "gateway-starts.log",
    "processes.json", "spawn-ledger.json", "interrupt_debug.log", ".restart_notify.json",
    ".restart_pending.json", ".restart_last_processed.json", ".update_pending.json",
    ".update_pending.claimed.json", ".update_prompt.json", ".update_output.txt", ".update_response",
    ".update_exit_code", "a2a_audit.jsonl",
    # Sandbox snapshot registries (ids that let a sandbox restore another session's filesystem).
    "modal_snapshots.json", "vercel_sandbox_snapshots.json", "singularity_snapshots.json",
})
# Backups and rotations of master stores (``config.yaml.bak-…``, ``.env.bak``, ``auth.json.1``).
_INTERNAL_STATE_PREFIXES = ("config.yaml", "config.yml", ".env", "auth.json", "auth.lock")
# SQLite/DuckDB stores and their -wal/-shm/-journal sidecars, backups and locks; lock, pid and
# socket files; per-platform token and pairing-approval stores.
_INTERNAL_STATE_PATTERN = re.compile(
    r"\.(db|sqlite3?|duckdb)([.-].*)?$|\.(lock|pid|sock)$|_tokens\.json$|-approved\.json$")
_INTERNAL_STATE_DIRS = frozenset({
    ".hermes", "logs", "profiles", "sessions", "memories", "memory", "pairing", "platforms", "cron",
    "secrets", "state", "state-snapshots", "checkpoints", "session-exports", "terminal-sessions",
    "spawn-trees", "a2a_conversations", "google_chat_user_tokens", "auth", "backups", "locks",
    "kanban", "whatsapp", "plugin-data", "gateway", "runtime", "hermes-agent",
})
_MANAGED_CONFIG_DIR = Path("/etc/hermes")
# Refusals are logged once per path; readiness probes re-resolve every skill's entries.
_warned_internal: set[str] = set()


def _internal_state_reason(rel: str, resolved: Path, hermes_home: Path) -> Optional[str]:
    """Why *rel* (resolved to *resolved*, symlinks followed) is Hermes-internal state, else None.

    Both the declared path and its resolved target are checked, so neither a symlink named
    ``token.json`` pointing at ``state.db`` nor a declared ``logs/../config.yaml`` gets through.
    """
    declared = Path(posixpath.normpath(rel.replace(os.sep, "/")))
    try:
        target_parts = resolved.relative_to(hermes_home.resolve()).parts
    except ValueError:
        target_parts = ()
    for name, parts in ((declared.name, declared.parts), (resolved.name, target_parts)):
        name = name.lower()
        if (name in _INTERNAL_STATE_NAMES or name.startswith(_INTERNAL_STATE_PREFIXES)
                or _INTERNAL_STATE_PATTERN.search(name)):
            return "a Hermes-internal state file"
        if parts and parts[0].lower() in _INTERNAL_STATE_DIRS:
            return f"inside Hermes-internal {parts[0]}/"
    if resolved == _MANAGED_CONFIG_DIR or _MANAGED_CONFIG_DIR in resolved.parents:
        return "managed Hermes config under /etc/hermes"
    return None


def _contained_host_path(rel: str, hermes_home: Path, abs_msg: str, traversal_msg: str) -> Optional[Path]:
    """Resolve *rel* under HERMES_HOME, refusing absolute paths, escapes and Hermes-internal state."""
    if os.path.isabs(rel):
        logger.warning(abs_msg, rel)
        return None
    host_path = hermes_home / rel
    from tools.path_security import validate_within_dir  # resolves symlinks and ``..`` before checking

    if containment_error := validate_within_dir(host_path, hermes_home):
        logger.warning(traversal_msg, rel, containment_error)
        return None
    resolved = host_path.resolve()
    if reason := _internal_state_reason(rel, resolved, hermes_home):
        if rel not in _warned_internal:
            _warned_internal.add(rel)
            logger.warning("credential_files: refused %r — it is %s, not a skill credential; "
                           "it is never synced into a sandbox", rel, reason)
        return None
    return resolved


def register_credential_file(relative_path: str, container_base: str = "/root/.hermes") -> bool:
    """Register a HERMES_HOME-relative credential file for mounting; True if it exists and was registered.

    Rejects absolute paths and traversal out of HERMES_HOME. Containment alone is not
    enough: HERMES_HOME holds the MASTER stores (``.env``, ``auth.json``, ``mcp-tokens/``),
    which are refused via the canonical read deny-list so the mount surface cannot hand a
    skill what the read surface denies. Fails CLOSED (logged) if the guard is unavailable or raises.
    """
    resolved = resolve_credential_file(relative_path)
    if resolved is None:
        return False

    container_path = f"{container_base.rstrip('/')}/{relative_path}"
    _get_registered()[container_path] = str(resolved)
    logger.debug("credential_files: registered %s -> %s", resolved, container_path)
    return True


def resolve_credential_file(relative_path: str) -> Optional[Path]:
    """Resolved host path of a mountable HERMES_HOME-relative credential file, else None.

    Side-effect free: the same checks ``register_credential_file`` applies (containment,
    existence, master-store deny-list), without registering anything — readiness probes
    (the dashboard's skill list) use it to report missing files.
    """
    resolved = _contained_host_path(
        relative_path, get_hermes_home(),
        "credential_files: rejected absolute path %r (must be relative to HERMES_HOME)",
        "credential_files: rejected path traversal %r (%s)")
    if resolved is None:
        return None
    if not resolved.is_file():
        logger.debug("credential_files: skipping %s (not found)", resolved)
        return None
    # Master credential stores are never mountable, even though they sit inside HERMES_HOME and therefore
    # pass the containment check above. Fails CLOSED: if the canonical guard can't be consulted we refuse
    # the mount rather than risk bind-mounting auth.json into a sandbox. The import lives at module top (no
    # circular-import concern — file_safety is stdlib-only); the sentinel + logger.exception keep guard
    # failures debuggable instead of silently swallowed (#67665).
    if get_read_block_error is None:
        logger.error("credential_files: refusing %r — agent.file_safety could not be "
                     "imported, so the master-store deny-list cannot be consulted", relative_path)
        return None
    try:
        denied = get_read_block_error(str(resolved))
    except Exception:
        logger.exception("credential_files: refusing %r — read guard raised", relative_path)
        return None
    if denied:
        logger.warning("credential_files: refused %r — it is a credential store the agent "
                       "is denied from reading; a skill may mount its own service token, "
                       "not the master key files", relative_path)
        return None
    return resolved


def _credential_entry_paths(entries: list) -> Iterator[str]:
    """Relative paths from skill-frontmatter entries (str or dict with ``path``/``name``)."""
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("path") or entry.get("name") or ""
        elif not isinstance(entry, str):
            continue
        if rel_path := str(entry).strip():
            yield rel_path


def register_credential_files(entries: list, container_base: str = "/root/.hermes") -> List[str]:
    """Register skill-frontmatter entries (str or dict with ``path``); return missing paths."""
    return [rel_path for rel_path in _credential_entry_paths(entries)
            if not register_credential_file(rel_path, container_base)]


def missing_credential_files(entries: list) -> List[str]:
    """Paths from skill-frontmatter entries that ``register_credential_files`` would report
    missing — computed without registering anything (read-only readiness probe)."""
    return [rel_path for rel_path in _credential_entry_paths(entries)
            if resolve_credential_file(rel_path) is None]


def _load_config_files() -> List[Dict[str, str]]:
    """Load ``terminal.credential_files`` from config.yaml (cached per profile home: the
    multiplexed gateway must never mount the launch profile's credential files into a
    secondary profile's sandbox)."""
    from hermes_constants import hermes_home_key
    home_key = hermes_home_key()
    cached = _config_files.get(home_key)
    if cached is not None:
        return cached

    result: List[Dict[str, str]] = []
    try:
        from hermes_cli.config import read_raw_config
        hermes_home = get_hermes_home()
        cred_files = cfg_get(read_raw_config(), "terminal", "credential_files")
        for item in cred_files if isinstance(cred_files, list) else []:
            rel = item.strip() if isinstance(item, str) else ""
            if not rel:
                continue
            resolved_path = _contained_host_path(
                rel, hermes_home,
                "credential_files: rejected absolute config path %r",
                "credential_files: rejected config path traversal %r (%s)")
            if resolved_path is not None and resolved_path.is_file():
                result.append(_mount(resolved_path, f"/root/.hermes/{rel}"))
    except Exception as e:
        logger.warning("Could not read terminal.credential_files from config: %s", e)

    _config_files[home_key] = result
    return result


def get_credential_file_mounts() -> List[Dict[str, str]]:
    """Skill-registered + config credential files as ``host_path``/``container_path`` dicts (re-checked for existence)."""
    mounts = {cp: hp for cp, hp in _get_registered().items() if Path(hp).is_file()}
    for entry in _load_config_files():
        cp, hp = entry["container_path"], entry["host_path"]
        if cp not in mounts and Path(hp).is_file():
            mounts[cp] = hp
    return [_mount(hp, cp) for cp, hp in mounts.items()]


# --- Skills directory mounts ---

def _skill_dir_roots(container_base: str) -> Iterator[Tuple[Path, str]]:
    """Yield ``(host_dir, container_root)`` for every existing skills directory.

    Local skills mount at ``<base>/skills``, external at ``<base>/external_skills/<i>``, trusted
    project-local at ``<base>/project_skills/<i>`` (own namespace so paths stay stable if external_dirs change).
    """
    base = container_base.rstrip("/")
    skills_dir = get_hermes_home() / "skills"
    if skills_dir.is_dir():
        yield skills_dir, f"{base}/skills"
    try:
        from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs
    except ImportError:
        return
    for label, dirs in (("external_skills", get_external_skills_dirs()), ("project_skills", get_project_skills_dirs())):
        yield from ((d, f"{base}/{label}/{idx}") for idx, d in enumerate(dirs) if d.is_dir())


def _walk_skill_tree(root: Path) -> Iterator[Tuple[Path, List[Path]]]:
    """Yield ``(dir, regular_non_symlink_files)`` for every directory a sandbox should receive.

    Prunes ``EXCLUDED_SKILL_DIRS`` *before* descending so bookkeeping/dependency trees (``.hub``,
    ``.archive``, ``.curator_backups``, ``node_modules``, ``.git``, ...) the remote agent never reads
    are never even walked; sync thus agrees with discovery on what is skill content. Deliberately
    not ``is_excluded_skill_path()``: that also prunes ``references/``, ``templates/``, ``assets/``,
    ``scripts/`` — progressive-disclosure files and bundled scripts the sandbox does execute.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_SKILL_DIRS)
        base = Path(dirpath)
        yield base, [f for f in (base / n for n in filenames) if not f.is_symlink() and f.is_file()]


def get_skills_directory_mount(container_base: str = "/root/.hermes") -> list[Dict[str, str]]:
    """Directory mount entries for all skill dirs (local + external + project).

    Bind mounts follow symlinks, so a dir containing any symlink is replaced by a sanitized
    temp copy (regular files only); symlink-free dirs are returned directly, zero overhead.
    """
    return [_mount(_safe_skills_path(d), cp) for d, cp in _skill_dir_roots(container_base)]


def _safe_skills_path(skills_dir: Path) -> str:
    """Return *skills_dir* if symlink-free, else a sanitized temp copy (same exclusions as sync)."""
    global _safe_skills_tempdir

    symlinks = [p for p in skills_dir.rglob("*") if p.is_symlink()]
    if not symlinks:
        return str(skills_dir)
    for link in symlinks:
        logger.warning("credential_files: skipping symlink in skills dir: %s -> %s", link, os.readlink(link))

    import atexit
    import shutil
    import tempfile

    if _safe_skills_tempdir and _safe_skills_tempdir.is_dir():
        shutil.rmtree(_safe_skills_tempdir, ignore_errors=True)
    safe_dir = _safe_skills_tempdir = Path(tempfile.mkdtemp(prefix="hermes-skills-safe-"))

    for base, files in _walk_skill_tree(skills_dir):
        (safe_dir / base.relative_to(skills_dir)).mkdir(parents=True, exist_ok=True)
        for item in files:
            shutil.copy2(str(item), str(safe_dir / item.relative_to(skills_dir)))

    atexit.register(lambda: safe_dir.is_dir() and shutil.rmtree(safe_dir, ignore_errors=True))
    logger.info("credential_files: created symlink-safe skills copy at %s", safe_dir)
    return str(safe_dir)


def iter_skills_files(container_base: str = "/root/.hermes") -> List[Dict[str, str]]:
    """Per-file entries for all skills files (for backends that upload individually)."""
    return [_mount(item, f"{container_root}/{item.relative_to(host_dir).as_posix()}")
            for host_dir, container_root in _skill_dir_roots(container_base)
            for _base, files in _walk_skill_tree(host_dir) for item in files]


# --- Cache directory mounts (documents, images, audio, videos, screenshots) ---

# (new_subpath, old_name) pairs matching hermes_constants.get_hermes_dir().
_CACHE_DIRS: list[tuple[str, str]] = [
    ("cache/documents", "document_cache"),
    ("cache/images", "image_cache"),
    ("cache/audio", "audio_cache"),
    ("cache/videos", "video_cache"),
    ("cache/screenshots", "browser_screenshots"),
    ("cache/web", "web_cache"),
    ("cache/delegation", "delegation_cache"),
    ("cache/spillover", "cache/spillover"),  # oversized tool results; host side is canonical
    # Flat top-level desktop staging dirs (tui_gateway attach RPCs; no legacy alias),
    # mounted so vision/file tools in sandboxes reach uploads and dropped files.
    # Mount it so vision can reach uploads inside sandbox containers (#69575). No legacy alias exists, so
    # both tuple slots are ``images``.
    ("images", "images"),
    # Mount it so the agent's file tools can read dropped binaries (zip/pdf/...) from inside sandbox
    # containers instead of dangling host paths (#76577).
    ("attachments", "attachments"),
    # Desktop stages a large plain-text paste as a `.txt` under this Hermes-managed dir
    # (apps/desktop/electron/composer-paste.ts; `COMPOSER_PASTES_DIRNAME` in
    # agent/context_references.py) and attaches it as `@file:`. Without a mount/sync
    # entry, remote execution backends (ssh/daytona/vercel_sandbox) never received the
    # bytes and `to_agent_visible_cache_path` left the gateway-host path dangling on
    # the remote host (#110174). No legacy alias, so both tuple slots match.
    ("composer-pastes", "composer-pastes"),
]


def _cache_dir_roots(container_base: str, *, create_missing: bool) -> Iterator[Tuple[Path, str]]:
    """Yield ``(host_dir, container_root)`` per cache dir; always maps to the *new* container layout."""
    base = container_base.rstrip("/")
    for new_subpath, old_name in _CACHE_DIRS:
        host_dir = get_hermes_dir(new_subpath, old_name)
        if not host_dir.is_dir():
            if not create_missing:
                continue
            # Docker snapshots this list at container CREATION, so a dir appearing later
            # would dangle for the container's life: create it now (empty bind mount is free).
            # get_hermes_dir already picked new-vs-legacy, so this can't shadow a legacy dir.
            try:
                # Create missing staging dirs instead of skipping them: Docker snapshots this mount list at
                # container CREATION, so a dir that appears later (first desktop attachment, first clipboard
                # image) would dangle for the whole life of a persistent container (#76577). An empty
                # bind-mounted dir costs nothing; a missing mount costs the feature. get_hermes_dir()
                # already resolved new-vs-legacy layout, so creating its answer cannot shadow a populated
                # legacy dir.
                host_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue  # unwritable home (tests, RO mounts) — skip as before
        yield host_dir, f"{base}/{new_subpath}"


# Caches the host writes and later parses or replays as its own (web extract index and pages,
# subagent summaries, spilled tool results). Sandboxes read them, but a remote copy must never
# flow back: it would poison what every later session is served from the host.
_HOST_CANONICAL_CACHE_DIRS = frozenset({"cache/web", "cache/delegation", "cache/spillover"})


def get_skill_host_dirs() -> List[Path]:
    """Host skills directories (local, external, trusted project-local) synced into sandboxes."""
    return [host_dir for host_dir, _ in _skill_dir_roots("/root/.hermes")]


def get_sync_back_cache_dirs() -> List[Path]:
    """Host cache directories a remote sandbox may write new or changed files back into."""
    return [host_dir for new_subpath, old_name in _CACHE_DIRS
            if new_subpath not in _HOST_CANONICAL_CACHE_DIRS
            for host_dir in [get_hermes_dir(new_subpath, old_name)]]


def get_cache_directory_mounts(container_base: str = "/root/.hermes") -> List[Dict[str, str]]:
    """Bind-mount entries for each cache directory (host layout via ``get_hermes_dir``)."""
    return [_mount(h, c) for h, c in _cache_dir_roots(container_base, create_missing=True)]


def _remap_cache_path(path: str, container_base: str, src: str, dst: str, join: Callable[[str, Path], str]) -> Optional[str]:
    """Translate *path* from the *src* side of a cache mount to its *dst* side; None if unmounted."""
    for mount in get_cache_directory_mounts(container_base=container_base):
        if Path(path).is_relative_to(mount[src]):
            return join(mount[dst], Path(path).relative_to(mount[src]))
    return None


def map_cache_path_to_container(host_path: str, container_base: str = "/root/.hermes") -> Optional[str]:
    """POSIX container path for a host path under an auto-mounted cache dir, else None."""
    return _remap_cache_path(host_path, container_base, "host_path", "container_path", lambda root, rel: posixpath.join(root, rel.as_posix()))


def from_agent_visible_cache_path(container_path: str, container_base: str = "/root/.hermes") -> str:
    """Inverse of :func:`to_agent_visible_cache_path`; unchanged unless Docker + cache dir."""
    if _terminal_backend() != "docker":
        return container_path
    mapped = _remap_cache_path(container_path, container_base, "container_path", "host_path", lambda root, rel: str(Path(root) / rel))
    return mapped if mapped is not None else container_path


# Backends whose file-sync lands under the remote home: ``~/.hermes`` is
# expanded by the remote shell, so it resolves regardless of the actual home.
_HOME_RELATIVE_BACKENDS = frozenset({"ssh", "daytona", "vercel_sandbox"})


def _terminal_backend() -> str:
    """Active ``TERMINAL_ENV`` through the per-turn terminal scope (a routed multiplex profile's
    backend, never the launch profile's process env)."""
    from tools.terminal_scope import terminal_env
    return (terminal_env("TERMINAL_ENV") or "local").strip().lower()


def to_agent_visible_cache_path(host_path: str, container_base: str = "/root/.hermes") -> str:
    """Translate a host cache path to where the active backend (TERMINAL_ENV) sees it.

    Mirrors ``_agent_cache_base_for_env`` in tools/image_generation_tool.py: docker/modal mount at
    ``/root/.hermes``; ssh/daytona/vercel_sandbox under ``~/.hermes``; plugin backends declare
    ``cache_path_base`` (None = host paths stay correct); local/singularity/unknown unchanged
    (Apptainer auto-binds the host home, so translation would dangle).

    * docker / modal — bind-mounted (docker) or per-file-synced (modal) at ``/root/.hermes`` (the
    *container_base* default). * ssh / daytona / vercel_sandbox — file-synced under the remote user's home;
    ``~/.hermes`` is shell-expanded by the remote shell, so tool commands resolve it regardless of the
    actual remote home. Previously these backends synced the bytes but still rendered the dangling host path
    (#76577 gap).
    """
    backend = _terminal_backend()
    if backend in _HOME_RELATIVE_BACKENDS:
        container_base = "~/.hermes"
    elif backend not in ("docker", "modal"):
        try:
            from agent.terminal_env_registry import provider_flag
            plugin_base = provider_flag(backend, "cache_path_base", None)
        except Exception:
            plugin_base = None
        if not plugin_base:
            return host_path
        container_base = str(plugin_base)

    mapped = map_cache_path_to_container(host_path, container_base=container_base)
    return mapped if mapped is not None else host_path


def iter_cache_files(container_base: str = "/root/.hermes") -> List[Dict[str, str]]:
    """Per-file cache entries (Modal upload/resync); skips symlinks."""
    return [_mount(item, f"{root}/{item.relative_to(host_dir)}")
            for host_dir, root in _cache_dir_roots(container_base, create_missing=False)
            for item in host_dir.rglob("*") if not item.is_symlink() and item.is_file()]


def clear_credential_files() -> None:
    """Reset the skill-scoped registry (e.g. on session reset)."""
    _get_registered().clear()
