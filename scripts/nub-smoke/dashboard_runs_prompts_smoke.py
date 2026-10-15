"""
Smoke test: secret/clarify prompts through the dashboard runs API.

Runs the REAL modules (gateway/platforms/dashboard_runs.py +
run_buffer.py) inside a checkout of the DevvGwardo/hermes-agent fork, on an
aiohttp test server, with a fake adapter/agent. The agent asks one clarify
question and one secret; the test answers both through the new
POST /v1/dashboard/runs/{id}/clarify and /secret routes and checks:

  - clarify.request / secret.request / *.responded land on the event stream
  - the agent thread receives the answers and the run completes
  - the secret value is saved to $HERMES_HOME/.env and never appears in any
    event or response body
  - a run started WITHOUT the capability header gets 409 <kind>_not_active

Usage (from the repo root):
  .venv/bin/python scripts/nub-smoke/dashboard_runs_prompts_smoke.py

Needs aiohttp, pyyaml, ruamel.yaml, python-dotenv in the interpreter.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

HOME = tempfile.mkdtemp(prefix="hermes-home-")
os.environ["HERMES_HOME"] = HOME
sys.path.insert(0, os.getcwd())

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from gateway.platforms import dashboard_runs  # noqa: E402
from gateway.platforms.run_buffer import RunBufferRegistry  # noqa: E402

SECRET_VALUE = "smoke-secret-value-9f8e7d"


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.clarify_callback = None
        self.answers = {}

    def run_conversation(self, user_message, conversation_history, task_id):
        if self.clarify_callback is not None:
            self.answers["clarify"] = self.clarify_callback("Which color?", ["red", "blue"])
        from tools.skills_tool import _scoped_secret_capture_callback  # type: ignore

        cb = _scoped_secret_capture_callback.get()
        if cb is not None:
            self.answers["secret"] = cb("SMOKE_API_KEY", "Paste your smoke key", {"skill_name": "smoke"})
        return {"final_response": json.dumps(self.answers, default=str)}


class FakeAdapter:
    def __init__(self):
        self._background_tasks = set()
        self._response_store = {}
        self.created = []

    def _check_auth(self, request):
        return None

    def _create_agent(self, **kwargs):
        agent = FakeAgent(**kwargs)
        self.created.append(agent)
        return agent


async def wait_event(client, run_id, name, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf = client.app["registry"].get(run_id)
        for ev in list(buf.events):
            if ev.get("event") == name:
                return ev
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {name}")


async def main() -> int:
    app = web.Application()
    adapter = FakeAdapter()
    registry = RunBufferRegistry()
    app["registry"] = registry
    dashboard_runs.register_routes(app, adapter, registry)
    client = TestClient(TestServer(app))
    await client.start_server()
    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f" ({detail})" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        # 1. Run WITHOUT capabilities: prompts are not active.
        r = await client.post("/v1/dashboard/runs", json={"input": "hi"})
        plain = (await r.json())["run_id"]
        r = await client.post(f"/v1/dashboard/runs/{plain}/secret", json={"request_id": "x", "value": "v"})
        body = await r.json()
        check("no capability → 409 secret_not_active", r.status == 409 and "secret_not_active" in json.dumps(body))
        check("no capability → clarify toolset not requested", "extra_toolsets" not in adapter.created[0].kwargs)

        # 2. Run WITH capabilities.
        r = await client.post(
            "/v1/dashboard/runs",
            json={"input": "hi"},
            headers={"X-Hermes-Client-Capabilities": "clarify,secret"},
        )
        run_id = (await r.json())["run_id"]
        creq = await wait_event(client, run_id, "clarify.request")
        check("clarify.request carries question + choices",
              creq.get("question") == "Which color?" and creq.get("choices") == ["red", "blue"])
        check("clarify toolset requested", adapter.created[1].kwargs.get("extra_toolsets") == ["clarify"])

        r = await client.post(f"/v1/dashboard/runs/{run_id}/clarify",
                              json={"request_id": creq["request_id"], "answer": "blue"})
        check("clarify answer → 200", r.status == 200, await r.text())
        await wait_event(client, run_id, "clarify.responded")

        sreq = await wait_event(client, run_id, "secret.request")
        check("secret.request names the env var", sreq.get("env_var") == "SMOKE_API_KEY")
        r = await client.post(f"/v1/dashboard/runs/{run_id}/secret",
                              json={"request_id": sreq["request_id"], "value": SECRET_VALUE})
        text = await r.text()
        check("secret answer → 200", r.status == 200, text)
        check("secret value not echoed in the response", SECRET_VALUE not in text)
        sresp = await wait_event(client, run_id, "secret.responded")
        check("secret.responded skipped=false", sresp.get("skipped") is False)

        await wait_event(client, run_id, "run.completed")
        buf = registry.get(run_id)
        check("secret value never on the event stream", SECRET_VALUE not in json.dumps(buf.events, default=str))
        answers = adapter.created[1].answers
        check("agent received the clarify answer", answers.get("clarify") == "blue", repr(answers))
        check("agent's secret capture succeeded", isinstance(answers.get("secret"), dict)
              and answers["secret"].get("skipped") is False, repr(answers.get("secret")))
        env_path = os.path.join(HOME, ".env")
        saved = open(env_path).read() if os.path.exists(env_path) else ""
        check("secret saved to $HERMES_HOME/.env", f"SMOKE_API_KEY={SECRET_VALUE}" in saved.replace('"', "").replace("'", ""))

        # 3. Answering a finished prompt → 409 not_pending.
        r = await client.post(f"/v1/dashboard/runs/{run_id}/clarify",
                              json={"request_id": creq["request_id"], "answer": "red"})
        check("stale request_id → 409 clarify_not_pending", r.status == 409)
        r = await client.post(f"/v1/dashboard/runs/run_nope/clarify", json={"request_id": "a", "answer": "b"})
        check("unknown run → 404", r.status == 404)
    finally:
        await client.close()

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
