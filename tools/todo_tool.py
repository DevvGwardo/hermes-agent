#!/usr/bin/env python3
"""
Todo Tool Module - Planning & Task Management

Thin wrapper around hermes-todo package. The core implementation
(TodoStore, todo_tool, TODO_SCHEMA) lives in the standalone
hermes-todo package: https://github.com/DevvGwardo/hermes-todo
"""

from hermes_todo import TodoStore, todo_tool, TODO_SCHEMA, VALID_STATUSES
from tools.registry import registry

# Re-export everything for backwards compatibility
__all__ = ["TodoStore", "todo_tool", "TODO_SCHEMA", "VALID_STATUSES"]


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


# --- Registry ---
registry.register(
    name="todo",
    toolset="todo",
    schema=TODO_SCHEMA,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"), merge=args.get("merge", False), store=kw.get("store")
    ),
    check_fn=check_todo_requirements,
    emoji="📋",
)
