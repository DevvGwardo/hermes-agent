"""
Smoke test: a dashboard run cannot take a detached (background) completion.

A dashboard run ends with its turn: nothing reads the session afterwards, so a
background ``delegate_task`` result has nowhere to land. Before the fix the
unbound session context read as push-capable; on 2026-09-29 a group room's
Chief dispatched a background subagent, said "I'll post the summary here", and
the finished result was dropped after 8 delivery attempts.

Runs the REAL modules (gateway/platforms/dashboard_runs.py +
run_buffer.py) inside a hermes-agent checkout. The fake agent reads the real
``async_delivery_supported()`` from a tool thread (context propagated the way
the agent's tool executor does it) and the smoke checks that:
  1. it is False during the run, so delegate_task runs the batch inline;
  2. the pooled executor thread is back to the default afterwards, so the
     flag can't leak into unrelated work that reuses the thread.

Usage (from the repo root):
  .venv/bin/python scripts/nub-smoke/dashboard_runs_sync_delegation_smoke.py

Needs aiohttp in the interpreter.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
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
from gateway.session_context import async_delivery_supported  # noqa: E402

# One worker, so the "after" probe runs on the thread that served the run.
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)


class FakeAgent:
    clarify_callback = None

    def run_conversation(self, user_message, conversation_history, task_id):
        # Tools run on their own thread with the caller's context copied.
        ctx = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as tools:
            seen = tools.submit(ctx.run, async_delivery_supported).result()
        return {"final_response": f"async_delivery={seen}"}


class FakeAdapter:
    def __init__(self):
        self._background_tasks = set()
        self._response_store = {}

    def _check_auth(self, request):
        return None

    def _create_agent(self, **kwargs):
        return FakeAgent()


async def main() -> int:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(POOL)
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
        text = json.dumps(snap, default=str)
        print(text[:300])
        assert snap.get("status") == "completed", f"run did not complete: {text[:400]}"
        assert "async_delivery=False" in text, "the run's tools still see async delivery as supported"
        after = await loop.run_in_executor(None, async_delivery_supported)
        assert after is True, "the flag leaked into the pooled executor thread"
    finally:
        await client.close()
    print("PASS dashboard runs run delegate_task inline and reset the flag")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
