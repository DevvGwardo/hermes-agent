"""Tests for HermesCLI todo task-list rendering."""

import json
from unittest.mock import patch

from cli import HermesCLI


def _make_cli_stub():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._invalidate = lambda: None
    return cli_obj


def test_on_tool_complete_prints_todo_cli_block():
    cli_obj = _make_cli_stub()
    result = json.dumps({"cli": "+===+\n| HERMES TODO |\n+===+"})

    with patch("cli._cprint") as mock_cprint:
        cli_obj._on_tool_complete(
            "call_todo_1",
            "todo",
            {"prompt": "Update the README and tests"},
            result,
        )

    mock_cprint.assert_called_once_with("+===+\n| HERMES TODO |\n+===+")


def test_on_tool_complete_ignores_non_todo_results():
    cli_obj = _make_cli_stub()

    with patch("cli._cprint") as mock_cprint:
        cli_obj._on_tool_complete("call_1", "web_search", {}, json.dumps({"cli": "ignored"}))

    mock_cprint.assert_not_called()


def test_on_tool_complete_ignores_missing_cli_block():
    cli_obj = _make_cli_stub()

    with patch("cli._cprint") as mock_cprint:
        cli_obj._on_tool_complete("call_todo_1", "todo", {}, json.dumps({"todos": []}))

    mock_cprint.assert_not_called()
