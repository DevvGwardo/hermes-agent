"""Focused tests for todo-specific AIAgent callback behavior."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_assistant_msg(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _mock_tool_call(name="todo", arguments="{}", call_id="call_todo_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def test_invoke_tool_forwards_todo_prompt_args(agent):
    with patch("tools.todo_tool.todo_tool", return_value='{"ok":true}') as mock_todo:
        agent._invoke_tool(
            "todo",
            {"prompt": "Update the README and tests", "min_tasks": 2},
            "task-1",
        )
        mock_todo.assert_called_once_with(
            todos=None,
            prompt="Update the README and tests",
            min_tasks=2,
            merge=False,
            store=agent._todo_store,
        )


def test_sequential_tool_complete_callback_receives_todo_result(agent):
    agent.tool_complete_callback = MagicMock()
    tool_call = _mock_tool_call(
        name="todo",
        arguments=json.dumps({"prompt": "Update the README and tests"}),
        call_id="call_todo_1",
    )
    assistant_message = _mock_assistant_msg(content="", tool_calls=[tool_call])
    messages = []
    result = json.dumps({"todos": [], "cli": "HERMES TODO"})

    with patch("tools.todo_tool.todo_tool", return_value=result):
        agent._execute_tool_calls_sequential(assistant_message, messages, "task-1")

    agent.tool_complete_callback.assert_called_once_with(
        "call_todo_1",
        "todo",
        {"prompt": "Update the README and tests"},
        result,
    )
