"""Execution-state surface router for agent turns.

The agent loop already records raw blocks and activity events. This
module projects those facts into distinct surfaces so UI, logs, resume,
and tests do not infer execution state from final prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


SURFACE_ORDER: tuple[str, ...] = (
    "approval_plan",
    "execution_todo",
    "tool_progress",
    "task_progress",
    "status",
    "resume",
)

_TOOL_TODO_ACTIONS = frozenset({
    "todo_write",
    "todo_update",
    "todo_list",
    "task_update",
})


@dataclass
class ExecutionStateItem:
    source: str
    surface: str
    audience: str
    lifetime: str
    turn_id: str
    item_id: str
    status: str
    message: str
    parent_id: str | None = None
    tool: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "surface": self.surface,
            "audience": self.audience,
            "lifetime": self.lifetime,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "status": self.status,
            "message": self.message,
        }
        if self.parent_id:
            out["parent_id"] = self.parent_id
        if self.tool:
            out["tool"] = self.tool
        if self.payload:
            out["payload"] = dict(self.payload)
        return out


def build_execution_state(
    *,
    turn_id: str,
    blocks: Iterable[dict[str, Any]] | None = None,
    activity_events: Iterable[dict[str, Any]] | None = None,
    stop_reason: str | None = None,
    transition_reason: str | None = None,
    aborted: bool | None = None,
    abort_reason: str | None = None,
) -> dict[str, Any]:
    """Project turn facts into first-class execution-state surfaces."""

    items: list[dict[str, Any]] = []

    def add(item: ExecutionStateItem) -> None:
        if item.surface not in SURFACE_ORDER:
            return
        items.append(item.asdict())

    for index, env in enumerate(blocks or ()):
        block = _unwrap_block(env)
        if not block:
            continue
        kind = str(block.get("kind") or env.get("kind") or "").strip()
        call_id = str(
            block.get("call_id")
            or block.get("tool_use_id")
            or block.get("id")
            or block.get("block_id")
            or f"block_{index}"
        )
        block_turn_id = str(env.get("turn_id") or turn_id or "")

        if kind == "approval_request":
            approval_id = str(block.get("approval_id") or call_id)
            add(
                ExecutionStateItem(
                    source="turn.block",
                    surface="approval_plan",
                    audience="operator",
                    lifetime="until_resolved",
                    turn_id=block_turn_id,
                    item_id=approval_id,
                    parent_id=call_id if call_id != approval_id else None,
                    status=_approval_status(block),
                    message=_approval_message(block),
                    payload=_pick(block, ("approval_id", "status")),
                )
            )
            continue

        if kind in {"tool_use", "tool_result"}:
            action = str(block.get("action") or "").strip()
            skill = str(block.get("skill_id") or "").strip()
            tool = ".".join(part for part in (skill, action) if part)
            is_result = kind == "tool_result"
            status = _tool_result_status(block) if is_result else "started"
            surface = "execution_todo" if action in _TOOL_TODO_ACTIONS else "tool_progress"
            add(
                ExecutionStateItem(
                    source="turn.block",
                    surface=surface,
                    audience="model" if surface == "execution_todo" else "operator",
                    lifetime="session" if surface == "execution_todo" else "turn",
                    turn_id=block_turn_id,
                    item_id=f"{call_id}:{kind}",
                    parent_id=call_id,
                    tool=tool or action or None,
                    status=status,
                    message=_tool_message(tool or action or "tool", status, block),
                    payload=_pick(block, ("action", "ok", "error_kind", "elapsed_ms", "recovery")),
                )
            )
            continue

        if kind in {"todo", "todo_item", "execution_todo"}:
            todo_id = str(block.get("id") or block.get("todo_id") or call_id)
            add(
                ExecutionStateItem(
                    source="turn.block",
                    surface="execution_todo",
                    audience="model",
                    lifetime="session",
                    turn_id=block_turn_id,
                    item_id=todo_id,
                    status=str(block.get("status") or "observed"),
                    message=_short_text(block.get("content") or block.get("text") or kind),
                    payload=_pick(block, ("id", "status")),
                )
            )

    for index, event in enumerate(activity_events or ()):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "").strip()
        if not kind:
            continue
        event_turn_id = str(event.get("turn_id") or turn_id or "")
        surface = _activity_surface(kind)
        if surface == "approval_plan":
            audience = "operator"
            lifetime = "until_resolved"
        elif surface == "task_progress":
            audience = "operator"
            lifetime = "task"
        else:
            audience = "operator"
            lifetime = "turn"
        parent_id = _first_str(event, ("parent_id", "team_run_id", "task_id"))
        item_id = _first_str(
            event,
            (
                "event_id",
                "team_task_id",
                "tool_call_id",
                "call_id",
                "approval_id",
                "seq",
            ),
        ) or f"activity_{index}"
        add(
            ExecutionStateItem(
                source="activity.event",
                surface=surface,
                audience=audience,
                lifetime=lifetime,
                turn_id=event_turn_id,
                item_id=str(item_id),
                parent_id=parent_id,
                status=_activity_status(kind, event),
                message=_activity_message(kind, event),
                payload=_pick(
                    event,
                    (
                        "kind",
                        "ok",
                        "status",
                        "subagent",
                        "team_run_id",
                        "team_task_id",
                    ),
                ),
            )
        )

    if stop_reason or transition_reason or aborted is not None or abort_reason:
        add(
            ExecutionStateItem(
                source="turn.budget",
                surface="status",
                audience="user",
                lifetime="turn",
                turn_id=turn_id,
                item_id=f"{turn_id}:status",
                status="aborted" if aborted else "stopped",
                message=_status_message(
                    stop_reason=stop_reason,
                    transition_reason=transition_reason,
                    abort_reason=abort_reason,
                ),
                payload={
                    k: v
                    for k, v in {
                        "stop_reason": stop_reason,
                        "transition_reason": transition_reason,
                        "aborted": aborted,
                        "abort_reason": abort_reason,
                    }.items()
                    if v is not None
                },
            )
        )

    surfaces: dict[str, list[dict[str, Any]]] = {name: [] for name in SURFACE_ORDER}
    for item in items:
        surfaces[item["surface"]].append(item)
    return {
        "version": 1,
        "items": items,
        "surfaces": surfaces,
        "counters": {name: len(surfaces[name]) for name in SURFACE_ORDER},
    }


def _unwrap_block(env: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(env, dict):
        return {}
    block = env.get("block")
    if isinstance(block, dict):
        return block
    return env


def _pick(source: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if key in source and source.get(key) is not None}


def _first_str(source: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _short_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _approval_status(block: dict[str, Any]) -> str:
    status = str(block.get("status") or "").strip().lower()
    if status:
        return status
    if block.get("resolved") is True:
        return "resolved"
    return "pending"


def _approval_message(block: dict[str, Any]) -> str:
    prompt = block.get("prompt")
    if isinstance(prompt, dict):
        text = prompt.get("text") or prompt.get("message") or prompt.get("reason")
        if text:
            return _short_text(text)
    return _short_text(block.get("reason") or block.get("message") or "approval requested")


def _tool_result_status(block: dict[str, Any]) -> str:
    if block.get("ok") is True:
        return "completed"
    if _is_tool_redirect(block):
        return "redirected"
    if block.get("ok") is False or block.get("error") or block.get("error_kind"):
        return "failed"
    return "observed"


def _tool_message(tool: str, status: str, block: dict[str, Any]) -> str:
    if status == "redirected":
        return _short_text(f"{tool} redirected to a native tool lane")
    if status == "failed":
        err = block.get("error") or block.get("error_kind") or ""
        return _short_text(f"{tool} failed {err}".strip())
    return _short_text(f"{tool} {status}")


def _is_tool_redirect(block: dict[str, Any]) -> bool:
    recovery = block.get("recovery")
    if not isinstance(recovery, dict):
        return False
    return str(recovery.get("reason") or "").strip().lower() == "tool_redirect"


def _activity_surface(kind: str) -> str:
    lowered = kind.lower()
    if lowered.startswith("approval."):
        return "approval_plan"
    if lowered.startswith(("team.", "task.", "subagent.", "workflow.")):
        return "task_progress"
    return "status"


def _activity_status(kind: str, event: dict[str, Any]) -> str:
    explicit = str(event.get("status") or "").strip().lower()
    if explicit:
        return explicit
    lowered = kind.lower()
    if lowered.endswith((".start", ".started", ".queued")):
        return "started"
    if lowered.endswith((".progress", ".update", ".running")):
        return "in_progress"
    if lowered.endswith((".end", ".complete", ".completed", ".succeeded", ".success")):
        return "completed"
    if lowered.endswith((".fail", ".failed", ".error", ".cancelled", ".canceled")):
        return "failed"
    if event.get("ok") is True:
        return "completed"
    if event.get("ok") is False:
        return "failed"
    return "observed"


def _activity_message(kind: str, event: dict[str, Any]) -> str:
    output = event.get("output")
    if isinstance(output, dict):
        summary = output.get("summary") or output.get("message")
        if summary:
            return _short_text(summary)
    message = event.get("message") or event.get("summary") or event.get("label")
    if message:
        return _short_text(message)
    subject = event.get("team_task_subject") or event.get("task") or event.get("subagent")
    if subject:
        return _short_text(f"{kind}: {subject}")
    return kind


def _status_message(
    *,
    stop_reason: str | None,
    transition_reason: str | None,
    abort_reason: str | None,
) -> str:
    bits = []
    if transition_reason:
        bits.append(f"transition={transition_reason}")
    if stop_reason:
        bits.append(f"stop={stop_reason}")
    if abort_reason:
        bits.append(f"abort={abort_reason}")
    return "; ".join(bits) if bits else "turn status recorded"


__all__ = [
    "ExecutionStateItem",
    "SURFACE_ORDER",
    "build_execution_state",
]
