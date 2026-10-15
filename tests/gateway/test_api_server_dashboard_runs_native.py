"""The Nub dashboard runs API is native on the maiavm fork.

hermes-deploy used to patch /v1/dashboard/runs into api_server.py on every VM
and re-apply it on each gateway start. The fork now ships it; hermes-deploy
detects the ``maiavm-native-dashboard-runs`` marker and stops patching. These
tests pin that contract: the marker, the routes on a real connect(), and the
``_dash_register`` name older repair scripts use to skip their patch.
"""

import inspect
import socket

import aiohttp
import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter

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
