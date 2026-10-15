"""
Per-run event buffer with reattach + resumption support.

Drop-in replacement for the asyncio.Queue-per-run model used by the
existing /v1/runs endpoints. The original design pops the queue when
the first SSE consumer disconnects, which makes it impossible for a
viewer to reconnect after a network blip or after Vercel's function
ceiling fires. The dashboard ships its chat through that path, so any
agent run longer than ~5 min got cut off.

This module solves that:

1.  ``RunBuffer`` keeps an append-only ``events`` list and an asyncio
    ``Condition``. Producers (the agent task) call ``append`` /
    ``mark_completed`` / ``mark_failed``; consumers (SSE writers) walk
    the list with their own cursor and ``await`` the condition for new
    events. Multiple concurrent consumers are supported.

2.  Event ids are monotonically increasing integers assigned at append
    time. Consumers can pass ``Last-Event-Id`` to resume after the
    last id they observed; the buffer just slices the list.

3.  Buffers stick around for ``RUN_GRACE_TTL_SECONDS`` after the run
    finishes so a refresh-after-completion still gets the full event
    history. ``RUN_MAX_TTL_SECONDS`` is a hard ceiling that protects
    against unbounded memory growth on a long-lived gateway. A
    background sweeper task drops expired buffers.

4.  Per-run event count is capped at ``RUN_MAX_EVENTS`` (default 20k).
    This is unrealistically high for normal use but bounds memory if
    something goes wrong (runaway tool loop, etc).

5.  ``RunBufferRegistry`` exposes lookup by ``run_id`` and the bounded
    set of currently-tracked runs. The api_server adapter holds one
    instance per process.

The module is intentionally standalone — no import of api_server —
so the install-runs-overlay script can drop it onto the VM as a single
file without touching unrelated parts of the gateway.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# How long a completed run sticks around so a late reconnect can still
# read the full event log. 1 hour matches the dashboard's stale-turn
# reaper window, giving the user generous headroom to come back to a
# tab they left open.
RUN_GRACE_TTL_SECONDS = _env_float("HERMES_RUN_GRACE_TTL_SECONDS", 3600.0)

# Hard ceiling on total run lifetime. A run that's been "active" for
# this long without progress is presumed wedged and gets reaped. The
# agent itself has its own iteration cap (HERMES_MAX_ITERATIONS); this
# is the buffer-side safety net.
RUN_MAX_TTL_SECONDS = _env_float("HERMES_RUN_MAX_TTL_SECONDS", 6 * 3600.0)

# Cap on retained events per run. Real runs emit O(50-500) events; 20k
# is hard to hit unless the agent loops pathologically.
RUN_MAX_EVENTS = _env_int("HERMES_RUN_MAX_EVENTS", 20_000)

# Concurrency cap on active dashboard runs. Higher than the original
# /v1/runs cap (10) because a single dashboard user can have several
# tabs open and we don't want a stale tab to lock out new turns.
RUN_MAX_CONCURRENT = _env_int("HERMES_RUN_MAX_CONCURRENT", 50)


class RunBuffer:
    """Append-only event log for a single agent run.

    Producer side (agent task):
        await buf.append({"event": "message.delta", "delta": "..."})
        await buf.mark_completed(final_text, usage)
        await buf.mark_cancelled(reason)  # /stop or task cancellation

    Consumer side (SSE writer):
        cursor = last_event_id  # int from Last-Event-Id, default 0
        while True:
            events, terminal = await buf.read_after(cursor, timeout=30)
            for ev in events:
                cursor = ev["id"]
                yield ev
            if terminal and cursor >= buf.last_id:
                return  # consumed everything
    """

    __slots__ = (
        "run_id",
        "events",
        "_next_id",
        "completed",
        "failed",
        "cancelled",
        "final_output",
        "error",
        "usage",
        "created_at",
        "completed_at",
        "last_activity_at",
        "_condition",
        "_agent_task",
        "_agent_ref",
        "_approval_session_key",
        "_approval_pending",
        "_approval_requested_at",
        "_stopping",
        "_prompt_broker",
    )

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: List[Dict[str, Any]] = []
        self._next_id: int = 1
        self.completed: bool = False
        self.failed: bool = False
        self.cancelled: bool = False
        self.final_output: Optional[str] = None
        self.error: Optional[str] = None
        self.usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        now = time.time()
        self.created_at: float = now
        self.completed_at: Optional[float] = None
        self.last_activity_at: float = now
        # Condition is bound to the running event loop; created lazily on
        # first await to avoid issues if RunBuffer is instantiated before
        # the loop starts (it isn't today, but it's cheap insurance).
        self._condition: Optional[asyncio.Condition] = None
        # Reference to the asyncio task running the agent + an opaque
        # handle to the agent itself so DELETE /v1/dashboard/runs/{id}
        # can interrupt it.
        self._agent_task: Optional[asyncio.Task] = None
        self._agent_ref: List[Any] = []
        # Set by the /stop route before it interrupts the agent: the run
        # wrapper checks it after the executor returns so an interrupted
        # result is recorded as cancelled, never completed (native parity).
        self._stopping: bool = False
        # Session key bound via tools.approval.set_current_session_key for this
        # run. The /v1/dashboard/runs/{id}/approval route resolves a pending
        # approval through this key instead of the native /v1/runs registries.
        self._approval_session_key: Optional[str] = None
        self._approval_pending: bool = False
        self._approval_requested_at: Optional[float] = None
        # Opt-in secret/clarify prompt broker (DevvGwardo fork only; see
        # dashboard_runs.py). None unless the client declared the capability.
        self._prompt_broker: Optional[Any] = None

    # -- producer side --------------------------------------------------

    def note_approval_requested(self) -> None:
        self._approval_pending = True
        self._approval_requested_at = time.time()

    def note_approval_resolved(self) -> None:
        self._approval_pending = False
        self._approval_requested_at = None

    def _ensure_condition(self) -> asyncio.Condition:
        if self._condition is None:
            self._condition = asyncio.Condition()
        return self._condition

    async def append(self, payload: Dict[str, Any]) -> int:
        """Append an event. Returns the assigned id.

        ``payload`` should already contain ``event`` (the SSE event
        name) and any event-specific fields. The buffer adds ``id``
        and ``timestamp``.
        """
        cond = self._ensure_condition()
        async with cond:
            if len(self.events) >= RUN_MAX_EVENTS:
                # Drop the oldest to keep memory bounded but never reuse
                # ids — clients use ids for resumption.
                self.events.pop(0)
            event_id = self._next_id
            self._next_id += 1
            evt = dict(payload)
            evt["id"] = event_id
            evt.setdefault("timestamp", time.time())
            self.events.append(evt)
            self.last_activity_at = evt["timestamp"]
            cond.notify_all()
            return event_id

    def append_threadsafe(self, loop: asyncio.AbstractEventLoop, payload: Dict[str, Any]) -> None:
        """Append from a worker thread (the agent runs blocking code in
        a thread pool). Schedules ``append`` on the loop.
        """
        async def _do() -> None:
            try:
                await self.append(payload)
            except Exception:
                logger.exception("[run_buffer %s] append failed", self.run_id)

        try:
            loop.call_soon_threadsafe(asyncio.create_task, _do())
        except RuntimeError:
            # Loop closed mid-run — best-effort, drop the event
            pass

    async def mark_completed(self, final_output: str, usage: Optional[Dict[str, int]] = None) -> None:
        cond = self._ensure_condition()
        async with cond:
            self.completed = True
            self.final_output = final_output
            if usage:
                self.usage = dict(usage)
            self.completed_at = time.time()
            self.last_activity_at = self.completed_at
            event_id = self._next_id
            self._next_id += 1
            self.events.append({
                "event": "run.completed",
                "id": event_id,
                "run_id": self.run_id,
                "timestamp": self.completed_at,
                "output": final_output,
                "usage": dict(self.usage),
            })
            if self._approval_pending:
                event_id = self._next_id
                self._next_id += 1
                self.events.append({
                    "event": "approval.responded",
                    "id": event_id,
                    "run_id": self.run_id,
                    "reason": "run_terminal:completed",
                    "timestamp": self.completed_at,
                })
                self._approval_pending = False
            cond.notify_all()

    async def mark_failed(self, error: str) -> None:
        cond = self._ensure_condition()
        async with cond:
            self.failed = True
            self.error = error
            self.completed_at = time.time()
            self.last_activity_at = self.completed_at
            event_id = self._next_id
            self._next_id += 1
            self.events.append({
                "event": "run.failed",
                "id": event_id,
                "run_id": self.run_id,
                "timestamp": self.completed_at,
                "error": error,
            })
            if self._approval_pending:
                event_id = self._next_id
                self._next_id += 1
                self.events.append({
                    "event": "approval.responded",
                    "id": event_id,
                    "run_id": self.run_id,
                    "reason": "run_terminal:failed",
                    "timestamp": self.completed_at,
                })
                self._approval_pending = False
            cond.notify_all()

    async def mark_cancelled(self, reason: str) -> None:
        """Terminal cancelled state (native run.cancelled parity).

        Guarded so a late task.cancel() landing inside the run wrapper
        cannot double-append a terminal event after /stop already
        marked the run cancelled.
        """
        cond = self._ensure_condition()
        async with cond:
            if self.terminal:
                return
            self.cancelled = True
            self.error = reason
            self.completed_at = time.time()
            self.last_activity_at = self.completed_at
            event_id = self._next_id
            self._next_id += 1
            self.events.append({
                "event": "run.cancelled",
                "id": event_id,
                "run_id": self.run_id,
                "timestamp": self.completed_at,
                "reason": reason,
            })
            if self._approval_pending:
                event_id = self._next_id
                self._next_id += 1
                self.events.append({
                    "event": "approval.responded",
                    "id": event_id,
                    "run_id": self.run_id,
                    "reason": "run_terminal:cancelled",
                    "timestamp": self.completed_at,
                })
                self._approval_pending = False
            cond.notify_all()

    @property
    def terminal(self) -> bool:
        return self.completed or self.failed or self.cancelled

    @property
    def last_id(self) -> int:
        # The id that would be assigned next minus one == id of the last
        # event currently in the buffer. 0 if nothing has been appended.
        return self._next_id - 1

    # -- consumer side --------------------------------------------------

    async def read_after(self, cursor: int, timeout: float) -> tuple[List[Dict[str, Any]], bool]:
        """Return all events with ``id > cursor``, or wait up to ``timeout``
        seconds for new ones to arrive.

        Returns ``(events, terminal)``. ``terminal`` is True iff the run
        has reached a terminal state and ``cursor`` is at or beyond the
        last event id (i.e. nothing more will ever come).

        If ``cursor`` is already past everything and the run is
        in-flight, blocks up to ``timeout`` and returns whatever
        accumulated (possibly an empty list — that's the keepalive
        signal for the SSE writer).
        """
        cond = self._ensure_condition()
        async with cond:
            # Fast path: already have new events.
            new_events = [e for e in self.events if e["id"] > cursor]
            if new_events:
                return new_events, self._terminal_locked(cursor + len(new_events))
            if self.terminal:
                return [], True
            try:
                await asyncio.wait_for(cond.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return [], False
            new_events = [e for e in self.events if e["id"] > cursor]
            return new_events, self._terminal_locked(
                cursor + len(new_events) if new_events else cursor
            )

    def _terminal_locked(self, cursor_after_emit: int) -> bool:
        # Caller already holds the condition lock.
        return self.terminal and cursor_after_emit >= self.last_id

    def snapshot(self) -> Dict[str, Any]:
        """JSON-serializable snapshot for /v1/dashboard/runs/{id}.

        Doesn't acquire the condition lock — the dict slots are
        atomic-enough for a snapshot view, and we don't want a producer
        to block on a polling client.
        """
        return {
            "run_id": self.run_id,
            "status": (
                "completed" if self.completed
                else "cancelled" if self.cancelled
                else "failed" if self.failed
                else "running"
            ),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "last_activity_at": self.last_activity_at,
            "last_event_id": self.last_id,
            "event_count": len(self.events),
            "final_output": self.final_output,
            "error": self.error,
            "usage": dict(self.usage),
            "pending_approval": self._approval_pending and not self.terminal,
        }

    # -- cancellation ---------------------------------------------------

    async def cancel(self, reason: str = "Run cancelled") -> None:
        agent = self._agent_ref[0] if self._agent_ref else None
        if agent is not None and hasattr(agent, "interrupt"):
            try:
                agent.interrupt(reason)
            except Exception:
                logger.exception("[run_buffer %s] interrupt failed", self.run_id)
        if self._agent_task is not None and not self._agent_task.done():
            self._agent_task.cancel()


class RunBufferRegistry:
    """Tracks all in-flight + recently-finished RunBuffers."""

    def __init__(self) -> None:
        self._buffers: Dict[str, RunBuffer] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None

    async def create(self, run_id: str) -> RunBuffer:
        async with self._lock:
            self._evict_expired_locked()
            active = sum(1 for b in self._buffers.values() if not b.terminal)
            if active >= RUN_MAX_CONCURRENT:
                raise RuntimeError(
                    f"Too many concurrent runs (max {RUN_MAX_CONCURRENT})"
                )
            buf = RunBuffer(run_id)
            self._buffers[run_id] = buf
            return buf

    def get(self, run_id: str) -> Optional[RunBuffer]:
        return self._buffers.get(run_id)

    async def list_active(self) -> List[str]:
        return [rid for rid, b in self._buffers.items() if not b.terminal]

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired: List[str] = []
        for rid, buf in self._buffers.items():
            if buf.terminal and buf.completed_at is not None:
                if now - buf.completed_at > RUN_GRACE_TTL_SECONDS:
                    expired.append(rid)
            elif now - buf.created_at > RUN_MAX_TTL_SECONDS:
                expired.append(rid)
        for rid in expired:
            self._buffers.pop(rid, None)

    async def sweep_loop(self) -> None:
        """Background task: periodically evict expired buffers."""
        while True:
            try:
                await asyncio.sleep(60)
                async with self._lock:
                    self._evict_expired_locked()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[run_buffer registry] sweep error")

    def start_sweeper(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._sweeper_task is not None:
            return
        self._sweeper_task = loop.create_task(self.sweep_loop())
