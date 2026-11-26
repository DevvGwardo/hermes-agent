"""Dashboard session control + subagent lineage (fork addition).

Pins the contract hermes-deploy's chat relies on:

- ``GET/POST /v1/dashboard/sessions/{id}/control`` drive the persisted goal
  through ``GoalManager`` (set / pause / resume / clear / status), refuse the
  gate actions, and answer with the same ``{goal, loop, heartbeat, revision}``
  envelope the Desktop card reads.
- Every mutating action appends ``session.control.update`` to each live run of
  that session (SSE consumers repaint from the event, not the reply).
- A dashboard run with an active goal advances it after the turn:
  ``evaluate_after_turn`` runs, the event stream carries the update and a
  ``goal.continuation`` boundary, and the continuation turn runs on the same
  agent — bounded by ``HERMES_DASHBOARD_GOAL_MAX_CONTINUATIONS``.
- ``POST /v1/dashboard/runs`` accepts ``parent_session_id`` / ``source`` so a
  delegation child is recorded with lineage and never drives the parent goal.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import dashboard_runs
from gateway.platforms.run_buffer import RunBufferRegistry

_TOKEN = "dashboard-control-test-key-0123456789"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB state_meta writes never touch the real one."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    # Warm the SessionDB off the event loop (the fixture runs outside it): a
    # cold cache first touched from a loop thread only gets a bounded grace
    # window, and a slow CI init drops the goal write.
    goals.load_goal("__dashboard_control_warmup__")
    yield home
    goals._DB_CACHE.clear()


class _FakeAgent:
    """The AIAgent surface a dashboard run uses, replaying canned replies."""

    def __init__(self, replies: List[str] | None = None) -> None:
        self.replies = list(replies or ["done"])
        self.calls: List[Dict[str, Any]] = []
        self.interrupted = False
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0

    def run_conversation(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        reply = self.replies[index]
        return dict(reply) if isinstance(reply, dict) else {"final_response": reply}

    def interrupt(self, *_args, **_kwargs) -> None:
        self.interrupted = True

    def steer(self, _text: str) -> bool:
        return True


class _Adapter:
    def __init__(self, agent: _FakeAgent | None = None) -> None:
        self.agent = agent or _FakeAgent()
        self.created_with: List[Dict[str, Any]] = []
        self._background_tasks: set = set()
        self._response_store: dict = {}

    def _create_agent(self, **kwargs):
        self.created_with.append(kwargs)
        return self.agent

    def _check_auth(self, request):
        if request.headers.get("Authorization") == f"Bearer {_TOKEN}":
            return None
        return web.json_response({"error": {"message": "unauthorized"}}, status=401)


def _app(adapter: _Adapter | None = None) -> tuple[web.Application, RunBufferRegistry, _Adapter]:
    registry = RunBufferRegistry()
    adapter = adapter or _Adapter()
    app = web.Application()
    dashboard_runs.register_routes(app, adapter, registry)
    return app, registry, adapter


async def _wait_run(registry: RunBufferRegistry, run_id: str) -> None:
    from tests.waiting import async_wait_until

    buf = registry.get(run_id)
    assert buf is not None
    await async_wait_until(lambda: buf.terminal and buf._agent_task.done(), what="dashboard run")


def _events(buf, name: str):
    return [event for event in buf.events if event["event"] == name]


# ---------------------------------------------------------------------------
# GET / control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_requires_bearer(hermes_home):
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/dashboard/sessions/sid-1/control")
        assert response.status == 401
        response = await client.post(
            "/v1/dashboard/sessions/sid-1/control",
            json={"action": "goal.status"},
        )
        assert response.status == 401


@pytest.mark.asyncio
async def test_get_control_reports_no_goal(hermes_home):
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/dashboard/sessions/sid-empty/control", headers=_AUTH)
        assert response.status == 200
        body = await response.json()
    assert body["object"] == "hermes.session.control"
    assert body["session_id"] == "sid-empty"
    assert body["control"]["goal"] is None
    assert body["control"]["revision"] == ""


@pytest.mark.asyncio
async def test_goal_lifecycle_via_control(hermes_home):
    from hermes_cli.goals import GoalManager

    sid = "sid-lifecycle"
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.set", "goal": "ship the fix", "max_turns": 5},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
        assert body["action"] == "goal.set"
        goal = body["control"]["goal"]
        assert goal["title"] == "ship the fix"
        assert goal["status"] == "active"
        assert goal["turns_used"] == 0
        assert goal["max_turns"] == 5
        assert "ship the fix" in body["message"]
        assert GoalManager(sid).state.goal == "ship the fix"

        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "goal.pause"},
        )
        assert response.status == 200
        paused = (await response.json())["control"]["goal"]
        assert paused["status"] == "paused"
        assert paused["paused_reason"] == "user-paused"
        assert GoalManager(sid).state.status == "paused"

        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "goal.resume"},
        )
        assert response.status == 200
        resumed = (await response.json())["control"]["goal"]
        assert resumed["status"] == "active"
        assert resumed["turns_used"] == 0

        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "goal.status"},
        )
        assert response.status == 200
        assert (await response.json())["control"]["goal"]["status"] == "active"

        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "goal.clear"},
        )
        assert response.status == 200
        assert (await response.json())["control"]["goal"] is None
        cleared = GoalManager(sid)
        assert cleared.state is None or cleared.state.status == "cleared"
        assert cleared.has_goal() is False

        for action in ("goal.pause", "goal.resume"):
            response = await client.post(
                f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
                json={"action": action},
            )
            assert response.status == 409
            assert (await response.json())["error"]["code"] == "goal_not_active"


@pytest.mark.asyncio
async def test_goal_set_accepts_text_alias_and_args(hermes_home):
    from hermes_cli.goals import GoalManager

    sid = "sid-text-alias"
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.set", "text": "  tighten the tests  "},
        )
        assert response.status == 200, await response.text()
        # The platform relay's wire shape: {action, args: {text}}.
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.set", "args": {"text": "ship the relay contract"}},
        )
        assert response.status == 200, await response.text()
    assert GoalManager(sid).state.goal == "ship the relay contract"


@pytest.mark.asyncio
async def test_subgoal_actions_via_control(hermes_home):
    from hermes_cli.goals import GoalManager

    sid = "sid-subgoals"
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "subgoal.add", "args": {"text": "no goal yet"}},
        )
        assert response.status == 409
        assert (await response.json())["error"]["code"] == "goal_not_active"

        assert (await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "goal.set", "args": {"text": "finish the card"}},
        )).status == 200

        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "subgoal.add", "args": {"text": "criterion one"}},
        )
        assert response.status == 200, await response.text()
        assert (await response.json())["control"]["goal"]["subgoals"] == ["criterion one"]
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "subgoal.remove", "args": {"index": 1}},
        )
        assert response.status == 200
        assert (await response.json())["control"]["goal"]["subgoals"] == []

        await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "subgoal.add", "args": {"text": "criterion two"}},
        )
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "subgoal.clear"},
        )
        assert response.status == 200
        assert (await response.json())["control"]["goal"]["subgoals"] == []
        assert GoalManager(sid).state.subgoals == []

        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH,
            json={"action": "subgoal.remove", "args": {"index": 9}},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_subgoal_index"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,code",
    [
        ({"action": "goal.gate.add", "args": {"command": "pytest"}}, "unsupported_control_action"),
        ({"action": "goal.gate", "args": {"command": "true"}}, "unsupported_control_action"),
        ({"action": "goal.explode"}, "invalid_control_action"),
        ({}, "invalid_control_action"),
        ({"action": "goal.set", "goal": "   "}, "invalid_goal"),
        ({"action": "goal.set", "goal": "x", "max_turns": 0}, "invalid_max_turns"),
        ({"action": "goal.set", "goal": "x", "max_turns": True}, "invalid_max_turns"),
    ],
)
async def test_refused_control_actions_create_nothing(hermes_home, payload, code):
    from hermes_cli.goals import GoalManager

    sid = "sid-refused"
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control", headers=_AUTH, json=payload,
        )
        assert response.status == 400
        body = await response.json()
    assert body["error"]["code"] == code
    assert GoalManager(sid).state is None


@pytest.mark.asyncio
async def test_control_mutation_publishes_to_live_runs_only(hermes_home):
    sid = "sid-publish"
    app, registry, _adapter = _app()
    mine = await registry.create("run_mine")
    mine.session_id = sid
    other = await registry.create("run_other")
    other.session_id = "sid-other"

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.set", "goal": "publish me"},
        )
        assert response.status == 200, await response.text()
        control = (await response.json())["control"]

        updates = _events(mine, "session.control.update")
        assert len(updates) == 1
        assert updates[0]["session_id"] == sid
        assert updates[0]["control"] == control
        assert updates[0]["control"]["goal"]["title"] == "publish me"
        assert _events(other, "session.control.update") == []

        # A read never repaints the card.
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.status"},
        )
        assert response.status == 200
        assert len(_events(mine, "session.control.update")) == 1

        # The same revision is not streamed twice.
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.pause"},
        )
        response = await client.post(
            f"/v1/dashboard/sessions/{sid}/control",
            headers=_AUTH,
            json={"action": "goal.pause"},
        )
        revisions = [event["control"]["revision"] for event in _events(mine, "session.control.update")]
    assert len(revisions) == 2
    assert revisions[0] != revisions[1]


# ---------------------------------------------------------------------------
# Goal continuation inside a dashboard run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_goal_advances_after_the_turn(hermes_home, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    sid = "sid-goal-run"
    GoalManager(sid).set("finish the task")
    verdicts = [
        ("continue", "still missing the test output", False, None, False),
        ("done", "tests pass, evidence attached", False, None, False),
    ]
    monkeypatch.setattr(goals, "judge_goal", lambda *_a, **_kw: verdicts.pop(0))

    agent = _FakeAgent(["Step one done.", "All done, with test output."])
    app, registry, _adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "start the task", "session_id": sid},
        )
        assert response.status == 202, await response.text()
        run_id = (await response.json())["run_id"]
        await _wait_run(registry, run_id)

    buf = registry.get(run_id)
    assert buf.final_output == "All done, with test output."
    assert len(agent.calls) == 2
    # The continuation is a new user turn carrying the goal text.
    assert "finish the task" in agent.calls[1]["user_message"]
    assert agent.calls[1]["conversation_history"] == []

    assert len(_events(buf, "goal.continuation")) == 1
    updates = _events(buf, "session.control.update")
    assert updates, "control state must reach the SSE stream"
    assert updates[-1]["control"]["goal"]["turns_used"] == 2
    assert GoalManager(sid).state.status == "done"


@pytest.mark.asyncio
async def test_goal_continuations_are_bounded(hermes_home, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    sid = "sid-goal-cap"
    GoalManager(sid).set("never finishes")
    monkeypatch.setattr(
        goals, "judge_goal",
        lambda *_a, **_kw: ("continue", "not yet", False, None, False),
    )
    monkeypatch.setattr(dashboard_runs, "_MAX_GOAL_CONTINUATIONS", 1)

    agent = _FakeAgent(["step one", "step two", "step three"])
    app, registry, _adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "go", "session_id": sid},
        )
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        await _wait_run(registry, run_id)

    assert len(agent.calls) == 2  # the user turn + one continuation
    assert GoalManager(sid).state.status == "active"
    assert len(_events(registry.get(run_id), "goal.continuation")) == 1


@pytest.mark.asyncio
async def test_goal_continuation_can_be_disabled_per_run(hermes_home, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    sid = "sid-goal-off"
    GoalManager(sid).set("finish")
    monkeypatch.setattr(
        goals, "judge_goal",
        lambda *_a, **_kw: ("continue", "not yet", False, None, False),
    )
    agent = _FakeAgent(["one", "two"])
    app, registry, _adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "go", "session_id": sid, "goal_continuation": False},
        )
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        await _wait_run(registry, run_id)

    assert len(agent.calls) == 1
    assert GoalManager(sid).state.turns_used == 0


@pytest.mark.asyncio
async def test_failed_turn_does_not_reach_the_judge(hermes_home, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    sid = "sid-goal-failed"
    GoalManager(sid).set("finish")
    judge = MagicMock(return_value=("continue", "not yet", False, None, False))
    monkeypatch.setattr(goals, "judge_goal", judge)

    agent = _FakeAgent([{"failed": True, "error": "provider 502"}])
    app, registry, _adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "go", "session_id": sid},
        )
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        await _wait_run(registry, run_id)

    judge.assert_not_called()
    assert len(agent.calls) == 1
    assert GoalManager(sid).state.turns_used == 0
    assert registry.get(run_id).final_output == "provider 502"


@pytest.mark.asyncio
async def test_subagent_run_records_lineage_and_skips_the_parent_goal(hermes_home, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    GoalManager("parent-session").set("parent work")
    judge = MagicMock(return_value=("continue", "not yet", False, None, False))
    monkeypatch.setattr(goals, "judge_goal", judge)

    agent = _FakeAgent(["child result"])
    app, registry, adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={
                "input": "child work",
                "source": "subagent",
                "parent_session_id": "parent-session",
            },
        )
        assert response.status == 202, await response.text()
        body = await response.json()
        run_id = body["run_id"]
        assert body["source"] == "subagent"
        assert body["parent_session_id"] == "parent-session"
        assert body["session_id"].startswith("subagent:")
        await _wait_run(registry, run_id)

        snapshot = await (await client.get(f"/v1/dashboard/runs/{run_id}", headers=_AUTH)).json()
        assert snapshot["source"] == "subagent"
        assert snapshot["parent_session_id"] == "parent-session"
        assert snapshot["session_id"] == body["session_id"]

        children = await (
            await client.get("/v1/dashboard/sessions/parent-session/subagents", headers=_AUTH)
        ).json()
        assert [entry["run_id"] for entry in children["data"]] == [run_id]
        # The child's session owns no goal, and the parent's goal was not judged.
        assert adapter.created_with[0]["session_id"] == body["session_id"]

    assert judge.call_count == 0
    assert len(agent.calls) == 1
    assert GoalManager(body["session_id"]).state is None
    assert len(_events(registry.get(run_id), "goal.continuation")) == 0


@pytest.mark.asyncio
async def test_direct_subagent_routes_match_the_platform_contract(hermes_home, monkeypatch):
    """hermes-deploy's /api/chat/subagents proxy calls exactly these shapes."""
    from hermes_cli import goals

    judge = MagicMock(return_value=("continue", "not yet", False, None, False))
    monkeypatch.setattr(goals, "judge_goal", judge)

    agent = _FakeAgent(["child result"])
    app, registry, adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/subagents",
            headers=_AUTH,
            json={"session_id": "dashboard", "goal": "scrape flights"},
        )
        assert response.status == 202, await response.text()
        started = await response.json()
        run_id = started["run_id"]
        assert started["object"] == "hermes.subagent"
        assert started["child_session_id"].startswith("subagent:")
        assert started["source"] == "subagent"
        # The child prompt is the goal the platform sent.
        await _wait_run(registry, run_id)
        assert agent.calls[0]["user_message"] == "scrape flights"
        assert adapter.created_with[0]["session_id"] == started["child_session_id"]

        snapshot = await (await client.get(f"/v1/dashboard/subagents/{run_id}", headers=_AUTH)).json()
        assert snapshot["run_id"] == run_id
        assert snapshot["status"] == "completed"
        assert snapshot["source"] == "subagent"

        listing = await (
            await client.get("/v1/dashboard/subagents?session_id=dashboard", headers=_AUTH)
        ).json()
        assert [entry["run_id"] for entry in listing] == [run_id]

    assert judge.call_count == 0
    assert len(agent.calls) == 1


@pytest.mark.asyncio
async def test_direct_subagent_defaults_context_and_lineage(hermes_home):
    agent = _FakeAgent(["ok"])
    app, registry, _adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/subagents",
            headers=_AUTH,
            json={
                "goal": "summarise",
                "context": "parent said: be brief",
                "parent_run_id": "run_parent",
            },
        )
        assert response.status == 202, await response.text()
        body = await response.json()
        run_id = body["run_id"]
        assert body["child_session_id"].startswith("subagent:")
        await _wait_run(registry, run_id)

        snapshot = await (await client.get(f"/v1/dashboard/subagents/{run_id}", headers=_AUTH)).json()
        assert snapshot["parent_run_id"] == "run_parent"
        assert "parent said: be brief" in agent.calls[0]["user_message"]
        assert "summarise" in agent.calls[0]["user_message"]

        response = await client.post(
            "/v1/dashboard/subagents",
            headers=_AUTH,
            json={"goal": "   "},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_goal"


@pytest.mark.asyncio
async def test_subagent_stop_and_steer_routes(hermes_home):
    from tests.waiting import async_wait_until

    gate = threading.Event()
    started = threading.Event()
    agent = _FakeAgent(["working"])
    original = agent.run_conversation

    def blocking_run(**kwargs):
        started.set()
        gate.wait(30)
        return original(**kwargs)

    agent.run_conversation = blocking_run
    app, registry, _adapter = _app(_Adapter(agent))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/subagents",
            headers=_AUTH,
            json={"goal": "long job"},
        )
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        await async_wait_until(started.is_set, what="subagent to start")

        response = await client.post(f"/v1/dashboard/subagents/{run_id}/steer", headers=_AUTH, json={"input": "go left"})
        assert response.status == 200, await response.text()
        assert (await response.json())["accepted"] is True
        await async_wait_until(
            lambda: any(event["event"] == "run.steered" for event in registry.get(run_id).events),
            what="steer event",
        )

        response = await client.post(f"/v1/dashboard/subagents/{run_id}/stop", headers=_AUTH)
        assert response.status == 200, await response.text()
        gate.set()
        await _wait_run(registry, run_id)

    buf = registry.get(run_id)
    assert buf.cancelled is True, (buf.completed, buf.failed, buf.error)
    assert any(event["event"] == "run.steered" for event in buf.events)


@pytest.mark.asyncio
async def test_subagent_status_is_404_for_a_non_subagent_run(hermes_home):
    app, registry, _adapter = _app(_Adapter(_FakeAgent(["ok"])))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "plain run", "session_id": "sid-plain"},
        )
        run_id = (await response.json())["run_id"]
        await _wait_run(registry, run_id)

        response = await client.get(f"/v1/dashboard/subagents/{run_id}", headers=_AUTH)
        assert response.status == 404
        assert (await response.json())["error"]["code"] == "subagent_not_found"


@pytest.mark.asyncio
async def test_start_run_refuses_unknown_source_and_bad_parent(hermes_home):
    app, _registry, _adapter = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "hi", "source": "room"},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_source"

        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "hi", "parent_session_id": "   "},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_parent_session_id"

        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={"input": "hi", "session_id": 42},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_session_id"


@pytest.mark.asyncio
async def test_run_started_event_carries_lineage(hermes_home):
    app, registry, _adapter = _app(_Adapter(_FakeAgent(["ok"])))
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/dashboard/runs",
            headers=_AUTH,
            json={
                "input": "hi",
                "source": "dashboard",
                "parent_session_id": "parent-session",
                "session_id": "sid-lineage",
            },
        )
        assert response.status == 202
        run_id = (await response.json())["run_id"]
        await _wait_run(registry, run_id)

    started = _events(registry.get(run_id), "run.started")[0]
    assert started["session_id"] == "sid-lineage"
    assert started["parent_session_id"] == "parent-session"
    assert started["source"] == "dashboard"
