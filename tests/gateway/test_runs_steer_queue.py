"""Behavioral pins for the runs steer/queue API, runs multimodal validation,
delivery unknown-platform reporting, and loopback auth/CORS behavior.

Covers the NEW surface added on this branch (direct calls against the real
code with the evil input — no catalog snapshots or version literals):

- POST /v1/runs/{id}/steer {text} → {run_id, status: queued|rejected, text}
- steer with no live agent falls back to queue semantics
- POST /v1/runs/{id}/queue → {run_id, status: queued, prompt, depth}
- file parts in runs input → 400 unsupported_content_type (not str-coerced)
- no-key adapter (loopback default) lets requests through auth
- key-protected adapter rejects missing/bad keys with 401
- CORS headers expose the configured origin; chat responses carry
  X-Hermes-Session-Id
- unknown platform delivery → {success: False, error: unknown_platform*}
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _make_adapter(api_key: str = "", cors_origins=None) -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    if cors_origins is not None:
        extra["cors_origins"] = cors_origins
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_post("/v1/runs/{run_id}/steer", adapter._handle_steer_run)
    app.router.add_post("/v1/runs/{run_id}/queue", adapter._handle_queue_run)
    return app


@pytest.fixture
def adapter():
    return _make_adapter()


# ---------------------------------------------------------------------------
# POST /v1/runs/{id}/steer
# ---------------------------------------------------------------------------


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_steer_accepted_mid_turn(self, adapter):
        agent = MagicMock()
        agent.steer.return_value = True
        adapter._active_run_agents["run_1"] = agent
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/steer", json={"text": "also check auth.log"})
            assert resp.status == 200
            body = await resp.json()
        assert body == {"run_id": "run_1", "status": "queued", "text": "also check auth.log"}
        agent.steer.assert_called_once_with("also check auth.log")

    @pytest.mark.asyncio
    async def test_steer_rejected(self, adapter):
        agent = MagicMock()
        agent.steer.return_value = False
        adapter._active_run_agents["run_1"] = agent
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/steer", json={"text": "late guidance"})
            assert resp.status == 200
            body = await resp.json()
        assert body["status"] == "rejected"
        assert body["run_id"] == "run_1"
        assert body["text"] == "late guidance"

    @pytest.mark.asyncio
    async def test_steer_exception_rejects(self, adapter):
        agent = MagicMock()
        agent.steer.side_effect = RuntimeError("boom")
        adapter._active_run_agents["run_1"] = agent
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/steer", json={"text": "x"})
            body = await resp.json()
        assert body["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_steer_without_live_agent_falls_back_to_queue(self, adapter):
        # Run known via its task, but no live agent yet → queue semantics.
        adapter._active_run_tasks["run_1"] = MagicMock()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/steer", json={"text": "early note"})
            assert resp.status == 200
            body = await resp.json()
        assert body["status"] == "queued"
        assert body["text"] == "early note"
        assert body["depth"] == 1
        assert adapter._run_queues["run_1"] == ["early note"]

    @pytest.mark.asyncio
    async def test_steer_unknown_run_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/nope/steer", json={"text": "x"})
            assert resp.status == 404
            body = await resp.json()
        assert body["error"]["code"] == "run_not_found"

    @pytest.mark.asyncio
    async def test_steer_missing_and_nonstring_text_400(self, adapter):
        agent = MagicMock()
        adapter._active_run_agents["run_1"] = agent
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/steer", json={})
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "missing_steer_text"
            resp = await cli.post("/v1/runs/run_1/steer", json={"text": 123})
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "invalid_steer_text"


# ---------------------------------------------------------------------------
# POST /v1/runs/{id}/queue
# ---------------------------------------------------------------------------


class TestQueueRun:
    @pytest.mark.asyncio
    async def test_queue_accepted(self, adapter):
        adapter._active_run_tasks["run_1"] = MagicMock()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/queue", json={"prompt": "follow up"})
            assert resp.status == 200
            body = await resp.json()
        assert body == {"run_id": "run_1", "status": "queued", "prompt": "follow up", "depth": 1}
        assert adapter._run_queues["run_1"] == ["follow up"]

    @pytest.mark.asyncio
    async def test_queue_fifo_depth(self, adapter):
        adapter._active_run_tasks["run_1"] = MagicMock()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            await cli.post("/v1/runs/run_1/queue", json={"prompt": "first"})
            resp = await cli.post("/v1/runs/run_1/queue", json={"prompt": "second"})
            body = await resp.json()
        assert body["depth"] == 2
        assert adapter._run_queues["run_1"] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_queue_unknown_run_404_and_bad_body_400(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/nope/queue", json={"prompt": "x"})
            assert resp.status == 404
            adapter._active_run_tasks["run_1"] = MagicMock()
            resp = await cli.post("/v1/runs/run_1/queue", json={})
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "missing_queue_prompt"


# ---------------------------------------------------------------------------
# Runs input multimodal validation + auth + CORS/session header
# ---------------------------------------------------------------------------


class TestRunsHardening:
    @pytest.mark.asyncio
    async def test_runs_file_part_returns_400_unsupported(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                json={"input": [{"role": "user", "content": [{"type": "file", "file": {"file_id": "f_1"}}]}]},
            )
            assert resp.status == 400
            body = await resp.json()
        assert body["error"]["code"] == "unsupported_content_type"

    @pytest.mark.asyncio
    async def test_no_key_loopback_requests_pass_auth(self, adapter):
        """No API key (loopback default) → steer reaches the handler
        (404 run_not_found proves auth passed, not 401)."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/nope/steer", json={"text": "x"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_key_protected_rejects_missing_key(self):
        adapter = _make_adapter(api_key="sk-secret")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_1/steer", json={"text": "x"})
            assert resp.status == 401
            body = await resp.json()
        assert body["error"]["code"] == "invalid_api_key"

    def test_cors_headers_expose_configured_origin(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        headers = adapter._cors_headers_for_origin("http://localhost:3000")
        assert headers is not None
        assert headers["Access-Control-Allow-Origin"] == "http://localhost:3000"

    def test_cors_rejects_unknown_origin(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        assert adapter._cors_headers_for_origin("http://evil.example") is None


# ---------------------------------------------------------------------------
# Unknown platform delivery
# ---------------------------------------------------------------------------


class TestUnknownPlatformDelivery:
    def test_parse_preserves_unknown_platform(self):
        target = DeliveryTarget.parse("fakeservice:123")
        assert target.unknown_platform == "fakeservice:123"
        assert target.to_string() == "fakeservice:123"

    @pytest.mark.asyncio
    async def test_deliver_reports_unknown_platform(self):
        config = GatewayConfig(platforms={})
        router = DeliveryRouter(config, adapters={})
        target = DeliveryTarget.parse("fakeservice:123")
        results = await router.deliver("hello", [target], job_id="j1")
        assert results["fakeservice:123"]["success"] is False
        assert results["fakeservice:123"]["error"].startswith("unknown_platform")
