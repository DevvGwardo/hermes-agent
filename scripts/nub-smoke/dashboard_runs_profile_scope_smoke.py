"""
Smoke test: dashboard runs mint key_cmd credentials inside a profile scope.

Under ``gateway.multiplex_profiles: true`` the fork fails closed on any
credential read made outside a profile secret scope. A lease-backed VM mints
its inference key with ``key_cmd`` from the agent's worker thread, so a
dashboard run that never entered the scope failed every call with
UnscopedSecretError ("custom didn't answer after 3 attempts"). Seen on
2026-09-28 in a group room on the owner's canary VM (maiavm-2026.09.28).

Runs the REAL modules (gateway/platforms/dashboard_runs.py +
run_buffer.py) inside a checkout of the DevvGwardo/hermes-agent fork with
multiplexing switched on. The fake adapter borrows the fork's real
``APIServerAdapter._profile_scope``, and the fake agent mints a token through
the fork's real ``CommandTokenSource`` both while it is built and from the
worker thread, like the OpenAI client's per-request key refresh does.

Usage (from the repo root):
  .venv/bin/python scripts/nub-smoke/dashboard_runs_profile_scope_smoke.py

Needs aiohttp, pyyaml, ruamel.yaml, python-dotenv, openai in the interpreter.
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

from agent.command_token_source import CommandTokenSource  # noqa: E402
from agent.secret_scope import set_multiplex_active  # noqa: E402
from gateway.platforms import dashboard_runs  # noqa: E402
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402
from gateway.platforms.run_buffer import RunBufferRegistry  # noqa: E402

TOKEN = "lease-token-smoke"


class FakeAgent:
    def __init__(self, source: CommandTokenSource):
        self.source = source
        self.clarify_callback = None

    def run_conversation(self, user_message, conversation_history, task_id):
        # Worker thread: the OpenAI client re-mints here when the lease lapses.
        self.source._token = ""
        return {"final_response": self.source()}


class FakeAdapter:
    _profile_scope = staticmethod(APIServerAdapter._profile_scope)

    def __init__(self):
        self._background_tasks = set()
        self._response_store = {}

    def _check_auth(self, request):
        return None

    def _create_agent(self, **kwargs):
        source = CommandTokenSource(f"printf {TOKEN}", "maia")
        source()  # runtime resolution may mint while the agent is built
        return FakeAgent(source)


async def main() -> int:
    set_multiplex_active(True)
    registry = RunBufferRegistry()
    app = web.Application()
    app["registry"] = registry
    dashboard_runs.register_routes(app, FakeAdapter(), registry)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        res = await client.post("/v1/dashboard/runs", json={"input": "hi", "session_id": "room:smoke"})
        assert res.status == 202, f"start: {res.status} {await res.text()}"
        run_id = (await res.json())["run_id"]
        deadline = time.monotonic() + 15
        snap: dict = {}
        while time.monotonic() < deadline:
            snap = await (await client.get(f"/v1/dashboard/runs/{run_id}")).json()
            if snap.get("status") in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        print(json.dumps({k: snap.get(k) for k in ("status", "error", "final_response", "output")}, default=str))
        text = json.dumps(snap, default=str)
        assert snap.get("status") == "completed", f"run did not complete: {text[:400]}"
        assert "UnscopedSecretError" not in text and "profile-scoping" not in text, text[:400]
        assert TOKEN in text, "minted token did not reach the run result"
    finally:
        await client.close()
        set_multiplex_active(False)
    print("PASS dashboard runs mint key_cmd inside the profile scope")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
