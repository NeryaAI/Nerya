"""Native task / plan tools — ``todo_write``, ``enter_plan_mode``, ``exit_plan_mode``.

These tools manipulate *session-level* state (todo list, plan mode flag,
pending plan body). The state lives in :class:`TaskState`, owned by the
agent kernel and passed to the executor via ``NativeToolDeps``.

Task tools:

* ``todo_write``      — set the *whole* todo list. Mirrors Claude
  Code's TodoWriteTool. ``content`` + ``activeForm`` per item.
* ``enter_plan_mode`` — flip ``plan_mode = True``. While true, the
  permission engine refuses mutating tools.
* ``exit_plan_mode``  — submit a plan body for user approval and
  optionally exit plan mode after approval. Until approval lands, the
  tool returns a ``permission_pending`` result so the model knows to
  wait.

All three are ``auto_approve=True`` (no permission prompt).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..types import (
    ContextModifier,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)


# ---------------------------------------------------------------------------
# TaskState (shared across tools)
# ---------------------------------------------------------------------------


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
    """Session-level task state shared across tool calls.

    Note: the agent loop is single-threaded per session, but multiple
    concurrent reads (UI rendering) make a small lock cheap insurance.
    """

    todos: list[TodoItem] = field(default_factory=list)
    plan_mode: bool = False
    pending_plan: Optional[str] = None
    pending_plan_id: Optional[str] = None
    plan_decision: Optional[str] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    updated_at: float = field(default_factory=time.time)

    def set_todos(self, todos: list[TodoItem]) -> None:
        with self._lock:
            self.todos = list(todos)
            self.updated_at = time.time()

    def snapshot_todos(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.asdict() for t in self.todos]

    def set_plan_mode(self, on: bool) -> None:
        with self._lock:
            self.plan_mode = on
            self.updated_at = time.time()

    def submit_plan(self, body: str) -> str:
        with self._lock:
            self.pending_plan = body
            self.pending_plan_id = f"plan_{int(time.time() * 1000)}"
            self.plan_decision = None
            self.updated_at = time.time()
            return self.pending_plan_id

    def resolve_plan(self, *, approved: bool) -> None:
        with self._lock:
            self.plan_decision = "approved" if approved else "rejected"
            if approved:
                self.plan_mode = False
            self.updated_at = time.time()


# ---------------------------------------------------------------------------
# todo_write
# ---------------------------------------------------------------------------


def todo_write_handler(call: ToolCall, *, task_state: TaskState) -> ToolResult:
    args = call.arguments or {}
    raw = args.get("todos")
    if not isinstance(raw, list):
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="todo_write requires 'todos' as a list",
            ),
        )

    todos: list[TodoItem] = []
    in_progress = 0
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=f"todo[{i}] must be an object",
                ),
            )
        item_id = str(item.get("id") or f"t{i+1}")
        content = str(item.get("content") or "").strip()
        active_form = str(item.get("activeForm") or item.get("active_form") or "").strip()
        status = str(item.get("status") or "pending").lower()
        if not content:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=f"todo[{i}] missing 'content'",
                ),
            )
        if not active_form:
            active_form = content if content.endswith("ing") else f"Working on: {content}"
        if status not in _VALID_STATUSES:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=f"todo[{i}] invalid status {status!r}",
                ),
            )
        if status == "in_progress":
            in_progress += 1
        todos.append(
            TodoItem(id=item_id, content=content, activeForm=active_form, status=status)
        )

    if in_progress > 1:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="only one todo may be in_progress at a time",
            ),
        )

    task_state.set_todos(todos)

    summary_lines = ["# Todo list"]
    for t in todos:
        marker = {
            "pending": "[ ]",
            "in_progress": "[~]",
            "completed": "[x]",
            "cancelled": "[/]",
        }.get(t.status, "[?]")
        summary_lines.append(
            f"- {marker} {t.content} ({t.activeForm})"
            if t.status == "in_progress"
            else f"- {marker} {t.content}"
        )
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part("\n".join(summary_lines)),
            ToolResultPart.json_part({"todos": [t.asdict() for t in todos]}),
        ],
        context_modifiers=[
            ContextModifier(kind="todo_update", payload={"count": len(todos)})
        ],
    )


# ---------------------------------------------------------------------------
# enter_plan_mode
# ---------------------------------------------------------------------------


def enter_plan_mode_handler(call: ToolCall, *, task_state: TaskState) -> ToolResult:
    if task_state.plan_mode:
        return ToolResult(
            tool_use_id=call.id,
            name=call.name,
            content=[ToolResultPart.text_part("Already in plan mode.")],
        )
    task_state.set_plan_mode(True)
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(
                "Entered plan mode. Mutating tools (edit_file, write_file, "
                "run_shell) will be blocked until you call exit_plan_mode "
                "with a plan body and the user approves."
            )
        ],
        context_modifiers=[
            ContextModifier(kind="plan_mode", payload={"plan_mode": True})
        ],
    )


# ---------------------------------------------------------------------------
# exit_plan_mode
# ---------------------------------------------------------------------------


def exit_plan_mode_handler(
    call: ToolCall,
    *,
    task_state: TaskState,
    permission_mode: str = "",
) -> ToolResult:
    args = call.arguments or {}
    plan = args.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="exit_plan_mode requires 'plan' (markdown body)",
            ),
        )
    plan_id = task_state.submit_plan(plan)
    # Headless / unattended mode (env var set by the kernel boot path)
    # auto-resolves the plan immediately so the very next ``plan_status``
    # call within this same turn returns ``approved``. Without this the
    # model loops on ``plan_status`` forever, because nothing else
    # writes a decision when no operator is sitting in front of the
    # dashboard. ``default`` mode keeps the original wait-for-operator
    # behaviour.
    import os as _os
    auto_pmode = (
        permission_mode or _os.environ.get("NERYA_PERMISSION_MODE") or ""
    ).strip().lower()
    if auto_pmode in {"auto", "yolo"}:
        try:
            task_state.resolve_plan(approved=True)
        except Exception:
            pass
        return ToolResult(
            tool_use_id=call.id,
            name=call.name,
            content=[
                ToolResultPart.text_part(
                    "Plan auto-approved (headless / "
                    f"NERYA_PERMISSION_MODE={auto_pmode}). You may now resume "
                    "mutating tools without further approval."
                ),
                ToolResultPart.json_part(
                    {
                        "plan_id": plan_id,
                        "status": "approved",
                        "auto_approved": True,
                        "permission_mode": auto_pmode,
                    }
                ),
            ],
            context_modifiers=[
                ContextModifier(
                    kind="plan_mode",
                    payload={"plan_mode": False, "pending_plan_id": plan_id,
                             "decision": "approved"},
                )
            ],
        )
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(
                "Plan submitted for user approval.\n\n"
                "Tool will return when the user accepts or rejects. The plan body is rendered "
                "in the dashboard's approval pane. Poll with plan_status(plan_id=...) to check "
                "whether the operator has decided yet."
            ),
            ToolResultPart.json_part(
                {
                    "plan_id": plan_id,
                    "status": "pending_approval",
                    "plan": plan.strip()[:8000],
                    "next_action": (
                        "call plan_status with this plan_id to poll for the "
                        "operator's decision"
                    ),
                }
            ),
        ],
        context_modifiers=[
            ContextModifier(
                kind="plan_mode",
                payload={"plan_mode": True, "pending_plan_id": plan_id},
            )
        ],
    )


# ---------------------------------------------------------------------------
# plan_status — poll resolution of a pending plan submission
# ---------------------------------------------------------------------------


def plan_status_handler(call: ToolCall, *, task_state: TaskState) -> ToolResult:
    """Return the current state of plan_mode + the most recent plan submission.

    The model uses this between turns to decide whether the operator has
    accepted the plan body it submitted via ``exit_plan_mode``. A
    pending plan stays in ``permission_pending`` until the user resolves
    it; we accomplish that loop with two tools.
    """

    args = call.arguments or {}
    plan_id = (args.get("plan_id") or "").strip() or None
    with task_state._lock:
        current_id = task_state.pending_plan_id
        decision = task_state.plan_decision
        plan_mode = task_state.plan_mode
        body = task_state.pending_plan or ""
        updated_at = task_state.updated_at

    if plan_id is not None and current_id is not None and plan_id != current_id:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "plan_id": plan_id,
                "status": "stale",
                "current_plan_id": current_id,
                "plan_mode": plan_mode,
                "hint": (
                    "the operator superseded this plan with another submission; "
                    "submit a fresh exit_plan_mode if you still need approval"
                ),
            },
        )

    if current_id is None:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "plan_id": None,
                "status": "no_pending_plan",
                "plan_mode": plan_mode,
            },
        )

    status = (
        "approved"
        if decision == "approved"
        else "rejected"
        if decision == "rejected"
        else "pending_approval"
    )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "plan_id": current_id,
            "status": status,
            "plan_mode": plan_mode,
            "decision_at": updated_at if decision else None,
            "plan_excerpt": body[:1200],
            "hint": {
                "approved": (
                    "operator approved; you may now resume mutating tools"
                ),
                "rejected": (
                    "operator rejected; revise the plan body and resubmit "
                    "exit_plan_mode, or send_message asking what to change"
                ),
                "pending_approval": (
                    "still pending — wait, then call plan_status again "
                    "before issuing more tool calls"
                ),
            }[status],
        },
    )


__all__ = [
    "TaskState",
    "TodoItem",
    "enter_plan_mode_handler",
    "exit_plan_mode_handler",
    "plan_status_handler",
    "todo_write_handler",
]
