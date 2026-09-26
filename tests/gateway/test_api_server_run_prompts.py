"""Opt-in secret/clarify prompts on the /v1/runs stream (POST /v1/runs/{id}/secret, /clarify)."""

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server_run_prompts as prompts_mod
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_run_prompts import parse_client_capabilities

_SECRET_ENTRY = {"name": "DEMO_API_KEY", "prompt": "Enter the demo key", "help": "https://example.com/keys"}
_SECRET_VALUE = "sk-demo-VERY-secret-value-123"


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/secret", adapter._handle_run_secret)
    app.router.add_post("/v1/runs/{run_id}/clarify", adapter._handle_run_clarify)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _agent(body):
    """Mock agent whose run_conversation executes ``body(agent)`` on the executor thread."""
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
    result = {}

    def _run(user_message=None, conversation_history=None, task_id=None):
        result["value"] = body(agent)
        return {"final_response": "done"}

    agent.run_conversation.side_effect = _run
    return agent, result


def _capture_secret(_agent):
    from tools.skills_tool_setup import _capture_required_environment_variables
    return _capture_required_environment_variables("demo-skill", [dict(_SECRET_ENTRY)])


async def _next_event(stream, name: str, seen: list) -> dict:
    """Read SSE frames until the event *name* arrives (raw frames are collected in *seen*)."""
    while True:
        line = await asyncio.wait_for(stream.content.readline(), timeout=10)
        assert line, f"stream closed before {name}"
        text = line.decode()
        seen.append(text)
        if text.startswith("data: "):
            event = json.loads(text[6:])
            if event.get("event") == name:
                return event


async def _drain(stream, seen: list) -> None:
    seen.append((await asyncio.wait_for(stream.text(), timeout=10)))


def test_capabilities_are_opt_in_and_restricted_to_bridgeable_prompts():
    assert parse_client_capabilities(None, None) == frozenset()
    assert parse_client_capabilities("Clarify, telepathy", ["secret", 7]) == {"clarify", "secret"}
    assert parse_client_capabilities("", "secret clarify") == {"clarify", "secret"}


@pytest.mark.asyncio
async def test_secret_prompt_round_trip_saves_value_without_echoing_it(monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "")
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent, result = _agent(_capture_secret)
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await cli.post("/v1/runs", json={"input": "use demo"},
                                  headers={"X-Hermes-Client-Capabilities": "secret"})
            run_id = (await resp.json())["run_id"]
            stream, seen = await cli.get(f"/v1/runs/{run_id}/events"), []
            request = await _next_event(stream, "secret.request", seen)
            assert {k: request[k] for k in ("env_var", "prompt", "url", "skill")} == {
                "env_var": "DEMO_API_KEY", "prompt": "Enter the demo key",
                "url": "https://example.com/keys", "skill": "demo-skill"}
            status = await (await cli.get(f"/v1/runs/{run_id}")).json()
            assert status["pending_input"] == {"kind": "secret", "request_id": request["request_id"]}

            answer = await cli.post(f"/v1/runs/{run_id}/secret",
                                    json={"request_id": request["request_id"], "value": _SECRET_VALUE})
            answer_text = await answer.text()
            assert answer.status == 200 and json.loads(answer_text)["skipped"] is False
            await _drain(stream, seen)
            status = await (await cli.get(f"/v1/runs/{run_id}")).json()

    assert result["value"]["missing_names"] == [] and not result["value"]["gateway_setup_hint"]
    from hermes_cli.config import load_env
    assert load_env()["DEMO_API_KEY"] == _SECRET_VALUE
    assert status["status"] == "completed" and "pending_input" not in status
    assert '"secret.responded"' in "".join(seen)
    assert _SECRET_VALUE not in "".join(seen) + answer_text + json.dumps(status)


@pytest.mark.asyncio
async def test_undeclared_client_keeps_hint_and_cannot_answer(monkeypatch):
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent, result = _agent(_capture_secret)
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent) as create:
            run_id = (await (await cli.post("/v1/runs", json={"input": "x"})).json())["run_id"]
            stream, seen = await cli.get(f"/v1/runs/{run_id}/events"), []
            await _drain(stream, seen)
            late = await cli.post(f"/v1/runs/{run_id}/secret", json={"request_id": "r1", "value": "v"})
    assert "extra_toolsets" not in create.call_args.kwargs
    assert result["value"]["missing_names"] == ["DEMO_API_KEY"] and result["value"]["gateway_setup_hint"]
    assert "secret.request" not in "".join(seen)
    assert late.status == 409


@pytest.mark.asyncio
async def test_secret_timeout_expires_and_leaves_skill_unconfigured(monkeypatch):
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    monkeypatch.setattr(prompts_mod, "SECRET_PROMPT_TIMEOUT_S", 0.2)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent, result = _agent(_capture_secret)
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            run_id = (await (await cli.post(
                "/v1/runs", json={"input": "x", "client_capabilities": ["secret"]})).json())["run_id"]
            stream, seen = await cli.get(f"/v1/runs/{run_id}/events"), []
            request = await _next_event(stream, "secret.request", seen)
            expired = await _next_event(stream, "secret.expire", seen)
            await _drain(stream, seen)
            late = await cli.post(f"/v1/runs/{run_id}/secret",
                                  json={"request_id": request["request_id"], "value": "v"})
    assert expired["request_id"] == request["request_id"]
    assert result["value"]["missing_names"] == ["DEMO_API_KEY"]
    assert late.status == 409


@pytest.mark.asyncio
async def test_clarify_multi_select_answer_reaches_tool_result():
    from tools.clarify_tool import clarify_tool
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent, result = _agent(lambda a: json.loads(clarify_tool(
        "Which envs?", ["staging", "prod", "dev"], multi_select=True, callback=a.clarify_callback)))
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent) as create:
            run_id = (await (await cli.post("/v1/runs", json={"input": "x"},
                                            headers={"X-Hermes-Client-Capabilities": "clarify"})).json())["run_id"]
            stream, seen = await cli.get(f"/v1/runs/{run_id}/events"), []
            request = await _next_event(stream, "clarify.request", seen)
            wrong_kind = await cli.post(f"/v1/runs/{run_id}/secret",
                                        json={"request_id": request["request_id"], "value": "v"})
            ok = await cli.post(f"/v1/runs/{run_id}/clarify",
                                json={"request_id": request["request_id"], "answer": ["staging", "prod"]})
            await _drain(stream, seen)
    assert create.call_args.kwargs["extra_toolsets"] == ["clarify"]
    assert request["question"] == "Which envs?" and request["multi_select"] is True
    assert request["choices"][1:] == ["prod", "dev"]
    assert wrong_kind.status == 409 and ok.status == 200
    assert result["value"]["user_response"] == ["staging", "prod"]


@pytest.mark.asyncio
async def test_clarify_batch_locks_per_question_until_all_answered():
    from tools.clarify_tool import clarify_tool
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent, result = _agent(lambda a: json.loads(clarify_tool(
        "", questions=[{"question": "Name?"}, {"question": "Color?", "choices": ["red", "blue"]}],
        callback=a.clarify_callback)))
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            run_id = (await (await cli.post("/v1/runs", json={"input": "x", "client_capabilities": "clarify"})
                             ).json())["run_id"]
            stream, seen = await cli.get(f"/v1/runs/{run_id}/events"), []
            request = await _next_event(stream, "clarify.request", seen)
            rid = request["request_id"]
            first = await (await cli.post(f"/v1/runs/{run_id}/clarify",
                                          json={"request_id": rid, "question_id": "q0", "answer": "Ada"})).json()
            bad = await cli.post(f"/v1/runs/{run_id}/clarify",
                                 json={"request_id": rid, "question_id": "q9", "answer": "x"})
            await cli.post(f"/v1/runs/{run_id}/clarify", json={"request_id": rid, "question_id": "q1", "answer": "blue"})
            await _drain(stream, seen)
    assert [q["qid"] for q in request["questions"]] == ["q0", "q1"]
    assert first["remaining"] == ["q1"] and bad.status == 400
    assert [r["user_response"] for r in result["value"]["responses"]] == ["Ada", "blue"]


@pytest.mark.asyncio
async def test_stop_releases_a_blocked_prompt():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    released = threading.Event()

    def _body(agent):
        value = agent.clarify_callback("Proceed?", None)
        released.set()
        return value

    agent, _ = _agent(_body)
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            run_id = (await (await cli.post("/v1/runs", json={"input": "x"},
                                            headers={"X-Hermes-Client-Capabilities": "clarify"})).json())["run_id"]
            stream, seen = await cli.get(f"/v1/runs/{run_id}/events"), []
            await _next_event(stream, "clarify.request", seen)
            assert (await cli.post(f"/v1/runs/{run_id}/stop")).status == 200
            await _drain(stream, seen)
    assert released.is_set()
    assert "clarify.expire" not in "".join(seen)


def test_clarify_tool_is_offered_only_to_declaring_clients(monkeypatch):
    """Real api_server toolset resolution: clarify stays off unless the run asks for it."""
    from toolsets import resolve_multiple_toolsets
    captured = []

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.append(kwargs["enabled_toolsets"])

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"provider": "openrouter"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "global/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda model="": {}))
    monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda *_: None)
    adapter._create_agent(session_id="plain")
    adapter._create_agent(session_id="declared", extra_toolsets=["clarify"])
    plain, declared = (set(resolve_multiple_toolsets(ts)) for ts in captured)
    assert "clarify" not in plain
    assert declared == plain | {"clarify"}
