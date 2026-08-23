"""brain-mcp MemoryProvider — cross-session memory shared with Claude Code and swarm agents.

Uses direct SQLite access to brain-mcp's database (WAL mode, concurrent-safe).
No MCP calls needed — faster and works even if the MCP server is down.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_BRAIN_MCP_PATH = str(Path.home() / "brain-mcp")
_DEFAULT_DB_PATH = str(Path.home() / ".claude" / "brain" / "brain.db")


def _get_brain_db_class():
    """Import BrainDB from brain-mcp's Python package."""
    if _BRAIN_MCP_PATH not in sys.path:
        sys.path.insert(0, _BRAIN_MCP_PATH)
    from hermes.db import BrainDB
    return BrainDB


class BrainMCPMemoryProvider(MemoryProvider):
    """Persistent cross-session memory via brain-mcp's SQLite database.

    Automatically injects relevant memories and context ledger entries
    before each LLM turn, and logs activity back to the brain database.
    Shares the same DB that Claude Code agents and brain swarm agents use.
    """

    def __init__(self):
        self._db: Any = None
        self._db_path = os.environ.get("BRAIN_DB_PATH", _DEFAULT_DB_PATH)
        self._session_id = str(uuid.uuid4())
        self._agent_name = "hermes"
        self._turn_count = 0
        self._room: Optional[str] = None
        self._agent_context: str = "primary"
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "brain-mcp"

    @property
    def room(self) -> str:
        if self._room is None:
            self._room = os.getcwd()
        return self._room

    def is_available(self) -> bool:
        if not os.path.exists(self._db_path):
            return False
        try:
            _get_brain_db_class()
            return True
        except (ImportError, Exception):
            return False

    def _get_db(self):
        if self._db is None:
            BrainDB = _get_brain_db_class()
            self._db = BrainDB(self._db_path)
        return self._db

    # -- Core lifecycle -------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._room = os.getcwd()
        self._agent_context = kwargs.get("agent_context", "primary")

        if self._agent_context != "primary":
            return

        try:
            db = self._get_db()
            db.register_session(
                name=self._agent_name,
                room=self.room,
                session_id=self._session_id,
            )
            db.push_context(
                room=self.room,
                session_id=self._session_id,
                agent_name=self._agent_name,
                entry_type="action",
                summary="Hermes session started",
                tags=["session-start"],
            )
        except Exception as exc:
            logger.debug("brain-mcp initialize failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Brain MCP tools already registered via mcp_tool.py — no duplication
        return []

    def system_prompt_block(self) -> str:
        if not os.path.exists(self._db_path):
            return ""
        return (
            "## Brain Memory (brain-mcp)\n"
            "You have persistent cross-session memory via brain-mcp. "
            "Knowledge stored with brain_remember persists across sessions and is shared "
            "with Claude Code agents and swarm agents working in this project. "
            "Use mcp_brain_brain_remember to store important discoveries, and "
            "mcp_brain_brain_recall to search for past knowledge. "
            "Context from previous sessions is automatically injected below.\n"
        )

    # -- Prefetch: inject brain memories before each turn --------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._agent_context != "primary":
            return ""
        try:
            db = self._get_db()
            room = self.room
            parts = []

            # 1. Top memories for this room (by access frequency)
            memories = db.recall_memory(room=room, limit=15)
            if memories:
                lines = []
                for m in memories:
                    lines.append(f"- **{m.key}** [{m.category}]: {m.content[:200]}")
                parts.append("### Persistent Memory\n" + "\n".join(lines))

            # 2. Recent context ledger entries
            ledger = db.get_context(room=room, limit=10)
            if ledger:
                lines = []
                for entry in reversed(ledger):  # chronological order
                    prefix = {
                        "action": "Did", "discovery": "Found",
                        "decision": "Decided", "error": "Error",
                        "file_change": "Changed", "checkpoint": "Saved",
                    }.get(entry.get("entry_type", ""), entry.get("entry_type", ""))
                    line = f"- [{prefix}] {entry['summary']}"
                    fp = entry.get("file_path")
                    if fp:
                        line += f" ({fp})"
                    lines.append(line)
                parts.append("### Recent Activity\n" + "\n".join(lines))

            # 3. Latest checkpoint summary
            cp = db.restore_checkpoint(room=room)
            if cp:
                try:
                    state = json.loads(cp.get("state", "{}"))
                    summary = state.get("progress_summary", "")
                    if summary:
                        parts.append(f"### Last Checkpoint\n{summary}")
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

            if not parts:
                return ""

            return (
                "## Brain Memory Context (from previous sessions)\n\n"
                + "\n\n".join(parts)
            )

        except Exception as exc:
            logger.debug("brain-mcp prefetch failed: %s", exc)
            return ""

    # -- Sync: log turn to context ledger ------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        if self._agent_context != "primary":
            return

        self._turn_count += 1

        # Log every 3rd turn + first turn to avoid ledger spam
        if self._turn_count > 1 and self._turn_count % 3 != 0:
            return

        def _sync():
            try:
                db = self._get_db()
                summary = f"Turn {self._turn_count}"
                if user_content:
                    summary += f": {user_content[:100]}"
                with self._lock:
                    db.push_context(
                        room=self.room,
                        session_id=self._session_id,
                        agent_name=self._agent_name,
                        entry_type="action",
                        summary=summary,
                        tags=["hermes-turn"],
                    )
            except Exception as exc:
                logger.debug("brain-mcp sync_turn failed: %s", exc)

        threading.Thread(target=_sync, daemon=True, name="brain-sync").start()

    # -- Optional hooks -------------------------------------------------------

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Save a checkpoint before context compression discards messages."""
        if self._agent_context != "primary":
            return ""

        try:
            db = self._get_db()

            # Extract file paths mentioned in messages being compressed
            files_touched = set()
            decisions = []
            for msg in messages[-20:]:  # scan last 20 messages
                content = msg.get("content", "")
                if isinstance(content, str):
                    if "decided" in content.lower() or "decision" in content.lower():
                        decisions.append(content[:200])

            with self._lock:
                db.save_checkpoint(
                    room=self.room,
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    state={
                        "current_task": "context compressed",
                        "files_touched": list(files_touched),
                        "decisions": decisions[:5],
                        "progress_summary": f"Hermes session (turn {self._turn_count}, pre-compression checkpoint)",
                        "blockers": [],
                        "next_steps": [],
                    },
                )
        except Exception as exc:
            logger.debug("brain-mcp on_pre_compress failed: %s", exc)
        return ""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._agent_context != "primary":
            return

        try:
            db = self._get_db()
            with self._lock:
                db.save_checkpoint(
                    room=self.room,
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    state={
                        "current_task": "session ended",
                        "files_touched": [],
                        "decisions": [],
                        "progress_summary": f"Hermes session completed ({self._turn_count} turns)",
                        "blockers": [],
                        "next_steps": [],
                    },
                )
                db.push_context(
                    room=self.room,
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    entry_type="action",
                    summary=f"Hermes session ended ({self._turn_count} turns)",
                    tags=["session-end"],
                )
        except Exception as exc:
            logger.debug("brain-mcp on_session_end failed: %s", exc)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror MEMORY.md/USER.md writes to brain memory table."""
        if action != "add" or not content.strip():
            return
        if self._agent_context != "primary":
            return

        def _mirror():
            try:
                db = self._get_db()
                # Create a stable key from content prefix
                key = f"hermes_{target}_{content[:50].strip().replace(' ', '_').lower()}"
                with self._lock:
                    db.store_memory(
                        room=self.room,
                        key=key,
                        content=content,
                        category=f"hermes-{target}",
                        created_by=self._session_id,
                        created_by_name=self._agent_name,
                    )
            except Exception as exc:
                logger.debug("brain-mcp on_memory_write failed: %s", exc)

        threading.Thread(target=_mirror, daemon=True, name="brain-memwrite").start()

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs
    ) -> None:
        if self._agent_context != "primary":
            return

        def _log():
            try:
                db = self._get_db()
                summary = f"Delegation: {task[:150]}"
                detail = result[:500] if result else None
                with self._lock:
                    db.push_context(
                        room=self.room,
                        session_id=self._session_id,
                        agent_name=self._agent_name,
                        entry_type="action",
                        summary=summary,
                        detail=detail,
                        tags=["delegation", "subagent"],
                    )
            except Exception as exc:
                logger.debug("brain-mcp on_delegation failed: %s", exc)

        threading.Thread(target=_log, daemon=True, name="brain-deleg").start()

    def shutdown(self) -> None:
        try:
            if self._db:
                self._db.remove_session(self._session_id)
                self._db.close()
                self._db = None
        except Exception as exc:
            logger.debug("brain-mcp shutdown failed: %s", exc)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register brain-mcp memory provider with hermes."""
    provider = BrainMCPMemoryProvider()
    ctx.register_memory_provider(provider)
