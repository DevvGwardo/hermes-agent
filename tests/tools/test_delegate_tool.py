"""Tests for delegate_task tool -- subagent todo integration."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import (
    DEFAULT_TOOLSETS,
    _run_single_child,
    delegate_task,
)


class TestDefaultToolsets:
    """Verify todo is in default toolsets for subagents."""

    def test_todo_in_defaults(self):
        assert "todo" in DEFAULT_TOOLSETS

    def test_defaults_contain_basics(self):
        for ts in ("terminal", "file", "web"):
            assert ts in DEFAULT_TOOLSETS


class TestInitialTodos:
    """Verify initial_todos are written to child store before conversation."""

    def _make_mock_child(self):
        """Create a mock child agent with a real-ish TodoStore."""
        from hermes_todo import TodoStore

        child = MagicMock()
        child._todo_store = TodoStore()
        child._delegate_saved_tool_names = []
        child.tool_progress_callback = None
        child.model = "test-model"
        child.session_prompt_tokens = 100
        child.session_completion_tokens = 50
        child.platform = None
        child.providers_allowed = None
        child.providers_ignored = None
        child.providers_order = None
        child.provider_sort = None
        child.api_key = None
        child.base_url = "http://localhost"
        child.provider = None
        child.api_mode = None
        child.acp_command = None
        child.acp_args = []
        child.max_tokens = None
        child.reasoning_config = None
        child.prefill_messages = None
        child.max_iterations = 50
        child._active_children = []
        child._active_children_lock = None
        child._session_db = None
        child._delegate_depth = 0
        return child

    def test_initial_todos_written_to_store(self):
        child = self._make_mock_child()
        initial = [
            {"id": "1", "content": "Step one", "status": "pending"},
            {"id": "2", "content": "Step two", "status": "pending"},
        ]

        # Mock run_conversation to avoid actual API calls
        child.run_conversation.return_value = {
            "final_response": "Done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        result = _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=MagicMock(),
            initial_todos=initial,
        )

        # Verify todos were written to child's store
        items = child._todo_store.read()
        assert len(items) == 2
        assert items[0]["id"] == "1"
        assert items[0]["content"] == "Step one"
        assert items[1]["id"] == "2"

    def test_no_initial_todos_leaves_empty_store(self):
        child = self._make_mock_child()
        child.run_conversation.return_value = {
            "final_response": "Done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=MagicMock(),
            initial_todos=None,
        )

        assert not child._todo_store.has_items()

    def test_empty_initial_todos_list_leaves_empty_store(self):
        child = self._make_mock_child()
        child.run_conversation.return_value = {
            "final_response": "Done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=MagicMock(),
            initial_todos=[],
        )

        assert not child._todo_store.has_items()


class TestTodoSummaryInResult:
    """Verify todo summary is appended to child's final_response."""

    def _make_mock_child(self):
        from hermes_todo import TodoStore

        child = MagicMock()
        child._todo_store = TodoStore()
        child._delegate_saved_tool_names = []
        child.tool_progress_callback = None
        child.model = "test-model"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.platform = None
        child.providers_allowed = None
        child.providers_ignored = None
        child.providers_order = None
        child.provider_sort = None
        child.api_key = None
        child.base_url = "http://localhost"
        child.provider = None
        child.api_mode = None
        child.acp_command = None
        child.acp_args = []
        child.max_tokens = None
        child.reasoning_config = None
        child.prefill_messages = None
        child.max_iterations = 50
        child._active_children = []
        child._active_children_lock = None
        child._session_db = None
        child._delegate_depth = 0
        return child

    def test_todo_summary_appended_when_store_has_items(self):
        child = self._make_mock_child()
        # Simulate subagent writing todos during its conversation
        child._todo_store.write([
            {"id": "1", "content": "Analyze code", "status": "completed"},
            {"id": "2", "content": "Write tests", "status": "in_progress"},
        ])
        child.run_conversation.return_value = {
            "final_response": "I analyzed the code and started writing tests.",
            "completed": True,
            "interrupted": False,
            "api_calls": 3,
            "messages": [],
        }

        result = _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=MagicMock(),
        )

        summary = result["summary"]
        assert "--- Task Tracker ---" in summary
        assert "[x] 1. Analyze code" in summary
        assert "[>] 2. Write tests" in summary

    def test_no_todo_summary_when_store_empty(self):
        child = self._make_mock_child()
        child.run_conversation.return_value = {
            "final_response": "Done with no todos.",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        result = _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=MagicMock(),
        )

        assert "--- Task Tracker ---" not in result["summary"]
        assert result["summary"] == "Done with no todos."

    def test_todo_summary_with_auto_cleared_store(self):
        """When todos auto-clear (all completed), store is empty so no summary."""
        child = self._make_mock_child()
        # Write todos that all get completed -> auto_clear fires
        child._todo_store.write([
            {"id": "1", "content": "Done task", "status": "completed"},
        ])
        # After auto_clear, store is empty
        assert not child._todo_store.has_items()

        child.run_conversation.return_value = {
            "final_response": "All done.",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        result = _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=MagicMock(),
        )

        # No todo summary since store auto-cleared
        assert "--- Task Tracker ---" not in result["summary"]
