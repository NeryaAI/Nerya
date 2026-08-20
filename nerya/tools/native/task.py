"""Session todo tracking for the workspace-native agent loop.

Planning is model behaviour, not a second authorization system. Nerya exposes
``todo_write`` to keep multi-step work visible across compaction, while actual
side effects remain governed by :class:`PermissionEngine` and domain safety
gates such as trading risk/approval. The former enter/exit/status plan tools
only maintained an isolated flag and created approval polling loops, so they
were removed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..tool_errors import schema_validation_result
from ..types import (
    ContextModifier,
    ToolCall,
    ToolResult,
    ToolResultPart,
)


_VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


@dataclass
class TodoItem:
    id: str
    content: str
    activeForm: str = ""
    status: str = "pending"

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "activeForm": self.activeForm,
            "status": self.status,
        }


@dataclass
class TaskState:
    """Session-level todo state shared across tool calls and UI reads."""

    todos: list[TodoItem] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    updated_at: float = field(default_factory=time.time)

    def set_todos(self, todos: list[TodoItem]) -> None:
        with self._lock:
            self.todos = list(todos)
            self.updated_at = time.time()

    def snapshot_todos(self) -> list[dict[str, Any]]:
        with self._lock:
            return [todo.asdict() for todo in self.todos]


def format_for_injection(task_state: TaskState) -> str:
    """Render unfinished todo state for prompt injection after compaction."""

    rows = [
        item
        for item in task_state.snapshot_todos()
        if item.get("status") in {"pending", "in_progress"}
    ]
    if not rows:
        return ""
    lines = ["# Task Progress", "Unfinished work from the current session:"]
    for item in rows:
        status = item.get("status") or "pending"
        content = item.get("activeForm") or item.get("content") or item.get("id")
        lines.append(f"- {status}: {content}")
    return "\n".join(lines)


def todo_write_handler(call: ToolCall, *, task_state: TaskState) -> ToolResult:
    args = call.arguments or {}
    raw = args.get("todos")
    if not isinstance(raw, list):
        return schema_validation_result(call, "todo_write requires 'todos' as a list")

    todos: list[TodoItem] = []
    in_progress = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return schema_validation_result(
                call,
                f"todo[{index}] must be an object",
            )
        item_id = str(item.get("id") or f"t{index + 1}")
        content = str(item.get("content") or "").strip()
        active_form = str(
            item.get("activeForm") or item.get("active_form") or ""
        ).strip()
        status = str(item.get("status") or "pending").lower()
        if not content:
            return schema_validation_result(
                call,
                f"todo[{index}] missing 'content'",
            )
        if not active_form:
            active_form = (
                content if content.endswith("ing") else f"Working on: {content}"
            )
        if status not in _VALID_STATUSES:
            return schema_validation_result(
                call,
                f"todo[{index}] invalid status {status!r}",
            )
        if status == "in_progress":
            in_progress += 1
        todos.append(
            TodoItem(
                id=item_id,
                content=content,
                activeForm=active_form,
                status=status,
            )
        )

    if in_progress > 1:
        return schema_validation_result(
            call,
            "only one todo may be in_progress at a time",
        )

    task_state.set_todos(todos)
    markers = {
        "pending": "[ ]",
        "in_progress": "[~]",
        "completed": "[x]",
        "cancelled": "[/]",
    }
    summary_lines = ["# Todo list"]
    for todo in todos:
        marker = markers.get(todo.status, "[?]")
        summary_lines.append(
            f"- {marker} {todo.content} ({todo.activeForm})"
            if todo.status == "in_progress"
            else f"- {marker} {todo.content}"
        )
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part("\n".join(summary_lines)),
            ToolResultPart.json_part({"todos": [todo.asdict() for todo in todos]}),
        ],
        context_modifiers=[
            ContextModifier(kind="todo_update", payload={"count": len(todos)})
        ],
    )


__all__ = [
    "TaskState",
    "TodoItem",
    "format_for_injection",
    "todo_write_handler",
]
