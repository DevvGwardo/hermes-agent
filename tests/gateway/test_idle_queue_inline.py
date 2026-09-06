"""Pin idle ``/queue`` inline handling in gateway/run.py.

With no active agent, ``/queue <prompt>`` must be handled inline — the
payload runs as a normal user turn right now (mirroring the idle /steer
path) — and must NOT leak to the agent as literal "/queue ..." text nor
be staged into the turn-boundary FIFO slot/overflow.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(),
        message_id="m1",
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, adapter


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


@pytest.mark.asyncio
async def test_idle_queue_empty_payload_returns_usage():
    """Idle ``/queue`` with no prompt surfaces the usage hint."""
    runner, _adapter = _make_runner(_session_entry())
    result = await runner._handle_message(_make_event("/queue"))
    assert result is not None
    assert "Usage" in result or "usage" in result
    assert "/queue" in result


@pytest.mark.asyncio
async def test_idle_queue_payload_runs_inline_as_normal_turn():
    """Idle ``/queue <prompt>`` strips the prefix and dispatches via the
    normal user-turn pipeline — never staged into the turn-boundary FIFO,
    never leaked as literal "/queue ..." text."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())

    captured = {}

    async def _fake_pipeline(*args, **kwargs):
        captured["event"] = args[0] if args else kwargs.get("event")
        return "sent"

    runner._handle_message_with_agent = _fake_pipeline  # type: ignore[method-assign]

    await runner._handle_message(_make_event("/queue follow up soon"))

    assert "event" in captured
    assert captured["event"].text == "follow up soon"
    assert not captured["event"].text.startswith("/queue")
    # Turn-boundary FIFO must stay empty on the idle path.
    assert runner._pending_messages == {}
    assert adapter._pending_messages == {}
    assert sk not in runner._running_agents
