"""Opt-in interactive prompts (skill secret capture, clarify) on the ``/v1/runs`` stream.

A plain OpenAI-compatible client never answers a blocking prompt, so these only exist for a
run whose client declared support (``X-Hermes-Client-Capabilities: clarify,secret`` header or
a ``client_capabilities`` body field). The agent thread blocks on a per-run broker entry (like
approvals block on ``tools.approval``) until ``POST /v1/runs/{id}/secret`` or ``/clarify``
answers, the timeout expires (``<kind>.expire`` event), or the run stops/ends.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("gateway.platforms.api_server")

CLIENT_CAPABILITIES_HEADER = "X-Hermes-Client-Capabilities"
CLIENT_CAPABILITIES_FIELD = "client_capabilities"
SUPPORTED_CLIENT_CAPABILITIES = frozenset({"clarify", "secret"})
# Same budget as the TUI secret.request bridge (tui_gateway/server.py::_block default).
SECRET_PROMPT_TIMEOUT_S = 300.0


def parse_client_capabilities(header_value: Any, body_value: Any) -> frozenset[str]:
    """Union of the header (comma/space separated) and body (list or string) declarations,
    restricted to the prompt kinds this server can bridge; unknown tokens are ignored."""
    tokens: List[str] = []
    for raw in (header_value, body_value):
        if isinstance(raw, str):
            tokens += raw.replace(",", " ").split()
        elif isinstance(raw, (list, tuple)):
            tokens += [item for item in raw if isinstance(item, str)]
    return frozenset(t.strip().lower() for t in tokens) & SUPPORTED_CLIENT_CAPABILITIES


@dataclass
class _Pending:
    kind: str
    event: threading.Event = field(default_factory=threading.Event)
    answer: Optional[str] = None
    batch_qids: Optional[List[str]] = None
    batch_answers: Dict[str, str] = field(default_factory=dict)


class RunPromptBroker:
    """Pending prompts for ONE run. ``emit(name, fields)`` publishes a run event (called from
    the agent thread); ``set_pending(summary | None)`` mirrors the open prompt onto run status."""

    def __init__(self, capabilities: frozenset[str], emit: Callable[[str, Dict[str, Any]], None],
                 set_pending: Callable[[Optional[Dict[str, Any]]], None]):
        self.capabilities = capabilities
        self._emit = emit
        self._set_pending = set_pending
        self._lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._closed = False

    # -- agent-thread side -------------------------------------------------------------

    def _ask(self, kind: str, fields: Dict[str, Any], timeout: Optional[float],
             batch_qids: Optional[List[str]] = None) -> tuple[bool, _Pending]:
        """Publish ``<kind>.request`` and block; returns ``(answered, entry)``. ``timeout``
        None/<=0 waits until answered or the run closes."""
        request_id = uuid.uuid4().hex[:12]
        entry = _Pending(kind, batch_qids=list(batch_qids) if batch_qids else None)
        with self._lock:
            if self._closed:
                return False, entry
            self._pending[request_id] = entry
        self._set_pending({"kind": kind, "request_id": request_id})
        self._emit(f"{kind}.request", {"request_id": request_id, **fields})
        deadline = None if not timeout or timeout <= 0 else time.monotonic() + timeout
        try:
            from tools.environments.base import touch_activity_if_due
        except Exception:  # pragma: no cover - optional
            touch_activity_if_due = None
        activity = {"last_touch": time.monotonic(), "start": time.monotonic()}
        # 1s slices keep the inactivity heartbeat alive during a long human wait.
        while True:
            remaining = 1.0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0 or entry.event.wait(timeout=min(1.0, remaining)):
                break
            if touch_activity_if_due is not None:
                touch_activity_if_due(activity, f"waiting for {kind} response")
        with self._lock:
            self._pending.pop(request_id, None)
            answered = entry.event.is_set()
            closed = self._closed
        self._set_pending(None)
        if not answered and not closed:
            self._emit(f"{kind}.expire", {"request_id": request_id})
        return answered, entry

    def secret_capture(self, env_var: str, prompt: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """``tools.skills_tool`` secret-capture contract; saves exactly like the TUI bridge."""
        metadata = metadata or {}
        fields: Dict[str, Any] = {"env_var": env_var, "prompt": prompt}
        help_text = str(metadata.get("help") or "").strip()
        if help_text.startswith(("https://", "http://")):
            fields["url"] = help_text
        elif help_text:
            fields["help"] = help_text
        for wire, key in (("skill", "skill_name"), ("required_for", "required_for")):
            if metadata.get(key):
                fields[wire] = str(metadata[key])
        answered, entry = self._ask("secret", fields, SECRET_PROMPT_TIMEOUT_S)
        value = entry.answer if answered else None
        if not value:
            return {"success": True, "stored_as": env_var, "validated": False, "skipped": True, "message": "skipped"}
        from hermes_cli.config import save_env_value_secure
        return {**save_env_value_secure(env_var, value), "skipped": False, "message": "ok"}

    def clarify(self, question, choices, multi_select: bool = False, questions=None) -> str:
        """``clarify_tool`` callback (batch-capable); same wire/answer shapes as the TUI bridge."""
        from tools.clarify_gateway import get_clarify_timeout
        from tools.clarify_tool import TIMEOUT_RESPONSE
        timeout = float(get_clarify_timeout())
        if questions:
            wire = [{"qid": e["qid"], "question": e["question"], "choices": e["choices"],
                     "multi_select": bool(e["multi_select"])} for e in questions]
            answered, entry = self._ask("clarify", {"questions": wire}, timeout,
                                        batch_qids=[e["qid"] for e in questions])
            if entry.answer is not None:  # whole-batch answer (or cancel-all "")
                return entry.answer
            result: Dict[str, Any] = {"answers": dict(entry.batch_answers)}
            if not answered:
                result["timed_out"] = True
            return json.dumps(result, ensure_ascii=False)
        fields: Dict[str, Any] = {"question": question, "choices": choices}
        if multi_select:
            fields["multi_select"] = True
        answered, entry = self._ask("clarify", fields, timeout)
        return (entry.answer or "") if answered else TIMEOUT_RESPONSE

    # -- HTTP side ---------------------------------------------------------------------

    def resolve(self, kind: str, request_id: str, value: str,
                question_id: str = "") -> tuple[Optional[str], Optional[List[str]]]:
        """Answer one pending prompt -> ``(error_code, remaining_qids)``; ``error_code`` is
        None on success. A batch clarify with ``question_id`` locks that answer and
        releases the waiter once every question is answered."""
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is None or entry.kind != kind or entry.event.is_set():
                return "not_pending", None
            if entry.batch_qids is not None and question_id:
                if question_id not in entry.batch_qids:
                    return "unknown_question", None
                entry.batch_answers[question_id] = value
                remaining = [q for q in entry.batch_qids if q not in entry.batch_answers]
                if not remaining:
                    entry.event.set()
                return None, remaining
            entry.answer = value
            entry.event.set()
            return None, None

    def close(self) -> None:
        """Release every waiter with an empty answer; later prompts return immediately."""
        with self._lock:
            self._closed = True
            for entry in self._pending.values():
                if not entry.event.is_set():
                    entry.answer = ""
                    entry.event.set()
