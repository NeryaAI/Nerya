"""Session restore — .

Implementation notes:

* Walk the transcript looking for the most recent ``TodoWrite``
  ``tool_use`` and replay its arguments into app state.
* The same pass restores the plan-mode flag and recently-read file pins.

Why we need this in Nerya
-------------------------
The kernel rebuilds :class:`NativeToolDeps` (and therefore a *fresh*
:class:`TaskState`) on every workspace boot. Without restore, the
first turn after a CLI/dashboard restart loses every todo / plan-mode
flag the model carefully maintained — even though the transcript is
still on disk and the journal records every ``todo_write`` call.

The restore is intentionally read-only on the journal: we never
rewrite a turn's history. We just rebuild the *transient* state that
lives in ``TaskState`` so the next turn's system prompt + tool
behaviour matches what the previous one assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..tools.native.task import TaskState, TodoItem


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class RestoredState:
    """Read-only result of a restore pass.

    The kernel applies these onto its live :class:`TaskState`; the
    dataclass itself is just a transport for tests and the dashboard
    "session resume" panel.
    """

    todos: list[TodoItem] = field(default_factory=list)
    plan_mode: bool = False
    pending_plan: Optional[str] = None
    pending_plan_id: Optional[str] = None
    plan_decision: Optional[str] = None
    invoked_skills: list[str] = field(default_factory=list)
    last_turn_id: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return {
            "todos": [t.asdict() for t in self.todos],
            "plan_mode": self.plan_mode,
            "pending_plan": self.pending_plan,
            "pending_plan_id": self.pending_plan_id,
            "plan_decision": self.plan_decision,
            "invoked_skills": list(self.invoked_skills),
            "last_turn_id": self.last_turn_id,
        }


def restore_from_journal(
    paths: WorkspacePaths,
    *,
    session_id: str,
) -> RestoredState:
    """Rebuild :class:`TaskState`-shaped data for ``session_id``.

    Walks the agent journal in chronological order so the *latest*
    ``todo_write`` wins, the *latest* ``enter_plan_mode`` /
    ``exit_plan_mode`` / approval decision wins, and the invoked-skills
    set is the union over the whole session.

    Returns an empty :class:`RestoredState` when the journal is missing
    or the session never wrote anything (legitimate first-run case).
    """

    state = RestoredState()
    journal = paths.journal("agent")
    if not journal.exists():
        return state

    last_todo_payload: Optional[dict[str, Any]] = None
    last_plan_event: Optional[dict[str, Any]] = None
    invoked: list[str] = []
    last_turn_id: Optional[str] = None

    for row in jsonl.read_all(journal):
        if not isinstance(row, dict):
            continue
        if str(row.get("session_id") or "") != session_id:
            continue
        kind = row.get("kind")
        if kind in {"agent.turn.start", "agent.turn.end"}:
            tid = row.get("turn_id")
            if tid:
                last_turn_id = str(tid)
            continue
        # The kernel records a per-tool envelope as
        # ``agent.tool`` (kept in sync with the streaming bus). The
        # exact field names here mirror what ``_event_sink`` publishes.
        action = str(row.get("action") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if action == "todo_write" and bool(row.get("ok", True)):
            last_todo_payload = payload
        elif action == "enter_plan_mode" and bool(row.get("ok", True)):
            last_plan_event = {"kind": "enter", "payload": payload}
        elif action == "exit_plan_mode" and bool(row.get("ok", True)):
            last_plan_event = {"kind": "exit", "payload": payload}
        # invoked-skill list — track whichever skills we've seen
        skill_id = row.get("skill_id") or row.get("skill")
        if skill_id and skill_id not in invoked:
            invoked.append(str(skill_id))

    state.last_turn_id = last_turn_id
    state.invoked_skills = invoked

    if last_todo_payload:
        items = last_todo_payload.get("todos") or last_todo_payload.get("items")
        if isinstance(items, list):
            for i, raw in enumerate(items):
                if not isinstance(raw, dict):
                    continue
                state.todos.append(
                    TodoItem(
                        id=str(raw.get("id") or f"t{i+1}"),
                        content=str(raw.get("content") or "").strip(),
                        activeForm=str(
                            raw.get("activeForm")
                            or raw.get("active_form")
                            or ""
                        ).strip(),
                        status=str(raw.get("status") or "pending"),
                    )
                )

    if last_plan_event:
        if last_plan_event["kind"] == "enter":
            state.plan_mode = True
        elif last_plan_event["kind"] == "exit":
            payload = last_plan_event.get("payload") or {}
            state.plan_mode = bool(payload.get("plan_mode", True))
            plan = payload.get("plan")
            if isinstance(plan, str):
                state.pending_plan = plan
            pid = payload.get("plan_id") or payload.get("pending_plan_id")
            if pid:
                state.pending_plan_id = str(pid)

    return state


def apply_to_task_state(
    state: RestoredState, *, task_state: TaskState,
) -> None:
    """Mutate a live :class:`TaskState` to match ``state``.

    Idempotent — calling twice with the same ``state`` produces the
    same end-state. The kernel calls this once when a session is
    resumed (``session_id`` already exists on disk and the kernel was
    rebuilt fresh).
    """

    if state.todos:
        task_state.set_todos(list(state.todos))
    if state.plan_mode:
        task_state.set_plan_mode(True)
    if state.pending_plan:
        task_state.submit_plan(state.pending_plan)
    if state.plan_decision == "approved":
        task_state.resolve_plan(approved=True)
    elif state.plan_decision == "rejected":
        task_state.resolve_plan(approved=False)


__all__ = [
    "RestoredState",
    "apply_to_task_state",
    "restore_from_journal",
]
