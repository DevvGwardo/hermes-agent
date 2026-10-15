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

- GET /v1/dashboard/runs/{run_id}
    JSON snapshot of run state. Polling fallback for clients that
    can't hold an SSE connection.

- DELETE /v1/dashboard/runs/{run_id}
    Explicit cancel. Calls ``agent.interrupt()`` and cancels the
    asyncio task.

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
    session_id = body.get("session_id") or stored_session_id or run_id

    try:
        buf = await registry.create(run_id)
    except RuntimeError as exc:
        return web.json_response(_openai_error(str(exc), code="rate_limit_exceeded"), status=429)

    loop = asyncio.get_running_loop()

    # Opt-in secret/clarify prompts (fork builds only): the client declares
    # them with X-Hermes-Client-Capabilities (or a client_capabilities body
    # field). The broker's events go on this run's durable stream exactly like
    # approval.request; POST .../secret and .../clarify answer them.
    capabilities: frozenset = frozenset()
    if RunPromptBroker is not None and parse_client_capabilities is not None:
        try:
            capabilities = parse_client_capabilities(
                request.headers.get(CLIENT_CAPABILITIES_HEADER),
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

    def _on_delta(delta: Optional[str]) -> None:
        # The agent emits None as a CLI box-close signal, not EOS.
        if delta is None:
            return
        buf.append_threadsafe(loop, {
            "event": "message.delta",
            "run_id": run_id,
            "delta": delta,
        })

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

    app.router.add_post("/v1/dashboard/runs", _start)
    app.router.add_get("/v1/dashboard/runs/{run_id}/events", _events)
    app.router.add_get("/v1/dashboard/runs/{run_id}", _snapshot)
    app.router.add_delete("/v1/dashboard/runs/{run_id}", _cancel)
    app.router.add_post("/v1/dashboard/runs/{run_id}/stop", _stop)
    app.router.add_post("/v1/dashboard/runs/{run_id}/steer", _steer)
    app.router.add_post("/v1/dashboard/runs/{run_id}/approval", _approval)
    app.router.add_post("/v1/dashboard/runs/{run_id}/secret", _secret)
    app.router.add_post("/v1/dashboard/runs/{run_id}/clarify", _clarify)
