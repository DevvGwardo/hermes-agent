"""
Dashboard-flavored runs API.

Drop-in replacement for the consume-once /v1/runs endpoints, with
reattach + resumption support. Designed for the hermes-deploy
dashboard chat — we control both ends.

Endpoints (registered by the install-runs-overlay patch in api_server.py):

- POST /v1/dashboard/runs
    Start a run. Returns ``{"run_id": "...", "status": "started"}``
    immediately. The run lives on as an asyncio task; it is *not*
    cancelled when the SSE consumer disconnects (the original
    /v1/runs design DOES cancel — that's the bug we're fixing).

    Body: same as POST /v1/runs (input, conversation_history,
    instructions, previous_response_id, session_id).

- GET /v1/dashboard/runs/{run_id}/events
    SSE stream of agent lifecycle events. Each event has a
    monotonic ``id`` and is sent with an ``id:`` SSE frame.
    Send ``Last-Event-Id`` (header) or ``?after=N`` (query) to
    resume from a specific event id. The stream blocks until new
    events arrive (with a 30s keepalive) or the run reaches a
    terminal state.

    A client may reconnect any number of times before the buffer's
    grace TTL expires. After the run completes the stream replays
    the full event log and then closes immediately.

    Assistant text streams as ``message.delta``; ``message.segment``
    (1-based ``index``) marks where a mid-turn assistant-text run ends,
    so the chat can render each part of one reply as its own bubble.
    Clients that don't know the event ignore it.

- GET /v1/dashboard/runs/{run_id}
    JSON snapshot of run state. Polling fallback for clients that
    can't hold an SSE connection.

- DELETE /v1/dashboard/runs/{run_id}
    Explicit cancel. Calls ``agent.interrupt()`` and cancels the
    asyncio task.

- GET  /v1/dashboard/sessions/{session_id}/control
- POST /v1/dashboard/sessions/{session_id}/control
    The goal card's REST surface. ``GET`` returns
    ``{"session_id", "control": {goal, loop, heartbeat, revision,
    updated_at}}``; ``POST`` accepts
    ``{"action": ..., "args": {"text"?, "index"?}}`` with ``goal.set``
    (``args.text`` or ``goal``/``text``, optional ``max_turns``),
    ``goal.pause``, ``goal.resume``, ``goal.clear``, ``goal.status``,
    ``subgoal.add`` (``args.text``), ``subgoal.remove``
    (``args.index``, 1-based) or ``subgoal.clear`` and answers the same
    envelope. ``goal.gate*`` is refused: a gate runs a shell command on
    the VM and stays behind the typed /goal slash command. Every
    mutating action appends ``session.control.update`` to each live run
    of that session.

- GET /v1/dashboard/sessions/{session_id}/subagents
    Runs started with ``parent_session_id=<session_id>`` (a dashboard
    delegation child), newest last. Each entry is the run snapshot.

- POST /v1/dashboard/subagents
- GET  /v1/dashboard/subagents[?session_id=…]
- GET  /v1/dashboard/subagents/{run_id}
- POST /v1/dashboard/subagents/{run_id}/stop
- POST /v1/dashboard/subagents/{run_id}/steer
    The direct child-run surface hermes-deploy's ``/api/chat/subagents``
    proxy calls. ``POST`` takes ``{session_id?, goal, parent_run_id?,
    context?}``, where ``session_id`` is the parent chat, and starts a
    ``source="subagent"`` run in its own ``subagent:<uuid>`` session,
    answering ``run_id`` + ``child_session_id``; ``GET`` lists the tracked
    child runs (optionally for one parent session) or one run's snapshot.
    Stop/steer are the same handlers as ``/runs/{id}/...``.

- POST /v1/dashboard/runs (``source`` / ``parent_session_id``)
    ``source`` may be ``user``, ``dashboard`` or ``subagent``. A
    ``subagent`` run defaults its own ``session_id`` to
    ``subagent:<uuid>`` and never drives the parent's goal loop.

The dashboard run loop also advances an active goal: after each turn it
calls ``GoalManager(session_id).evaluate_after_turn(...)``, streams
``session.control.update`` and, while the goal stays active, runs the
continuation prompt as another turn on the same agent (bounded by
``HERMES_DASHBOARD_GOAL_MAX_CONTINUATIONS``, default 8, and by the goal's
own ``max_turns``).

Authentication: same Bearer token as the rest of /v1/* — uses the
adapter's ``_check_auth`` helper.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — gateway always has aiohttp
    web = None  # type: ignore[assignment]

try:
    from agent.redact import redact_sensitive_text
    from agent.interrupt_compat import request_hard_interrupt
except ImportError:  # pragma: no cover — gateway always has agent.*
    redact_sensitive_text = None  # type: ignore[assignment]
    request_hard_interrupt = None  # type: ignore[assignment]

# Opt-in secret/clarify prompts (the DevvGwardo/hermes-agent fork's
# gateway/platforms/api_server_run_prompts.py). Absent on upstream builds:
# the capability is then never granted, so runs behave exactly as before.
try:
    from gateway.platforms.api_server_run_prompts import (
        CLIENT_CAPABILITIES_FIELD,
        CLIENT_CAPABILITIES_HEADER,
        RunPromptBroker,
        parse_client_capabilities,
    )
except ImportError:  # pragma: no cover — legacy agent builds
    CLIENT_CAPABILITIES_FIELD = "client_capabilities"
    CLIENT_CAPABILITIES_HEADER = "X-Hermes-Client-Capabilities"
    RunPromptBroker = None  # type: ignore[assignment,misc]
    parse_client_capabilities = None  # type: ignore[assignment]

# Sibling module — both files live in gateway/platforms/.
from gateway.platforms.run_buffer import (
    RunBuffer,
    RunBufferRegistry,
    RUN_MAX_CONCURRENT,
)


logger = logging.getLogger(__name__)


# Match the existing SSE keepalive cadence in api_server.
SSE_KEEPALIVE_SECONDS = 30.0


# Mirror api_server._coerce_request_bool: external clients sometimes
# serialize booleans as strings, and Python truthiness makes "false"
# truthy. Treat only explicit bool-ish scalars as booleans; anything
# else falls back to the caller's default.
_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# ── Session-control surface (goals) ───────────────────────────────────
#
# The dashboard app's goal card talks to these two routes instead of the
# gateway's websocket ``session.control`` RPC: the run transport is plain
# HTTP + SSE. Only the goal actions the card renders are accepted; quality
# gates stay behind the typed /goal slash command because a gate runs a
# shell command on the VM.

_CONTROL_ACTIONS = frozenset({
    "goal.set", "goal.pause", "goal.resume", "goal.clear", "goal.status",
    # The same card also hosts the subgoal list; the platform relay forwards
    # ``{action, args: {text | index}}`` verbatim.
    "subgoal.add", "subgoal.remove", "subgoal.clear",
})
# Anything under this prefix answers 400 instead of 404 so a client built
# against the full websocket action set sees an explicit refusal.
_REFUSED_CONTROL_PREFIX = "goal.gate"
_CONTROL_MAX_SESSION_ID = 256
_CONTROL_MAX_GOAL_CHARS = 8_000
_CONTROL_MAX_SUBGOAL_CHARS = 2_000
_CONTROL_MAX_TURNS = 10_000

# Sources a dashboard run may declare. ``subagent`` marks a child run: it is
# recorded with ``parent_session_id`` and never drives its own goal loop.
_DASHBOARD_SOURCES = frozenset({"user", "dashboard", "subagent"})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# Hard cap on goal continuations inside one dashboard run. GoalManager's own
# ``max_turns`` budget pauses the loop first in normal use; this only bounds a
# judge that keeps returning CONTINUE against a broken state row.
_MAX_GOAL_CONTINUATIONS = max(1, _env_int("HERMES_DASHBOARD_GOAL_MAX_CONTINUATIONS", 8))


class _ControlError(Exception):
    """A refused control action, answered as the OpenAI-style error envelope."""

    def __init__(self, message: str, *, code: str = "invalid_request_error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _valid_session_id(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > _CONTROL_MAX_SESSION_ID:
        return None
    return value


def _goal_state_snapshot(state: Any) -> Optional[Dict[str, Any]]:
    """Frontend-safe GoalState view (same shape the Desktop card uses)."""
    if state is None or getattr(state, "status", "cleared") == "cleared":
        return None
    contract = getattr(state, "contract", None)
    snapshot: Dict[str, Any] = {
        "title": state.goal,
        "status": state.status,
        "turns_used": state.turns_used,
        "max_turns": state.max_turns,
        "contract": contract.to_dict() if contract is not None and hasattr(contract, "to_dict") else {},
        "subgoals": list(getattr(state, "subgoals", None) or []),
        "gates": [
            {
                "command": gate.command,
                "timeout_seconds": gate.timeout_seconds,
                "max_retries": gate.max_retries,
                "attempts": gate.attempts,
                "last_exit_code": gate.last_exit_code,
            }
            for gate in (getattr(state, "gates", None) or [])
        ],
    }
    if state.created_at:
        snapshot["created_at"] = state.created_at
    if state.last_turn_at:
        snapshot["updated_at"] = state.last_turn_at
    if state.paused_reason:
        snapshot["paused_reason"] = state.paused_reason
    if state.last_verdict:
        snapshot["last_verdict"] = state.last_verdict
    if state.last_reason:
        snapshot["last_reason"] = state.last_reason
    if state.waiting_until and time.time() < state.waiting_until:
        snapshot["wait_barrier"] = {
            "type": "until", "until_at": state.waiting_until,
            "reason": state.waiting_reason or "",
        }
    elif state.waiting_on_session is not None:
        snapshot["wait_barrier"] = {
            "type": "session", "target": state.waiting_on_session,
            "reason": state.waiting_reason or "",
        }
    elif state.waiting_on_pid is not None:
        snapshot["wait_barrier"] = {
            "type": "pid", "target": state.waiting_on_pid,
            "reason": state.waiting_reason or "",
        }
    return snapshot


def _load_control_states(session_id: str) -> tuple[Any, Any, Any]:
    """Best-effort (goal, loop, heartbeat) rows; a missing manager module is None."""
    from hermes_cli.goals import load_goal

    goal_state = load_goal(session_id)
    loop_state = heartbeat_state = None
    try:
        from hermes_cli.loops import load_loop

        loop_state = load_loop(session_id)
    except Exception:
        pass
    try:
        from hermes_cli.heartbeat import load_heartbeat

        heartbeat_state = load_heartbeat(session_id)
    except Exception:
        pass
    return goal_state, loop_state, heartbeat_state


def _loop_state_snapshot(state: Any, *, deferred_by_goal: bool) -> Optional[Dict[str, Any]]:
    if state is None or getattr(state, "status", "cleared") == "cleared":
        return None
    snapshot = {
        "prompt": state.prompt,
        "status": state.status,
        "mode": state.mode,
        "interval_seconds": state.interval_seconds,
        "current_delay": state.current_delay,
        "times": state.times,
        "until": state.until,
        "max_ticks": state.max_ticks,
        "ticks_fired": state.ticks_fired,
        "created_at": state.created_at,
        "last_fired_at": state.last_fired_at,
        "next_due_at": state.next_due_at,
        "awaiting_response": state.awaiting_response,
        "deferred_by_goal": deferred_by_goal,
    }
    if state.paused_reason:
        snapshot["paused_reason"] = state.paused_reason
    if state.last_stop_reason:
        snapshot["last_stop_reason"] = state.last_stop_reason
    return snapshot


def _heartbeat_state_snapshot(state: Any) -> Optional[Dict[str, Any]]:
    if state is None or getattr(state, "status", "cleared") == "cleared":
        return None
    return {
        "prompt": state.prompt,
        "status": state.status,
        "interval_seconds": state.interval_seconds,
        "created_at": state.created_at,
        "last_fired_at": state.last_fired_at,
        "fire_count": state.fire_count,
    }


def _session_control_snapshot(session_id: str, *, manager: Any = None) -> Dict[str, Any]:
    """Stable goal / loop / heartbeat view for one session.

    ``manager`` is an already-loaded ``GoalManager`` whose in-memory state is
    newer than the DB row (the post-turn judge and a just-applied action), so
    only loop/heartbeat are read back in that case.
    """
    if manager is not None:
        goal_state = manager.state
        _, loop_state, heartbeat_state = _load_control_states(session_id)
    else:
        goal_state, loop_state, heartbeat_state = _load_control_states(session_id)
    goal = _goal_state_snapshot(goal_state)
    deferred = bool(goal and goal["status"] == "active" and not goal.get("wait_barrier"))
    loop = _loop_state_snapshot(
        loop_state,
        deferred_by_goal=bool(deferred and loop_state is not None and loop_state.status == "active"),
    )
    heartbeat = _heartbeat_state_snapshot(heartbeat_state)
    canonical = json.dumps(
        {"goal": goal, "loop": loop, "heartbeat": heartbeat},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return {
        "goal": goal,
        "loop": loop,
        "heartbeat": heartbeat,
        "revision": hashlib.sha256(canonical.encode("utf-8")).hexdigest() if (goal or loop or heartbeat) else "",
        "updated_at": max(
            [v for state, fields in (
                (goal_state, ("created_at", "last_turn_at")),
                (loop_state, ("created_at", "last_fired_at")),
                (heartbeat_state, ("created_at", "last_fired_at")),
            ) if state is not None for field in fields if (v := getattr(state, field, 0))] or [0]
        ),
    }


def _apply_control_action(session_id: str, action: str, body: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """Apply one goal/subgoal action through GoalManager; returns (snapshot, status line)."""
    from hermes_cli.goals import GoalManager

    # The platform relay sends ``{action, args: {text?, index?}}``; accept the
    # flat fields too so a direct caller can post them.
    raw_args = body.get("args")
    args: Dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

    manager = GoalManager(session_id=session_id)
    if action == "goal.set":
        raw_goal = args.get("text", body.get("goal", body.get("text", body.get("input"))))
        if not isinstance(raw_goal, str) or not raw_goal.strip():
            raise _ControlError("'goal' must be a non-empty string", code="invalid_goal")
        if len(raw_goal) > _CONTROL_MAX_GOAL_CHARS:
            raise _ControlError(
                f"'goal' is too long (max {_CONTROL_MAX_GOAL_CHARS} characters)",
                code="invalid_goal",
            )
        raw_max = body.get("max_turns")
        if raw_max is not None:
            if isinstance(raw_max, bool) or not isinstance(raw_max, int) or not 1 <= raw_max <= _CONTROL_MAX_TURNS:
                raise _ControlError(
                    f"'max_turns' must be an integer between 1 and {_CONTROL_MAX_TURNS}",
                    code="invalid_max_turns",
                )
        manager.set(raw_goal.strip(), max_turns=raw_max)
    elif action == "goal.pause":
        if not manager.has_goal() or manager.pause() is None:
            raise _ControlError("No goal to pause", code="goal_not_active", status=409)
    elif action == "goal.resume":
        if not manager.has_goal() or manager.resume() is None:
            raise _ControlError("No goal to resume", code="goal_not_active", status=409)
    elif action == "goal.clear":
        manager.clear()
    elif action == "subgoal.add":
        text = args.get("text", body.get("text"))
        if not isinstance(text, str) or not text.strip() or len(text) > _CONTROL_MAX_SUBGOAL_CHARS:
            raise _ControlError(
                f"'text' must be a non-empty string (max {_CONTROL_MAX_SUBGOAL_CHARS} characters)",
                code="invalid_subgoal",
            )
        try:
            manager.add_subgoal(text.strip())
        except RuntimeError as exc:
            raise _ControlError("No active goal", code="goal_not_active", status=409) from exc
    elif action == "subgoal.remove":
        index = args.get("index", body.get("index"))
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise _ControlError("'index' must be a 1-based integer", code="invalid_subgoal_index")
        try:
            manager.remove_subgoal(index)
        except RuntimeError as exc:
            raise _ControlError("No active goal", code="goal_not_active", status=409) from exc
        except IndexError as exc:
            raise _ControlError(str(exc), code="invalid_subgoal_index") from exc
    elif action == "subgoal.clear":
        try:
            manager.clear_subgoals()
        except RuntimeError as exc:
            raise _ControlError("No active goal", code="goal_not_active", status=409) from exc
    # goal.status is a read: no mutation, and the caller does not publish.
    return _session_control_snapshot(session_id, manager=manager), manager.status_line()


async def _publish_session_control(
    registry: RunBufferRegistry,
    session_id: str,
) -> None:
    """Append ``session.control.update`` to every live run of a session.

    Reads the persisted state under the publish lock, then appends: a snapshot
    taken before a newer one must never be appended after it (an SSE client
    keeps the last frame it saw, so a stale "active" would paint over a
    just-paused goal).
    """
    async with registry.control_publish_lock():
        try:
            control = await asyncio.to_thread(_session_control_snapshot, session_id)
        except Exception:
            logger.debug("[dashboard_runs] control snapshot failed for %s", session_id, exc_info=True)
            return
        revision = control.get("revision") or ""
        for buf in registry.for_session(session_id):
            if buf.terminal or revision == buf._control_revision:
                continue
            await buf.append({
                "event": "session.control.update",
                "run_id": buf.run_id,
                "session_id": session_id,
                "timestamp": time.time(),
                "control": control,
            })
            buf._control_revision = revision


def _schedule_session_control(
    registry: RunBufferRegistry,
    loop: asyncio.AbstractEventLoop,
    session_id: str,
) -> None:
    """``_publish_session_control`` from the executor thread (post-turn judge)."""
    async def _publish() -> None:
        try:
            # The read happens under the publish lock, so a user action that
            # landed meanwhile is what gets streamed.
            await _publish_session_control(registry, session_id)
        except Exception:
            logger.debug("[dashboard_runs] control publish failed for %s", session_id, exc_info=True)

    try:
        loop.call_soon_threadsafe(asyncio.create_task, _publish())
    except RuntimeError:
        # Loop closed mid-run — best-effort, same as append_threadsafe.
        pass


def _openai_error(message: str, code: str = "invalid_request_error", status: int = 400) -> Dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": code,
            "code": code,
        }
    }


def _redact_text(value: str) -> str:
    """Force secret redaction of free-text fields before they hit the stream.

    The agent.redact import is guarded; if it is somehow unavailable, the
    safest fallback is to drop the field rather than leak a raw credential.
    Callers that need lossy-safe text (the approval command) already strip the
    field upstream, so returning "" here is acceptable.
    """
    if redact_sensitive_text is None:
        return ""
    return redact_sensitive_text(value, force=True)


def _approval_event_choices(*, smart_denied: bool, allow_permanent: bool) -> List[str]:
    """Mirror api_server._approval_event_choices for the dashboard stream."""
    if smart_denied:
        return ["once", "deny"]
    return ["once", "session", "always", "deny"] if allow_permanent else ["once", "session", "deny"]


def _verify_signed_url_for_run(run_id: str, request: "web.Request") -> bool:
    """Validate ``?sig=<hex>&exp=<unix>`` HMAC-signed query params.

    Secret is the gateway's bearer token (``HERMES_GATEWAY_TOKEN`` /
    ``API_SERVER_KEY``). Returns True only on a valid, unexpired
    signature scoped to ``run_id``. On any malformed / missing /
    expired / mismatched input the caller falls back to the Bearer
    token check — signed URLs are an additive auth path for browsers
    that can't set custom headers on ``EventSource``.
    """
    sig_hex = request.query.get("sig")
    exp_raw = request.query.get("exp")
    if not sig_hex or not exp_raw:
        return False
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp <= int(time.time()):
        return False
    token = os.environ.get("HERMES_GATEWAY_TOKEN") or os.environ.get("API_SERVER_KEY")
    if not token:
        return False
    msg = f"{run_id}|{exp}".encode("utf-8")
    expected = hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, sig_hex)
    except Exception:
        return False


def _auth_or_signed(adapter: Any, request: "web.Request", run_id: str):
    """Return None when access is allowed, else an aiohttp error Response.

    Signed URL first (cheap; lets the browser open EventSource without
    custom headers) then the existing Bearer-token check.
    """
    if _verify_signed_url_for_run(run_id, request):
        return None
    return adapter._check_auth(request)


def _parse_last_event_id(request: "web.Request") -> int:
    raw = request.headers.get("Last-Event-Id")
    if raw is None:
        raw = request.query.get("after")
    if raw is None:
        return 0
    try:
        n = int(raw)
        return n if n >= 0 else 0
    except ValueError:
        return 0


def _normalize_user_message(raw_input: Any) -> Optional[str]:
    if isinstance(raw_input, str):
        return raw_input or None
    if isinstance(raw_input, list) and raw_input:
        last = raw_input[-1]
        if isinstance(last, dict):
            content = last.get("content")
            if isinstance(content, str):
                return content or None
            if isinstance(content, list):
                # Flatten multi-part text content.
                parts: List[str] = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text = p.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                joined = " ".join(parts).strip()
                return joined or None
        elif isinstance(last, str):
            return last or None
    return None


# Images ride in a separate ``input_images`` field (list of data:image/* or
# https:// URLs) so a platform that sends them still works against an older
# copy of this overlay: that one ignores the unknown key and runs on the text.
# The platform validates every image (MIME allowlist, no SVG, magic bytes);
# this re-checks the scheme only. Without this the dashboard chat flattened
# pasted images to the literal text "[image]" and the agent never saw them.
_MAX_INPUT_IMAGES = 4
_MAX_INPUT_IMAGE_CHARS = 1_400_000


def _input_images_error(raw_images: Any) -> Optional[str]:
    """Why ``input_images`` can't be used as sent, or None.

    Refused out loud instead of skipped: a dropped image used to leave the
    agent answering about a picture it never saw, with nobody told. The
    platform's own chat paths never trip this (they cap images at 4 and the
    whole body at 1.4M chars); a direct caller does.
    """
    if raw_images is None:
        return None
    if not isinstance(raw_images, list):
        return "'input_images' must be an array of image URLs"
    if len(raw_images) > _MAX_INPUT_IMAGES:
        return f"Too many images: at most {_MAX_INPUT_IMAGES} per message"
    for u in raw_images:
        if not isinstance(u, str):
            return "Each 'input_images' entry must be a data:image/* or https:// URL"
        if len(u) > _MAX_INPUT_IMAGE_CHARS:
            return "Image too large: send it smaller (about 1 MB)"
        low = u[:32].lower()
        if not ((low.startswith("data:image/") and not low.startswith("data:image/svg")) or low.startswith("https://")):
            return "Unsupported image: use a data:image/* (not SVG) or https:// URL"
    return None


def _with_input_images(user_message: Optional[str], raw_images: Any) -> Any:
    if not isinstance(raw_images, list) or not raw_images:
        return user_message
    urls: List[str] = []
    for u in raw_images[:_MAX_INPUT_IMAGES]:
        if not isinstance(u, str) or len(u) > _MAX_INPUT_IMAGE_CHARS:
            continue
        low = u[:32].lower()
        if (low.startswith("data:image/") and not low.startswith("data:image/svg")) or low.startswith("https://"):
            urls.append(u)
    if not urls:
        return user_message
    parts: List[Dict[str, Any]] = []
    # "[image]" is the placeholder text the platform sends for an image-only
    # message (an older overlay needs non-empty text); drop it here.
    if user_message and user_message.strip() != "[image]":
        parts.append({"type": "text", "text": user_message})
    parts.extend({"type": "image_url", "image_url": {"url": u}} for u in urls)
    return parts


def _normalize_history(raw_history: Any) -> Optional[List[Dict[str, str]]]:
    if not raw_history:
        return []
    if not isinstance(raw_history, list):
        return None
    out: List[Dict[str, str]] = []
    for entry in raw_history:
        if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
            return None
        content = entry["content"]
        if isinstance(content, list):
            # Flatten OpenAI-style multi-part content to text.
            parts: List[str] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    text = p.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            content = " ".join(parts)
        out.append({"role": str(entry["role"]), "content": str(content)})
    return out


def _profile_scope(adapter: Any) -> Any:
    """The adapter's profile runtime scope for an un-prefixed run, or a no-op.

    Under ``gateway.multiplex_profiles`` every credential read must happen
    inside a profile secret scope, or it raises ``UnscopedSecretError`` (a
    ``key_cmd`` lease re-mint fails the whole turn "after 3 attempts").
    ``_profile_scope(None)`` enters the DEFAULT profile's scope when
    multiplexing is on and is a no-op otherwise (native ``/v1/runs`` parity).
    Builds without it (legacy pin) never multiplex, so nullcontext is right.
    ContextVars do not follow ``run_in_executor``: enter it in each thread.
    """
    scope = getattr(adapter, "_profile_scope", None)
    return scope(None) if callable(scope) else nullcontext()


def _declare_no_async_delivery() -> tuple[Any, Any]:
    """Mark this run's worker thread as unable to take a detached completion.

    A dashboard run ends when its turn ends: nothing reads the session later
    to pick up a background ``delegate_task`` result. Unbound, the session
    context reads as push-capable, so the agent dispatched in the background,
    told the user "I'll post it when it lands", and the finished result was
    dropped after 8 delivery attempts (group room, 2026-09-29). Native
    ``/v1/runs`` binds ``async_delivery=False``; this sets only that var, so
    ``delegate_task`` falls back to running inline and its result is this
    turn's reply. Returns ``(var, token)`` for the reset, or ``(None, None)``
    on a build without the var. Executor threads are pooled: always reset.
    """
    try:
        from gateway import session_context as _sc

        var = getattr(_sc, "_SESSION_ASYNC_DELIVERY", None)
        if var is None:
            return None, None
        return var, var.set(False)
    except Exception:
        return None, None


async def handle_start_run(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return web.json_response(_openai_error("Invalid JSON"), status=400)
    return await _start_run(adapter, registry, body, request=request)


async def _start_run(
    adapter: Any,
    registry: RunBufferRegistry,
    body: Any,
    *,
    request: Optional["web.Request"] = None,
    default_source: Optional[str] = None,
) -> "web.Response":
    """Shared body of ``POST /runs`` and ``POST /subagents`` (already authed)."""
    if not isinstance(body, dict):
        return web.json_response(_openai_error("Invalid JSON"), status=400)

    # Delegation lineage. ``source="subagent"`` marks a child run (the dashboard
    # chat's delegation card starts one); ``parent_session_id`` names the
    # owning chat session. Both are echoed on run.started and the snapshot so
    # the client can group the tree; a subagent run never drives a goal loop of
    # its own (its parent owns the goal).
    source: Optional[str] = body.get("source")
    if source is not None:
        if not isinstance(source, str) or source.strip().lower() not in _DASHBOARD_SOURCES:
            return web.json_response(
                _openai_error(
                    "'source' must be one of: user, dashboard, subagent",
                    code="invalid_source",
                ),
                status=400,
            )
        source = source.strip().lower()
    elif default_source is not None:
        source = default_source
    parent_session_id = body.get("parent_session_id")
    if parent_session_id is not None:
        if not isinstance(parent_session_id, str) or not parent_session_id.strip() or len(parent_session_id) > _CONTROL_MAX_SESSION_ID:
            return web.json_response(
                _openai_error(
                    f"'parent_session_id' must be a non-empty string (max {_CONTROL_MAX_SESSION_ID} characters)",
                    code="invalid_parent_session_id",
                ),
                status=400,
            )
        parent_session_id = parent_session_id.strip()
    parent_run_id = body.get("parent_run_id")
    if parent_run_id is not None:
        if not isinstance(parent_run_id, str) or not parent_run_id.strip() or len(parent_run_id) > _CONTROL_MAX_SESSION_ID:
            return web.json_response(
                _openai_error(
                    f"'parent_run_id' must be a non-empty string (max {_CONTROL_MAX_SESSION_ID} characters)",
                    code="invalid_parent_run_id",
                ),
                status=400,
            )
        parent_run_id = parent_run_id.strip()

    raw_input = body.get("input")
    if raw_input is None:
        # Allow ``messages`` as an alias so dashboards that already
        # build OpenAI Chat Completions payloads can reuse them
        # without translation.
        raw_input = body.get("messages")
    images_err = _input_images_error(body.get("input_images"))
    if images_err:
        return web.json_response(_openai_error(images_err), status=400)
    user_message = _with_input_images(
        _normalize_user_message(raw_input), body.get("input_images")
    )
    if not user_message:
        return web.json_response(_openai_error("Missing or empty user message in 'input' / 'messages'"), status=400)

    explicit_history = _normalize_history(body.get("conversation_history"))
    if explicit_history is None:
        return web.json_response(
            _openai_error("'conversation_history' must be an array of {role, content} objects"),
            status=400,
        )

    # If input was a list and no explicit history, derive history from
    # all but the last message (matches /v1/runs behavior).
    history: List[Dict[str, str]] = list(explicit_history)
    if not history and isinstance(raw_input, list) and len(raw_input) > 1:
        for msg in raw_input[:-1]:
            if isinstance(msg, dict) and msg.get("role") and msg.get("content") is not None:
                content = msg["content"]
                if isinstance(content, list):
                    parts = [
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    content = " ".join(parts)
                history.append({"role": str(msg["role"]), "content": str(content)})

    instructions = body.get("instructions")
    previous_response_id = body.get("previous_response_id")
    stored_session_id: Optional[str] = None
    if not history and previous_response_id:
        stored = adapter._response_store.get(previous_response_id)
        if stored:
            history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            if instructions is None:
                instructions = stored.get("instructions")

    run_id = f"run_{uuid.uuid4().hex}"
    explicit_session_id = body.get("session_id")
    if explicit_session_id is not None and not isinstance(explicit_session_id, str):
        return web.json_response(_openai_error("'session_id' must be a string", code="invalid_session_id"), status=400)
    session_id = (
        (explicit_session_id or stored_session_id)
        or (f"subagent:{uuid.uuid4().hex}" if source == "subagent" else run_id)
    )

    # A child run starts a fresh session per run; it must not share the parent's
    # goal (the parent's goal judge would then see the child's turn as its own).
    goal_continuation = (
        source != "subagent"
        and _coerce_request_bool(body.get("goal_continuation"), default=True)
    )
    # Set by _continue_active_goal once the judge ran; the run wrapper then
    # publishes the final control state BEFORE the terminal event, so an SSE
    # client that closes on run.completed still saw the judge's last step.
    goal_evaluated = False

    try:
        buf = await registry.create(run_id)
    except RuntimeError as exc:
        return web.json_response(_openai_error(str(exc), code="rate_limit_exceeded"), status=429)
    buf.session_id = session_id
    buf.parent_session_id = parent_session_id
    buf.parent_run_id = parent_run_id
    buf.source = source

    loop = asyncio.get_running_loop()

    # Opt-in secret/clarify prompts (fork builds only): the client declares
    # them with X-Hermes-Client-Capabilities (or a client_capabilities body
    # field). The broker's events go on this run's durable stream exactly like
    # approval.request; POST .../secret and .../clarify answer them.
    capabilities: frozenset = frozenset()
    if RunPromptBroker is not None and parse_client_capabilities is not None:
        try:
            capabilities = parse_client_capabilities(
                request.headers.get(CLIENT_CAPABILITIES_HEADER) if request is not None else None,
                body.get(CLIENT_CAPABILITIES_FIELD),
            )
        except Exception:
            capabilities = frozenset()
    broker = None
    if capabilities:

        def _prompt_emit(name: str, fields: Dict[str, Any]) -> None:
            # fields never carry a secret value: secret.request describes the
            # env var; the answer only arrives on the HTTP side.
            buf.append_threadsafe(loop, {
                "event": name,
                "run_id": run_id,
                "timestamp": time.time(),
                **fields,
            })

        broker = RunPromptBroker(capabilities, _prompt_emit, lambda _summary: None)
        buf._prompt_broker = broker

    # Segment boundaries for the dashboard chat: a run of streamed assistant
    # text closed by a tool batch is one bubble. ``streamed_since_segment`` is
    # raised by message.delta and lowered by a marker; ``segment_index`` is
    # 1-based over the markers this run emitted.
    segment_index = 0
    streamed_since_segment = False

    def _on_delta(delta: Optional[str]) -> None:
        nonlocal streamed_since_segment
        # The agent emits None as a CLI box-close signal, not EOS.
        if delta is None:
            return
        if delta:
            streamed_since_segment = True
        buf.append_threadsafe(loop, {
            "event": "message.delta",
            "run_id": run_id,
            "delta": delta,
        })

    def _on_interim(text: str, *, already_streamed: bool = False) -> None:
        """Close the streamed assistant-text run before a tool batch.

        The dashboard joins every ``message.delta`` into one growing bubble, so
        a reply that narrates before each tool call would otherwise read as a
        single wall of text. ``already_streamed=False`` means these tokens never
        left the agent as deltas: send them as one first, so the marker never
        hides them. Only text actually streamed since the last marker opens a
        segment, so two tool rounds with no text between them emit one marker,
        never an empty segment.

        ``_create_agent`` drops this callback when
        ``display.interim_assistant_messages`` is off (api_server parity), and
        the platform still infers the same break from ``tool.started``.
        """
        nonlocal segment_index, streamed_since_segment
        if not isinstance(text, str) or not text.strip():
            return
        if not already_streamed:
            _on_delta(text)
        if not streamed_since_segment:
            return
        segment_index += 1
        streamed_since_segment = False
        buf.append_threadsafe(loop, {
            "event": "message.segment",
            "run_id": run_id,
            "index": segment_index,
            "timestamp": time.time(),
        })

    def _close_segment_before_goal_continuation() -> None:
        """One bubble per turn: a goal continuation is a new assistant reply."""
        nonlocal segment_index, streamed_since_segment
        if not streamed_since_segment:
            return
        segment_index += 1
        streamed_since_segment = False
        buf.append_threadsafe(loop, {
            "event": "message.segment",
            "run_id": run_id,
            "index": segment_index,
            "timestamp": time.time(),
        })

    def _continue_active_goal(agent: Any, result: Any) -> Any:
        """Advance an active goal after a dashboard turn, bounded.

        The gateway's post-turn hook re-enqueues the continuation through the
        adapter FIFO so a simultaneous user message wins. A dashboard run owns
        its session's turn outright, so the continuation is another
        ``run_conversation`` on the same agent: the event stream stays one
        durable run the client can reattach to, and the goal's own
        ``max_turns`` budget pauses the loop before
        ``_MAX_GOAL_CONTINUATIONS`` normally matters.
        """
        nonlocal goal_evaluated
        for _ in range(_MAX_GOAL_CONTINUATIONS):
            if buf._stopping:
                return result
            final_text = ""
            if isinstance(result, dict):
                if result.get("failed"):
                    # A failed turn is no evidence for the judge (the gateway's
                    # post-turn hook skips the goal for the same reason).
                    return result
                final_text = str(result.get("final_response") or result.get("error") or "")
            if not final_text.strip():
                # A failed/empty turn is no evidence for the judge (gateway
                # skips the hook for the same reason).
                return result
            try:
                from hermes_cli.goals import (
                    GoalManager,
                    count_active_delegations,
                    gather_background_processes,
                )

                manager = GoalManager(session_id=session_id)
                if not manager.is_active():
                    return result
                background_processes = None
                active_delegations = 0
                try:
                    background_processes = gather_background_processes(owner_task_id=session_id)
                except Exception:
                    background_processes = None
                try:
                    active_delegations = count_active_delegations(session_id)
                except Exception:
                    active_delegations = 0
                decision = manager.evaluate_after_turn(
                    final_text,
                    user_initiated=True,
                    background_processes=background_processes,
                    active_delegations=active_delegations,
                )
                goal_evaluated = True
            except Exception:
                logger.exception("[dashboard_runs %s] goal evaluation failed", run_id)
                return result
            # The card repaints from the stream, not the reply: the judge has
            # just moved turns_used / the status. The publish re-reads state
            # under its lock, so a concurrent user action wins.
            _schedule_session_control(registry, loop, session_id)
            prompt = decision.get("continuation_prompt") or ""
            if not decision.get("should_continue") or not prompt:
                return result
            _close_segment_before_goal_continuation()
            buf.append_threadsafe(loop, {
                "event": "goal.continuation",
                "run_id": run_id,
                "session_id": session_id,
                "timestamp": time.time(),
                "turn": int(getattr(manager.state, "turns_used", 0) or 0),
                "reason": _redact_text(str(decision.get("reason") or "")),
            })
            result = agent.run_conversation(
                user_message=prompt,
                conversation_history=[],
                task_id="default",
            )
        logger.info(
            "[dashboard_runs %s] goal continuation cap reached (%d)",
            run_id, _MAX_GOAL_CONTINUATIONS,
        )
        return result

    def _on_tool_progress(event_type: str, tool_name: Optional[str] = None,
                           preview: Optional[str] = None, args: Any = None,
                           **kwargs: Any) -> None:
        ts = time.time()
        if event_type == "tool.started":
            buf.append_threadsafe(loop, {
                "event": "tool.started",
                "run_id": run_id,
                "tool": tool_name,
                "preview": preview,
                "timestamp": ts,
            })
        elif event_type == "tool.completed":
            buf.append_threadsafe(loop, {
                "event": "tool.completed",
                "run_id": run_id,
                "tool": tool_name,
                "duration": round(kwargs.get("duration", 0) or 0, 3),
                "error": bool(kwargs.get("is_error", False)),
                "timestamp": ts,
            })
        elif event_type == "reasoning.available":
            buf.append_threadsafe(loop, {
                "event": "reasoning.available",
                "run_id": run_id,
                "text": preview or "",
                "timestamp": ts,
            })
        elif event_type in {"subagent.start", "subagent.complete"}:
            # Mirror api_server._make_run_event_callback: pass the lifecycle
            # boundaries through so clients can observe delegate_task work.
            event: Dict[str, Any] = {
                "event": event_type,
                "run_id": run_id,
                "timestamp": ts,
            }
            if preview is not None:
                event["preview"] = _redact_text(str(preview))
            for key in (
                "goal",
                "task_count",
                "task_index",
                "subagent_id",
                "child_session_id",
                "parent_id",
                "depth",
                "model",
                "tool_count",
                "status",
                "summary",
                "duration_seconds",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "api_calls",
                "cost_usd",
                "files_read",
                "files_written",
                "output_tail",
            ):
                value = kwargs.get(key)
                if value is None:
                    continue
                # Free-text fields can carry child terminal/tool output —
                # force the same secret redaction the native API applies.
                if key in ("goal", "summary", "output_tail") and isinstance(value, str):
                    value = _redact_text(value)
                event[key] = value
            buf.append_threadsafe(loop, event)

    # Approval queues gate host-side tool execution and must be isolated per
    # dashboard run (native /v1/runs parity). The approval session key lives
    # on the buffer so POST .../approval can resolve it without touching the
    # native run registries.
    approval_session_key = run_id
    buf._approval_session_key = approval_session_key

    def _approval_notify(approval_data: Dict[str, Any]) -> None:
        """tools.approval callback — runs in the agent worker thread."""
        event = dict(approval_data or {})
        # Redact credentials from the command before it enters the event
        # stream. Reuse the gateway seam; drop the field entirely if the
        # import is unavailable rather than leak the raw command.
        if "command" in event:
            try:
                from gateway.run import _redact_approval_command

                event["command"] = _redact_approval_command(event.get("command"))
            except Exception:
                logger.exception("[dashboard_runs %s] approval command redaction failed", run_id)
                event.pop("command", None)
        event.update({
            "event": "approval.request",
            "run_id": run_id,
            "timestamp": time.time(),
            "choices": _approval_event_choices(
                smart_denied=bool(event.get("smart_denied")),
                allow_permanent=event.get("allow_permanent") is not False,
            ),
        })
        buf.append_threadsafe(loop, event)
        buf.note_approval_requested()

    async def _run_and_close() -> None:
        try:
            await buf.append({
                "event": "run.started",
                "run_id": run_id,
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "parent_run_id": parent_run_id,
                "source": source,
            })
            # Clarify is off the api_server toolset for plain clients; a
            # declaring client gets it back (native /v1/runs parity).
            wants_clarify = broker is not None and "clarify" in broker.capabilities
            extra: Dict[str, Any] = {"extra_toolsets": ["clarify"]} if wants_clarify else {}
            with _profile_scope(adapter):
                agent = adapter._create_agent(
                    ephemeral_system_prompt=instructions,
                    session_id=session_id,
                    stream_delta_callback=_on_delta,
                    tool_progress_callback=_on_tool_progress,
                    interim_assistant_callback=_on_interim,
                    **extra,
                )
            if wants_clarify:
                agent.clarify_callback = broker.clarify
            buf._agent_ref.append(agent)

            def _run_sync() -> tuple[Dict[str, Any], Dict[str, int]]:
                with _profile_scope(adapter):
                    return _run_sync_scoped()

            def _run_sync_scoped() -> tuple[Dict[str, Any], Dict[str, int]]:
                from tools.approval import (
                    register_gateway_notify,
                    unregister_gateway_notify,
                )

                # The session-key helpers moved to tools.approval_context
                # (the tools.approval aliases are deprecated there); older
                # builds only have the tools.approval names.
                try:
                    from tools.approval_context import (
                        reset_current_session_key,
                        set_current_session_key,
                    )
                except ImportError:
                    from tools.approval import (  # type: ignore[no-redef]
                        reset_current_session_key,
                        set_current_session_key,
                    )

                approval_token = None
                secret_reset = None
                async_var, async_token = _declare_no_async_delivery()
                try:
                    # Bind approval identity via contextvars so concurrent runs
                    # don't share process env state (native /v1/runs parity).
                    approval_token = set_current_session_key(approval_session_key)
                    register_gateway_notify(approval_session_key, _approval_notify)
                    if broker is not None and "secret" in broker.capabilities:
                        # Scoped (contextvar) so it only applies to this run's
                        # worker thread, never to other concurrent runs.
                        from tools.skills_tool import (
                            bind_scoped_secret_capture_callback,
                            reset_scoped_secret_capture_callback,
                        )

                        secret_token = bind_scoped_secret_capture_callback(broker.secret_capture)
                        secret_reset = lambda: reset_scoped_secret_capture_callback(secret_token)  # noqa: E731
                    result = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=history,
                        task_id="default",
                    )
                    if goal_continuation:
                        # The judge is a synchronous aux-LLM call; this is the
                        # executor thread, so it blocks no gateway loop work.
                        result = _continue_active_goal(agent, result)
                    usage = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return result, usage
                finally:
                    try:
                        unregister_gateway_notify(approval_session_key)
                    finally:
                        if secret_reset is not None:
                            try:
                                secret_reset()
                            except Exception:
                                pass
                        if approval_token is not None:
                            try:
                                reset_current_session_key(approval_token)
                            except Exception:
                                pass
                        if async_token is not None:
                            try:
                                async_var.reset(async_token)
                            except Exception:
                                pass

            result, usage = await loop.run_in_executor(None, _run_sync)
            if buf._stopping:
                # /stop won the race — the executor returned an interrupted
                # result. Native treats a stopped run as cancelled, never as
                # completed with whatever the interrupted agent returned.
                await buf.mark_cancelled("Stop requested via API")
                return
            if goal_continuation and goal_evaluated:
                # The judge's last state must be on the stream before the
                # terminal event: an SSE consumer closes at run.completed and
                # would never read an append that lands after it.
                try:
                    await _publish_session_control(registry, session_id)
                except Exception:
                    logger.debug("[dashboard_runs %s] final control publish failed", run_id)
            final = ""
            if isinstance(result, dict):
                final = result.get("final_response") or result.get("error") or ""
            await buf.mark_completed(final, usage)
        except asyncio.CancelledError:
            await buf.mark_cancelled("Run cancelled")
            raise
        except Exception as exc:
            logger.exception("[dashboard_runs] run %s failed", run_id)
            await buf.mark_failed(str(exc))
        finally:
            # If the asyncio wrapper is cancelled (e.g. /stop) while the
            # executor thread is blocked on an approval Event, unregistering
            # releases those waits immediately (native /v1/runs teardown).
            try:
                from tools.approval import unregister_gateway_notify

                unregister_gateway_notify(approval_session_key)
            except Exception:
                pass
            # Release any secret/clarify waiter with an empty answer so a
            # cancelled run's worker thread never blocks on a prompt.
            if broker is not None:
                try:
                    broker.close()
                except Exception:
                    pass

    task = loop.create_task(_run_and_close())
    buf._agent_task = task
    try:
        adapter._background_tasks.add(task)
    except Exception:
        pass
    if hasattr(task, "add_done_callback"):
        task.add_done_callback(adapter._background_tasks.discard)

    return web.json_response({
        "run_id": run_id,
        "status": "started",
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "parent_run_id": parent_run_id,
        "source": source,
    }, status=202)


async def handle_run_events(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.StreamResponse":
    run_id = request.match_info["run_id"]
    auth_err = _auth_or_signed(adapter, request, run_id)
    if auth_err:
        return auth_err

    cursor = _parse_last_event_id(request)

    # Allow a brief grace window if the SSE subscriber raced ahead of
    # the POST handler that created the buffer (very tight in practice
    # because POST returns synchronously, but a misbehaving proxy can
    # reorder).
    buf = registry.get(run_id)
    if buf is None:
        for _ in range(20):
            await asyncio.sleep(0.05)
            buf = registry.get(run_id)
            if buf is not None:
                break
    if buf is None:
        return web.json_response(
            _openai_error(f"Run not found: {run_id}", code="run_not_found"),
            status=404,
        )

    # CORS for StreamResponse must be resolved HERE, not in
    # api_server.py's cors_middleware. Reason: the middleware injects
    # Allow-Origin AFTER ``await handler(request)`` returns, but a
    # StreamResponse flushes its headers to the wire inside
    # ``response.prepare(request)`` — which runs inside this handler.
    # By the time the middleware tries to mutate response.headers, the
    # headers are already on the wire and the update is a no-op.
    # Browsers then reject every direct EventSource hit with
    # "No Access-Control-Allow-Origin header is present", even though
    # the SSE body itself streams successfully.
    #
    # Fix: ask the adapter for the headers that match the request's
    # Origin (same allowlist + same logic as the preflight handler in
    # cors_middleware) and merge them into sse_headers BEFORE prepare().
    # If the origin isn't allowlisted, the middleware would have
    # short-circuited with a 403 in its own ``_origin_allowed`` check;
    # so when we reach this handler we know either (a) no Origin
    # header (curl, server-side fetch) or (b) an allowed Origin. Both
    # are safe — _cors_headers_for_origin returns None for (a) and a
    # valid header dict for (b).
    sse_headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    adapter_for_cors = request.app.get("api_server_adapter")
    if adapter_for_cors is not None:
        cors_headers = adapter_for_cors._cors_headers_for_origin(
            request.headers.get("Origin", "")
        )
        if cors_headers:
            sse_headers.update(cors_headers)
    response = web.StreamResponse(status=200, headers=sse_headers)
    await response.prepare(request)

    try:
        # Send a comment immediately so proxies (Cloudflare, Vercel)
        # commit the response headers and don't buffer.
        await response.write(b": connected\n\n")
        terminal = False
        while not terminal:
            try:
                events, terminal = await buf.read_after(cursor, timeout=SSE_KEEPALIVE_SECONDS)
            except asyncio.CancelledError:
                # Server-side cancel (gateway shutting down). Don't
                # touch the buffer — let it survive for the next
                # consumer.
                return response
            if not events:
                # Keepalive — empty comment frame keeps Cloudflare's
                # 100s idle timer at bay.
                try:
                    await response.write(b": keepalive\n\n")
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    return response
                continue
            for evt in events:
                cursor = evt["id"]
                payload = (
                    f"id: {cursor}\n"
                    f"event: {evt.get('event', 'message')}\n"
                    f"data: {json.dumps(evt)}\n\n"
                )
                try:
                    await response.write(payload.encode())
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    # Client dropped — buffer stays intact for the
                    # next reconnect. Nothing to clean up.
                    return response
        # Terminal — drained everything. Send a comment so well-behaved
        # SSE clients close cleanly.
        try:
            await response.write(b": stream closed\n\n")
        except Exception:
            pass
    finally:
        # Critical: do NOT pop the buffer. Other consumers may be
        # mid-stream and the run may want to be reattached. The
        # registry sweeper drops it after the grace TTL.
        pass

    return response


async def handle_run_snapshot(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    run_id = request.match_info["run_id"]
    auth_err = _auth_or_signed(adapter, request, run_id)
    if auth_err:
        return auth_err

    buf = registry.get(run_id)
    if buf is None:
        return web.json_response(
            _openai_error(f"Run not found: {run_id}", code="run_not_found"),
            status=404,
        )
    return web.json_response(buf.snapshot())


async def handle_run_cancel(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    run_id = request.match_info["run_id"]
    buf = registry.get(run_id)
    if buf is None:
        return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)
    if not buf.terminal:
        await buf.cancel("Run cancelled by client")
    return web.json_response({"run_id": run_id, "status": "cancel-requested"})


async def handle_run_stop(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/runs/{run_id}/stop — hard-interrupt a running agent.

    Resolves the agent through the RunBuffer (not the native /v1/runs
    registries, which the dashboard path never populates).
    """
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    run_id = request.match_info["run_id"]
    buf = registry.get(run_id)
    if buf is None:
        return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

    agent = buf._agent_ref[0] if buf._agent_ref else None
    if agent is not None:
        try:
            if request_hard_interrupt is not None:
                request_hard_interrupt(agent, "Stop requested via API")
            elif hasattr(agent, "interrupt"):
                agent.interrupt("Stop requested via API")
        except Exception:
            logger.exception("[dashboard_runs %s] hard interrupt failed", run_id)

    # Native /stop semantics: the interrupted executor return must not be
    # recorded as run.completed. Flag the buffer so the run wrapper marks
    # the run cancelled instead, and cancel the wrapper task so the buffer
    # reaches a terminal run.cancelled even if the agent thread is wedged
    # on an approval wait (the wrapper's finally-block unregister releases
    # those waits immediately).
    buf._stopping = True
    if buf._agent_task is not None and not buf._agent_task.done():
        buf._agent_task.cancel()

    return web.json_response({"run_id": run_id, "status": "stopping"})


async def handle_run_steer(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/runs/{run_id}/steer — inject mid-run guidance."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    run_id = request.match_info["run_id"]
    buf = registry.get(run_id)
    if buf is None:
        return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

    agent = buf._agent_ref[0] if buf._agent_ref else None
    if agent is None or not hasattr(agent, "steer"):
        return web.json_response(
            _openai_error(
                f"Run is not currently accepting steer input: {run_id}",
                code="run_not_accepting_steer",
            ),
            status=409,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(_openai_error("Invalid JSON"), status=400)

    raw_text = body.get("input") or body.get("message") or body.get("text") or ""
    steer_text = str(raw_text).strip()
    if not steer_text:
        return web.json_response(
            _openai_error(
                "Missing non-empty steer text; expected 'input', 'message', or 'text'.",
                code="invalid_steer_input",
            ),
            status=400,
        )

    try:
        accepted = bool(agent.steer(steer_text))
    except Exception:
        logger.exception("[dashboard_runs %s] steer failed", run_id)
        return web.json_response(_openai_error("steer failed", code="steer_failed"), status=500)
    if not accepted:
        return web.json_response(
            _openai_error(f"Run did not accept steer text: {run_id}", code="steer_not_accepted"),
            status=409,
        )

    # Native parity: surface the steer on the durable run stream.
    buf.append_threadsafe(asyncio.get_running_loop(), {
        "event": "run.steered",
        "run_id": run_id,
        "timestamp": time.time(),
        "accepted": True,
    })

    return web.json_response({"object": "hermes.run.steer", "run_id": run_id, "accepted": True})


async def handle_run_approval(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/runs/{run_id}/approval — resolve a pending approval."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    run_id = request.match_info["run_id"]
    buf = registry.get(run_id)
    if buf is None:
        return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response(_openai_error("Invalid JSON"), status=400)

    raw_choice = str(body.get("choice", "")).strip().lower()
    aliases = {"approve": "once", "approved": "once", "allow": "once"}
    choice = aliases.get(raw_choice, raw_choice)
    allowed = {"once", "session", "always", "deny"}
    if choice not in allowed:
        return web.json_response(
            _openai_error(
                "Invalid approval choice; expected one of: once, session, always, deny",
                code="invalid_approval_choice",
            ),
            status=400,
        )

    approval_session_key = buf._approval_session_key
    if not approval_session_key:
        return web.json_response(
            _openai_error(
                f"Run has no active approval session: {run_id}",
                code="approval_not_active",
            ),
            status=409,
        )

    resolve_all = (
        _coerce_request_bool(body.get("all"), default=False)
        or _coerce_request_bool(body.get("resolve_all"), default=False)
    )
    try:
        from tools.approval import resolve_gateway_approval

        resolved = resolve_gateway_approval(
            approval_session_key,
            choice,
            resolve_all=resolve_all,
        )
    except Exception as exc:
        logger.exception("[dashboard_runs %s] approval resolution failed", run_id)
        return web.json_response(_openai_error(str(exc)), status=500)

    if resolved <= 0:
        return web.json_response(
            _openai_error(
                f"Run has no pending approval: {run_id}",
                code="approval_not_pending",
            ),
            status=409,
        )

    # Native parity: push the resolution onto the durable run stream so
    # reattached SSE consumers see the approval was answered.
    buf.note_approval_resolved()
    buf.append_threadsafe(asyncio.get_running_loop(), {
        "event": "approval.responded",
        "run_id": run_id,
        "timestamp": time.time(),
        "choice": choice,
        "resolved": resolved,
    })

    return web.json_response({
        "object": "hermes.run.approval_response",
        "run_id": run_id,
        "choice": choice,
        "resolved": resolved,
    })


async def _handle_run_prompt(
    adapter: Any,
    registry: RunBufferRegistry,
    request: "web.Request",
    kind: str,
    value_key: str,
) -> "web.Response":
    """Shared body of POST .../secret and .../clarify (native /v1/runs parity).

    The answer value is never logged, echoed, or put on the event stream.
    """
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    run_id = request.match_info["run_id"]
    buf = registry.get(run_id)
    if buf is None:
        return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response(_openai_error("Invalid JSON"), status=400)
    if not isinstance(body, dict):
        return web.json_response(_openai_error("Invalid JSON"), status=400)

    request_id = body.get("request_id")
    raw_value = body.get(value_key, "")
    if kind == "clarify" and isinstance(raw_value, (list, dict)):
        # Multi-select array / whole-batch answers.
        raw_value = json.dumps(raw_value, ensure_ascii=False)
    question_id = body.get("question_id") or ""
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
        return web.json_response(
            _openai_error(f"{kind.capitalize()} request_id is required.", code=f"invalid_{kind}_request"),
            status=400,
        )
    if raw_value is not None and not isinstance(raw_value, str):
        return web.json_response(
            _openai_error(f"'{value_key}' must be a string.", code=f"invalid_{kind}_{value_key}"),
            status=400,
        )
    if not isinstance(question_id, str):
        return web.json_response(
            _openai_error("'question_id' must be a string.", code=f"invalid_{kind}_request"),
            status=400,
        )

    broker = getattr(buf, "_prompt_broker", None)
    if broker is None or kind not in broker.capabilities:
        return web.json_response(
            _openai_error(f"Run has no active {kind} prompts: {run_id}", code=f"{kind}_not_active"),
            status=409,
        )

    error, remaining = broker.resolve(kind, request_id.strip(), raw_value or "", question_id)
    if error == "unknown_question":
        return web.json_response(
            _openai_error(f"Unknown question_id: {question_id}", code="invalid_clarify_question"),
            status=400,
        )
    if error is not None:
        return web.json_response(
            _openai_error(
                f"Run has no pending {kind} request {request_id.strip()}",
                code=f"{kind}_not_pending",
            ),
            status=409,
        )

    result: Dict[str, Any] = {"run_id": run_id, "request_id": request_id.strip()}
    if kind == "secret":
        result["skipped"] = not raw_value
    if remaining is not None:
        result.update(question_id=question_id, remaining=remaining)
    event: Dict[str, Any] = {"event": f"{kind}.responded", "run_id": run_id, "timestamp": time.time()}
    event.update({k: v for k, v in result.items() if k != "run_id"})
    buf.append_threadsafe(asyncio.get_running_loop(), event)
    return web.json_response({"object": f"hermes.run.{kind}_response", **result})


async def handle_run_secret(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/runs/{run_id}/secret — answer a secret.request ({request_id, value}; empty = skip)."""
    return await _handle_run_prompt(adapter, registry, request, "secret", "value")


async def handle_run_clarify(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/runs/{run_id}/clarify — answer a clarify.request ({request_id, answer, question_id?})."""
    return await _handle_run_prompt(adapter, registry, request, "clarify", "answer")


async def handle_start_subagent(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/subagents — start a direct child run.

    Contract used by hermes-deploy's ``/api/chat/subagents`` proxy:
    ``{session_id?, goal, parent_run_id?, context?}`` -> ``run_id`` +
    ``child_session_id``. ``session_id`` names the PARENT chat the child
    belongs to; the child itself always gets a fresh ``subagent:<uuid>``
    session so its transcript never mixes with the parent's. ``goal`` becomes
    the first user turn; ``context`` is appended to it (the agent's system
    prompt is left alone). ``wait`` is accepted for parity but not honoured:
    the call answers as soon as the run is started, and the caller polls the
    run snapshot.
    """
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return web.json_response(_openai_error("Invalid JSON"), status=400)
    if not isinstance(body, dict):
        return web.json_response(_openai_error("Invalid JSON"), status=400)

    raw_goal = body.get("goal", body.get("input"))
    if not isinstance(raw_goal, str) or not raw_goal.strip():
        return web.json_response(_openai_error("'goal' is required", code="invalid_goal"), status=400)
    if len(raw_goal) > _CONTROL_MAX_GOAL_CHARS:
        return web.json_response(
            _openai_error(f"'goal' is too long (max {_CONTROL_MAX_GOAL_CHARS} characters)", code="invalid_goal"),
            status=400,
        )
    goal = raw_goal.strip()

    session_id = _valid_session_id(body.get("session_id") or "dashboard")
    if session_id is None:
        return web.json_response(_openai_error("Invalid session_id", code="invalid_session_id"), status=400)
    parent_session_id = session_id
    # A direct subagent gets its OWN session (the platform stores the returned
    # ``child_session_id`` on the child ChatTurn); the request's session_id is
    # the parent chat it belongs to.
    child_session_id = _valid_session_id(body.get("child_session_id")) or f"subagent:{uuid.uuid4().hex}"

    context = body.get("context")
    if context is not None:
        if not isinstance(context, str) or len(context) > 12_000:
            return web.json_response(
                _openai_error("'context' must be a string (max 12000 characters)", code="invalid_context"),
                status=400,
            )
        if context.strip():
            goal = f"{goal}\n\nContext:\n{context.strip()}"

    run_body: Dict[str, Any] = {
        "input": goal,
        "session_id": child_session_id,
        "source": "subagent",
        "goal_continuation": False,
        "parent_run_id": body.get("parent_run_id"),
        "parent_session_id": parent_session_id,
    }
    if body.get("input_images") is not None:
        run_body["input_images"] = body.get("input_images")

    response = await _start_run(
        adapter,
        registry,
        run_body,
        request=request,
        default_source="subagent",
    )
    if response.status != 202:
        return response
    try:
        payload = json.loads(response.body or b"{}")
    except Exception:
        payload = {}
    payload["object"] = "hermes.subagent"
    payload["child_session_id"] = payload.get("session_id")
    return web.json_response(payload, status=202)


async def handle_list_subagents(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """GET /v1/dashboard/subagents?session_id=… — runs the gateway tracks."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    raw_session = request.query.get("session_id")
    session_id = _valid_session_id(raw_session) if raw_session is not None else None
    if raw_session is not None and session_id is None:
        return web.json_response(_openai_error("Invalid session_id", code="invalid_session_id"), status=400)
    return web.json_response([buf.snapshot() for buf in registry.subagents(session_id)])


async def handle_subagent_snapshot(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """GET /v1/dashboard/subagents/{run_id} — status of one direct child run."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    run_id = request.match_info["run_id"]
    buf = registry.get(run_id)
    if buf is None or buf.source != "subagent":
        return web.json_response(
            _openai_error(f"Subagent run not found: {run_id}", code="subagent_not_found"),
            status=404,
        )
    return web.json_response(buf.snapshot())


async def handle_session_control_get(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """GET /v1/dashboard/sessions/{session_id}/control — read goal/loop state."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    session_id = _valid_session_id(request.match_info.get("session_id"))
    if session_id is None:
        return web.json_response(_openai_error("Invalid session_id", code="invalid_session_id"), status=400)
    try:
        control = await asyncio.to_thread(_session_control_snapshot, session_id)
    except Exception as exc:
        logger.exception("[dashboard_runs] control snapshot failed for %s", session_id)
        return web.json_response(
            _openai_error(f"Session control unavailable: {exc}", code="control_unavailable"),
            status=500,
        )
    return web.json_response({
        "object": "hermes.session.control",
        "session_id": session_id,
        "control": control,
    })


async def handle_session_control_post(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """POST /v1/dashboard/sessions/{session_id}/control — goal set/pause/resume/clear/status."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    session_id = _valid_session_id(request.match_info.get("session_id"))
    if session_id is None:
        return web.json_response(_openai_error("Invalid session_id", code="invalid_session_id"), status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response(_openai_error("Invalid JSON"), status=400)
    if not isinstance(body, dict):
        return web.json_response(_openai_error("Invalid JSON"), status=400)

    raw_action = body.get("action")
    if not isinstance(raw_action, str) or not raw_action.strip():
        return web.json_response(
            _openai_error("'action' is required", code="invalid_control_action"),
            status=400,
        )
    action = raw_action.strip()
    if action.startswith(_REFUSED_CONTROL_PREFIX):
        return web.json_response(
            _openai_error(
                "Goal gate controls are not available over the dashboard API; use the /goal gate command",
                code="unsupported_control_action",
            ),
            status=400,
        )
    if action not in _CONTROL_ACTIONS:
        return web.json_response(
            _openai_error(
                "Unsupported action; expected one of: " + ", ".join(sorted(_CONTROL_ACTIONS)),
                code="invalid_control_action",
            ),
            status=400,
        )

    try:
        control, message = await asyncio.to_thread(_apply_control_action, session_id, action, body)
    except _ControlError as exc:
        return web.json_response(_openai_error(exc.message, code=exc.code), status=exc.status)
    except Exception as exc:
        logger.exception("[dashboard_runs] control action %s failed for %s", action, session_id)
        return web.json_response(
            _openai_error(f"Session control unavailable: {exc}", code="control_unavailable"),
            status=500,
        )

    # Reads never publish (the Desktop card repaints only on a change). The
    # publish re-reads under its lock: a judge that landed a newer snapshot
    # meanwhile is what gets streamed, never this call's older one.
    if action != "goal.status":
        await _publish_session_control(registry, session_id)
    return web.json_response({
        "object": "hermes.session.control",
        "session_id": session_id,
        "action": action,
        "message": message,
        "control": control,
    })


async def handle_session_subagents(adapter: Any, registry: RunBufferRegistry, request: "web.Request") -> "web.Response":
    """GET /v1/dashboard/sessions/{session_id}/subagents — child runs of a session."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    session_id = _valid_session_id(request.match_info.get("session_id"))
    if session_id is None:
        return web.json_response(_openai_error("Invalid session_id", code="invalid_session_id"), status=400)
    return web.json_response({
        "object": "list",
        "session_id": session_id,
        "data": [buf.snapshot() for buf in registry.for_parent(session_id)],
    })


def attach_to_adapter(adapter: Any, app: "web.Application") -> None:
    """Install the dashboard runs API on one api_server adapter/router.

    Called from ``APIServerAdapter.connect()``; keeps the registry on the
    adapter so later repair scripts and tests still find it under the legacy
    ``_dashboard_run_registry`` name. A build lacking run_buffer still serves
    the native ``/v1/runs`` endpoints.
    """
    if getattr(adapter, "_dashboard_run_registry", None) is None:
        try:
            adapter._dashboard_run_registry = RunBufferRegistry()
        except Exception as exc:  # pragma: no cover - import guard
            logger.warning("[api_server] dashboard run registry disabled: %s", exc)
            adapter._dashboard_run_registry = None
    registry = adapter._dashboard_run_registry
    if registry is None:
        return
    try:
        register_routes(app, adapter, registry)
        registry.start_sweeper(asyncio.get_running_loop())
        logger.info("[api_server] /v1/dashboard/runs routes registered")
    except Exception as exc:  # pragma: no cover
        logger.warning("[api_server] failed to register dashboard runs: %s", exc)


def register_routes(app: "web.Application", adapter: Any, registry: RunBufferRegistry) -> None:
    """Wire the dashboard endpoints into the api_server's aiohttp app."""

    async def _start(request: "web.Request") -> "web.Response":
        return await handle_start_run(adapter, registry, request)

    async def _events(request: "web.Request") -> "web.StreamResponse":
        return await handle_run_events(adapter, registry, request)

    async def _snapshot(request: "web.Request") -> "web.Response":
        return await handle_run_snapshot(adapter, registry, request)

    async def _cancel(request: "web.Request") -> "web.Response":
        return await handle_run_cancel(adapter, registry, request)

    async def _stop(request: "web.Request") -> "web.Response":
        return await handle_run_stop(adapter, registry, request)

    async def _steer(request: "web.Request") -> "web.Response":
        return await handle_run_steer(adapter, registry, request)

    async def _approval(request: "web.Request") -> "web.Response":
        return await handle_run_approval(adapter, registry, request)

    async def _secret(request: "web.Request") -> "web.Response":
        return await handle_run_secret(adapter, registry, request)

    async def _clarify(request: "web.Request") -> "web.Response":
        return await handle_run_clarify(adapter, registry, request)

    async def _control_get(request: "web.Request") -> "web.Response":
        return await handle_session_control_get(adapter, registry, request)

    async def _control_post(request: "web.Request") -> "web.Response":
        return await handle_session_control_post(adapter, registry, request)

    async def _subagents(request: "web.Request") -> "web.Response":
        return await handle_session_subagents(adapter, registry, request)

    async def _subagent_start(request: "web.Request") -> "web.Response":
        return await handle_start_subagent(adapter, registry, request)

    async def _subagent_list(request: "web.Request") -> "web.Response":
        return await handle_list_subagents(adapter, registry, request)

    async def _subagent_snapshot(request: "web.Request") -> "web.Response":
        return await handle_subagent_snapshot(adapter, registry, request)

    app.router.add_post("/v1/dashboard/runs", _start)
    app.router.add_get("/v1/dashboard/runs/{run_id}/events", _events)
    app.router.add_get("/v1/dashboard/runs/{run_id}", _snapshot)
    app.router.add_delete("/v1/dashboard/runs/{run_id}", _cancel)
    app.router.add_post("/v1/dashboard/runs/{run_id}/stop", _stop)
    app.router.add_post("/v1/dashboard/runs/{run_id}/steer", _steer)
    app.router.add_post("/v1/dashboard/runs/{run_id}/approval", _approval)
    app.router.add_post("/v1/dashboard/runs/{run_id}/secret", _secret)
    app.router.add_post("/v1/dashboard/runs/{run_id}/clarify", _clarify)
    app.router.add_post("/v1/dashboard/subagents", _subagent_start)
    app.router.add_get("/v1/dashboard/subagents", _subagent_list)
    app.router.add_get("/v1/dashboard/subagents/{run_id}", _subagent_snapshot)
    app.router.add_post("/v1/dashboard/subagents/{run_id}/stop", _stop)
    app.router.add_post("/v1/dashboard/subagents/{run_id}/steer", _steer)
    app.router.add_get("/v1/dashboard/sessions/{session_id}/control", _control_get)
    app.router.add_post("/v1/dashboard/sessions/{session_id}/control", _control_post)
    app.router.add_get("/v1/dashboard/sessions/{session_id}/subagents", _subagents)
