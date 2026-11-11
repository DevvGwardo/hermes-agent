"""The Nub dashboard runs API is native on the maiavm fork.

hermes-deploy used to patch /v1/dashboard/runs into api_server.py on every VM
and re-apply it on each gateway start. The fork now ships it; hermes-deploy
detects the ``maiavm-native-dashboard-runs`` marker and stops patching. These
tests pin that contract: the marker, the routes on a real connect(), and the
``_dash_register`` name older repair scripts use to skip their patch.

They also pin ``message.segment``: the marker a dashboard run emits when
streamed assistant text closes before a tool batch, so one reply can render as
several bubbles (the hermes-deploy chat-segments contract).
"""

import inspect
import socket
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server, dashboard_runs
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.run_buffer import RunBufferRegistry
from tests.waiting import async_wait_until

_KEY = "dashboard-runs-test-key-0123456789"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_marker_and_legacy_sentinel_present():
    src = inspect.getsource(api_server)
    # hermes-deploy keys off this exact string (NATIVE_DASHBOARD_RUNS_MARKER).
    assert src.count("maiavm-native-dashboard-runs") == 1
    # Older overlay-repair.py copies skip DR-02 when this name is present.
    assert "_dash_register" in src
    assert "_dashboard_run_registry" in src


@pytest.mark.asyncio
async def test_connect_registers_dashboard_runs_routes():
    port = _free_port()
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": port, "key": _KEY})
    )
    assert await adapter.connect() is True
    try:
        assert adapter._dashboard_run_registry is not None
        base = f"http://127.0.0.1:{port}/v1/dashboard/runs"
        auth = {"Authorization": f"Bearer {_KEY}"}
        async with aiohttp.ClientSession() as http:
            # Unknown run: the dashboard handler answers, not the router's 404.
            async with http.get(f"{base}/nope", headers=auth) as res:
                assert res.status == 404
                assert "run_not_found" in await res.text()
            # hermes-deploy's move-vm-agent-pin verification probe.
            async with http.post(f"{base}/nope/secret", headers=auth, json={}) as res:
                assert res.status == 404
                assert "run_not_found" in await res.text()
            # Bearer auth still applies.
            async with http.delete(f"{base}/nope") as res:
                assert res.status == 401
    finally:
        await adapter.disconnect()


def test_events_resume_cursor_contract():
    # hermes-deploy resumes a stream with ?after=<last event id> (withAfterCursor,
    # src/lib/chat-runs.ts) as well as Last-Event-Id. Keep both.
    from gateway.platforms import dashboard_runs

    src = inspect.getsource(dashboard_runs)
    assert 'request.query.get("after")' in src
    assert "Last-Event-Id" in src


# ---------------------------------------------------------------------------
# message.segment — one bubble per assistant-text run closed by a tool batch
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """The APIServerAdapter surface ``handle_start_run`` touches."""

    def __init__(self, create_agent):
        self._create_agent = create_agent
        self._background_tasks: set = set()
        self._response_store: dict = {}

    def _check_auth(self, _request):
        return None


def _assistant_stream(buf):
    """Assistant-text wire order: deltas and segment markers only."""
    return [
        (e["event"], e.get("delta") if e["event"] == "message.delta" else e.get("index"))
        for e in buf.events
        if e["event"] in {"message.delta", "message.segment"}
    ]


async def _run_script(script):
    """Run one dashboard run whose fake agent replays ``script(delta, interim)``."""
    registry = RunBufferRegistry()

    def create_agent(**kwargs):
        delta = kwargs["stream_delta_callback"]
        interim = kwargs["interim_assistant_callback"]
        agent = MagicMock()

        def run_conversation(**_kwargs):
            script(delta, interim)
            return {"final_response": "All done."}

        agent.run_conversation.side_effect = run_conversation
        agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
        return agent

    app = web.Application()
    dashboard_runs.register_routes(app, _FakeAdapter(create_agent), registry)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/dashboard/runs", json={"input": "hello"})
        assert response.status == 202, await response.text()
        buf = registry.get((await response.json())["run_id"])
        assert buf is not None
        await async_wait_until(
            lambda: buf.terminal and buf._agent_task.done(),
            what="dashboard run to finish",
        )
    return buf


@pytest.mark.asyncio
async def test_commentary_before_a_tool_batch_emits_a_segment_marker():
    def script(delta, interim):
        delta("Let me check the config. ")
        interim("Let me check the config. ", already_streamed=True)
        delta("The answer is 42.")

    buf = await _run_script(script)

    # The marker sits between the two streamed parts, index counted from 1.
    assert _assistant_stream(buf) == [
        ("message.delta", "Let me check the config. "),
        ("message.segment", 1),
        ("message.delta", "The answer is 42."),
    ]
    marker = next(e for e in buf.events if e["event"] == "message.segment")
    assert marker["run_id"] == buf.run_id
    assert isinstance(marker["timestamp"], float)


@pytest.mark.asyncio
async def test_no_marker_when_nothing_streamed_since_the_last_one():
    def script(delta, interim):
        delta("Reading the file. ")
        interim("Reading the file. ", already_streamed=True)
        # Second tool round: no assistant text streamed since the marker.
        interim("Reading another file. ", already_streamed=True)
        delta("Done.")

    buf = await _run_script(script)

    assert [e["index"] for e in buf.events if e["event"] == "message.segment"] == [1]
    assert _assistant_stream(buf) == [
        ("message.delta", "Reading the file. "),
        ("message.segment", 1),
        ("message.delta", "Done."),
    ]


@pytest.mark.asyncio
async def test_unstreamed_commentary_is_sent_as_a_delta_before_the_marker():
    def script(delta, interim):
        # already_streamed=False: the client never saw these tokens as deltas.
        interim("Checking the config.", already_streamed=False)
        delta("Done.")

    buf = await _run_script(script)

    assert _assistant_stream(buf) == [
        ("message.delta", "Checking the config."),
        ("message.segment", 1),
        ("message.delta", "Done."),
    ]


@pytest.mark.asyncio
async def test_final_response_alone_emits_no_marker():
    def script(delta, _interim):
        delta("The answer is 42.")

    buf = await _run_script(script)

    assert _assistant_stream(buf) == [("message.delta", "The answer is 42.")]
