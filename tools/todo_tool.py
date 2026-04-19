#!/usr/bin/env python3
"""
Todo Tool Module - Planning & Task Management

Compatibility wrapper around the standalone hermes-todo package.

Hermes should keep working even when its virtualenv still has an older
hermes-todo release installed. This shim forwards to the package when the
newer API is available and otherwise provides local fallbacks for:

- prompt-driven todo auto-launch
- terminal-ready CLI rendering
- extracting the CLI block from tool results
- the expanded tool schema with `prompt` and `min_tasks`
"""

from __future__ import annotations

import inspect
import json
import re
import textwrap
from typing import Any, Dict, List, Optional

import hermes_todo as _hermes_todo

from tools.registry import registry

_BaseTodoStore = _hermes_todo.TodoStore
_package_todo_tool = _hermes_todo.todo_tool
VALID_STATUSES = set(
    getattr(
        _hermes_todo,
        "VALID_STATUSES",
        {"pending", "in_progress", "completed", "cancelled"},
    )
)

_PACKAGE_SUPPORTS_PROMPT = {
    "prompt",
    "min_tasks",
}.issubset(inspect.signature(_package_todo_tool).parameters)

ACTION_VERBS = {
    "add",
    "audit",
    "build",
    "check",
    "clean",
    "compare",
    "create",
    "debug",
    "deploy",
    "design",
    "document",
    "draft",
    "edit",
    "explain",
    "extract",
    "fetch",
    "finalize",
    "find",
    "fix",
    "format",
    "generate",
    "implement",
    "improve",
    "inspect",
    "investigate",
    "launch",
    "list",
    "migrate",
    "move",
    "optimize",
    "organize",
    "plan",
    "polish",
    "prepare",
    "refactor",
    "release",
    "rename",
    "repair",
    "replace",
    "research",
    "review",
    "run",
    "scan",
    "ship",
    "show",
    "sort",
    "summarize",
    "test",
    "track",
    "triage",
    "update",
    "upgrade",
    "verify",
    "write",
}

LEADING_PREFIXES = (
    "please help me ",
    "please ",
    "can you ",
    "could you ",
    "would you ",
    "will you ",
    "help me ",
    "i need you to ",
    "i want you to ",
    "i need to ",
    "need to ",
    "lets ",
    "let's ",
)

LEADING_FILLERS = (
    "and ",
    "then ",
    "also ",
    "next ",
    "finally ",
)

LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|[0-9]+[.)])\s*")
WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")


class TodoStore(_BaseTodoStore):
    """Backward-compatible TodoStore with terminal rendering."""

    def format_for_cli(self, width: int = 72) -> str:
        base_method = getattr(_BaseTodoStore, "format_for_cli", None)
        if callable(base_method):
            return base_method(self, width=width)
        return _format_items_for_cli(self.read(), width=width)


def extract_tasks(prompt: str) -> List[str]:
    """Extract likely task clauses from a raw prompt."""
    text = _normalize_prompt(prompt)
    if not text:
        return []

    line_tasks = _extract_line_tasks(text)
    if len(line_tasks) >= 2:
        return line_tasks

    inline_tasks = _extract_inline_tasks(text)
    if len(inline_tasks) >= 2:
        return inline_tasks

    return []


def build_todos_from_tasks(tasks: List[str], activate_first: bool = True) -> List[Dict[str, str]]:
    """Convert task strings into normalized todo items."""
    todos: List[Dict[str, str]] = []
    for index, task in enumerate(tasks, start=1):
        todos.append(
            {
                "id": str(index),
                "content": task,
                "status": "in_progress" if activate_first and index == 1 else "pending",
            }
        )
    return todos


def todo_cli_from_result(result: str) -> Optional[str]:
    """Extract the CLI task-list block from a todo tool result."""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return None

    cli = payload.get("cli")
    return cli if isinstance(cli, str) and cli else None


def todo_tool(
    todos: Optional[List[Dict[str, Any]]] = None,
    prompt: Optional[str] = None,
    min_tasks: int = 2,
    merge: bool = False,
    store: Optional[TodoStore] = None,
) -> str:
    """
    Single entry point for the todo tool. Reads or writes depending on params.
    """
    if store is None:
        return json.dumps({"error": "TodoStore not initialized"}, ensure_ascii=False)

    if _PACKAGE_SUPPORTS_PROMPT:
        return _package_todo_tool(
            todos=todos,
            prompt=prompt,
            min_tasks=min_tasks,
            merge=merge,
            store=store,
        )

    had_items_before = store.has_items()
    prompt_tasks: List[str] = []
    launched_from_prompt = False

    if todos is not None:
        items = store.write(todos, merge)
    elif prompt is not None:
        prompt_tasks = extract_tasks(prompt)
        if len(prompt_tasks) >= max(1, min_tasks):
            items = store.write(build_todos_from_tasks(prompt_tasks), merge=False)
            launched_from_prompt = True
        else:
            items = store.read()
    else:
        items = store.read()

    auto_cleared = had_items_before and not items and todos is not None
    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    result = {
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
        "cli": store.format_for_cli(),
        "injection": getattr(store, "format_for_injection", lambda: None)(),
    }

    if prompt is not None:
        result["prompt_analysis"] = {
            "task_count": len(prompt_tasks),
            "launch_threshold": max(1, min_tasks),
            "launched": launched_from_prompt,
        }

    if auto_cleared:
        result["done"] = True

    return json.dumps(result, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


def _normalize_prompt(prompt: str) -> str:
    prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", prompt).strip()


def _normalize_task(task: str) -> str:
    task = re.sub(r"\s+", " ", task).strip(" \t\n\r,;:.")
    lowered = task.lower()

    changed = True
    while changed and task:
        changed = False
        for prefix in LEADING_PREFIXES:
            if lowered.startswith(prefix):
                task = task[len(prefix):].strip()
                lowered = task.lower()
                changed = True
        for filler in LEADING_FILLERS:
            if lowered.startswith(filler):
                task = task[len(filler):].strip()
                lowered = task.lower()
                changed = True

    return task.strip(" \t\n\r,;:.")


def _lead_verb(task: str) -> Optional[str]:
    words = WORD_RE.findall(_normalize_task(task).lower())
    for word in words[:3]:
        if word in ACTION_VERBS:
            return word
    return None


def _lead_verb_text(task: str) -> Optional[str]:
    words = WORD_RE.findall(_normalize_task(task))
    for word in words[:3]:
        if word.lower() in ACTION_VERBS:
            return word
    return None


def _is_short_phrase(task: str) -> bool:
    return len(WORD_RE.findall(task)) <= 6


def _looks_like_task(task: str) -> bool:
    if not task or not WORD_RE.search(task):
        return False
    if _lead_verb(task):
        return True
    return len(WORD_RE.findall(task)) >= 2


def _polish_task(task: str) -> str:
    task = _normalize_task(task)
    words = WORD_RE.findall(task)
    if not words:
        return task
    first = words[0]
    if first.lower() in ACTION_VERBS and first[:1].islower():
        return f"{first.capitalize()}{task[len(first):]}"
    return task


def _dedupe(tasks: List[str]) -> List[str]:
    seen = set()
    deduped = []
    for task in tasks:
        key = task.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
    return deduped


def _extract_line_tasks(text: str) -> List[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []

    marked_lines = [line for line in lines if LIST_MARKER_RE.match(line)]
    source_lines = marked_lines if len(marked_lines) >= 2 else lines

    tasks = []
    for line in source_lines:
        cleaned = _normalize_task(LIST_MARKER_RE.sub("", line))
        if cleaned and _looks_like_task(cleaned):
            tasks.append(_polish_task(cleaned))

    return _dedupe(tasks)


def _split_coordinated_clause(clause: str) -> List[str]:
    clause = _normalize_task(clause)
    if not clause:
        return []

    parts = re.split(r"\s+(?:and|&)\s+", clause)
    if len(parts) < 2:
        return [clause]

    lead_verb = _lead_verb(parts[0])
    lead_verb_text = _lead_verb_text(parts[0])
    if not lead_verb or not lead_verb_text:
        return [clause]

    tasks = []
    for index, part in enumerate(parts):
        cleaned = _normalize_task(part)
        if not cleaned:
            continue
        if index == 0 or _lead_verb(cleaned):
            tasks.append(cleaned)
            continue
        if _is_short_phrase(cleaned):
            tasks.append(f"{lead_verb_text} {cleaned}")
            continue
        tasks.append(cleaned)

    return tasks


def _extract_inline_tasks(text: str) -> List[str]:
    segments = [text]
    if "," in text:
        segments = [part for part in re.split(r",|\band then\b|\bthen\b|\bafter that\b|\bnext\b|\balso\b|\bplus\b", text, flags=re.IGNORECASE) if part.strip()]

    tasks: List[str] = []
    for segment in segments:
        tasks.extend(_split_coordinated_clause(segment))

    polished = [_polish_task(task) for task in tasks if _looks_like_task(task)]
    return _dedupe(polished)


def _format_items_for_cli(items: List[Dict[str, str]], width: int = 72) -> str:
    width = max(48, min(width, 120))
    inner_width = width - 4

    def border(char: str = "-") -> str:
        return f"+{char * (width - 2)}+"

    def row(text: str = "") -> str:
        return f"| {text.ljust(inner_width)} |"

    def wrapped_rows(prefix: str, content: str) -> List[str]:
        wrap_width = max(12, inner_width - len(prefix))
        chunks = textwrap.wrap(
            content,
            width=wrap_width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        rows = [row(f"{prefix}{chunks[0]}")]
        indent = " " * len(prefix)
        for chunk in chunks[1:]:
            rows.append(row(f"{indent}{chunk}"))
        return rows

    pending = sum(1 for item in items if item["status"] == "pending")
    in_progress = sum(1 for item in items if item["status"] == "in_progress")
    completed = sum(1 for item in items if item["status"] == "completed")
    cancelled = sum(1 for item in items if item["status"] == "cancelled")

    lines = [
        border("="),
        row("HERMES TODO"),
    ]

    if not items:
        lines.extend(
            [
                border(),
                row("No tasks yet."),
                row("Call todo_tool(prompt=...) to launch one."),
                border("="),
            ]
        )
        return "\n".join(lines)

    summary = (
        f"{len(items)} tasks | active {in_progress} | pending {pending} | "
        f"done {completed} | cancelled {cancelled}"
    )
    markers = {
        "completed": "[x]",
        "in_progress": "[>]",
        "pending": "[ ]",
        "cancelled": "[~]",
    }

    lines.extend([row(summary), border()])
    for item in items:
        prefix = f"{markers.get(item['status'], '[?]')} {item['id']}. "
        lines.extend(wrapped_rows(prefix, item["content"]))
    lines.append(border("="))
    return "\n".join(lines)


TODO_SCHEMA: Dict[str, Any] = {
    "name": "todo",
    "description": (
        "Manage your task list for the current session. Use for complex tasks "
        "with 2+ distinct tasks or steps. Call with no parameters to read the "
        "current list. Fastest path: pass the raw user request as 'prompt' and "
        "this tool will auto-launch a todo list whenever it detects at least "
        "two tasks.\n\n"
        "Writing:\n"
        "- Provide 'prompt' to auto-create a fresh list from the user request\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n"
        "- 'min_tasks' controls how many detected tasks are required before "
        "prompt auto-launch triggers (default 2)\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
        "cancel it and add a revised item.\n\n"
        "Always returns the full current list plus CLI-ready rendering."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Raw user request. Use this to auto-create a todo list from "
                    "the prompt when it contains multiple tasks."
                ),
            },
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique item identifier",
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "min_tasks": {
                "type": "integer",
                "description": (
                    "Minimum number of detected tasks required before a raw "
                    "prompt auto-launches a list."
                ),
                "default": 2,
                "minimum": 1,
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list. Ignored when "
                    "a raw prompt is used to auto-create a list."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


__all__ = [
    "TodoStore",
    "todo_tool",
    "todo_cli_from_result",
    "TODO_SCHEMA",
    "VALID_STATUSES",
]


registry.register(
    name="todo",
    toolset="todo",
    schema=TODO_SCHEMA,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"),
        prompt=args.get("prompt"),
        min_tasks=args.get("min_tasks", 2),
        merge=args.get("merge", False),
        store=kw.get("store"),
    ),
    check_fn=check_todo_requirements,
    emoji="📋",
)
