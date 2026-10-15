"""
Smoke test: pasted images reach the agent through the dashboard runs API.

Runs the REAL modules (gateway/platforms/dashboard_runs.py +
run_buffer.py) inside a checkout of the DevvGwardo/hermes-agent fork, on an
aiohttp test server, with a fake adapter/agent, and checks what
run_conversation receives:

  - input + input_images → a multimodal [text, image_url...] user message
  - image-only ("[image]" placeholder) → image parts only
  - no input_images → the plain string, unchanged
  - svg / http / non-string / oversize entries, or more than 4, are refused
    with a 400 (never silently dropped)

Usage (from the repo root):
  .venv/bin/python scripts/nub-smoke/dashboard_runs_images_smoke.py

Needs aiohttp, pyyaml, ruamel.yaml, python-dotenv in the interpreter.
"""
from __future__ import annotations

import asyncio
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

IMG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ"


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.received = None

    def run_conversation(self, user_message, conversation_history, task_id):
        self.received = user_message
        return {"final_response": "ok"}


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


async def wait_completed(client, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf = client.app["registry"].get(run_id)
        if any(ev.get("event") in ("run.completed", "run.failed") for ev in list(buf.events)):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("run did not finish")


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

    async def run(body):
        r = await client.post("/v1/dashboard/runs", json=body)
        assert r.status in (200, 202), await r.text()
        run_id = (await r.json())["run_id"]
        await wait_completed(client, run_id)
        return adapter.created[-1].received

    try:
        got = await run({"input": "what is this?", "input_images": [IMG]})
        check("text + image → multimodal message",
              got == [{"type": "text", "text": "what is this?"},
                      {"type": "image_url", "image_url": {"url": IMG}}], repr(got)[:200])

        got = await run({"input": "[image]", "input_images": [IMG]})
        check("image only → image parts, no placeholder text",
              got == [{"type": "image_url", "image_url": {"url": IMG}}], repr(got)[:200])

        got = await run({"input": "hello"})
        check("no images → plain string", got == "hello", repr(got))

        started = len(adapter.created)
        for name, images in [
            ("svg", ["data:image/svg+xml;base64,PHN2Zz4="]),
            ("http", ["http://a/b.png"]),
            ("non-string", [7]),
            ("more than 4", [IMG] * 5),
            ("oversize", ["data:image/png;base64," + "A" * 1_400_000]),
            ("not a list", IMG),
        ]:
            r = await client.post("/v1/dashboard/runs", json={"input": "x", "input_images": images})
            text = await r.text()
            check(f"{name} → 400 with a reason", r.status == 400 and "error" in text, f"{r.status} {text[:120]}")
        check("refused requests start no run", len(adapter.created) == started)

        got = await run({"input": "x", "input_images": [IMG] * 4})
        n_img = sum(1 for p in got if isinstance(p, dict) and p.get("type") == "image_url") if isinstance(got, list) else -1
        check("4 images still go through", n_img == 4, repr(got)[:200])
    finally:
        await client.close()

    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
