"""Fork-owned mid-turn agent eviction safety for gateway session execution.

``gateway.session_execution`` stays close to upstream: its two one-line hooks delegate here,
where a cached agent displaced by a config change while its turn is still running is parked
until that turn ends instead of being torn down under a live request.
"""
from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.session_execution import GatewaySessionAgentMixin

# Defaults; the mixin's class attributes (when present) win so tests can shorten the poll.
_MIDTURN_RELEASE_POLL_S = 5.0
_MIDTURN_RELEASE_MAX_WAIT_S = 3 * 3600.0


def classify_evicted_agent(mixin: "GatewaySessionAgentMixin", out: Any) -> None:
    """Pop the displaced cache entry and record it on ``out`` for release.

    A mid-turn agent is never released from the turn's cache path: it goes on
    ``out.evicted_running`` for ``release_evicted_agents`` to pick up once its
    running-set entry clears. Everything else goes on ``out.evicted`` for the
    normal off-lock soft release.
    """
    evicted = mixin._pop_cached_agent_for_eviction()
    if evicted is not None and id(evicted) in mixin._runner._running_agent_ids():
        out.evicted_running = evicted
    else:
        out.evicted = evicted


def release_evicted_agents(mixin: "GatewaySessionAgentMixin", found: Any) -> None:
    """Release the evicted agents recorded by ``classify_evicted_agent``.

    Immediate evictions use the mixin's normal off-lock release. A mid-turn eviction is
    handed to a daemon thread that waits (off the gateway loop) until the agent leaves
    the running set, then soft-releases it instead of leaking its client pool to the GC.
    Gives up after the wait cap.
    """
    if found.evicted is not None:
        mixin._release_evicted_agent(found.evicted)
    agent = getattr(found, "evicted_running", None)
    if agent is None:
        return
    runner = mixin._runner

    def _wait_then_release(a) -> None:
        poll_s = getattr(mixin, "_MIDTURN_RELEASE_POLL_S", _MIDTURN_RELEASE_POLL_S)
        max_wait_s = getattr(mixin, "_MIDTURN_RELEASE_MAX_WAIT_S", _MIDTURN_RELEASE_MAX_WAIT_S)
        deadline = time.monotonic() + max_wait_s
        while id(a) in runner._running_agent_ids():
            if time.monotonic() >= deadline:
                return
            time.sleep(poll_s)
        runner._release_evicted_agent_soft(a)

    with suppress(Exception):
        runner._spawn_release_thread(
            _wait_then_release, (agent,), f"agent-midturn-evict-{str(mixin._ctx.session_key)[:24]}",
            inline_fallback=False, session_key=mixin._ctx.session_key,
        )
