"""Structured observation of tool execution paused for operator approval.

Authorization remains owned by :mod:`nerya.tools.executor`.  This module never
re-evaluates policy; it only projects already-produced typed results, canonical
blocks, and child rejected-action envelopes onto one immutable value.  Keeping
observation separate from authorization prevents the loop, kernel, and team
runtime from growing independent approval gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .approval_contracts import is_permission_pending_marker
from .types import ToolErrorKind, ToolResult


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class ApprovalPause:
    """One structured, already-decided approval pause."""

    tool_use_id: str = ""
    tool_name: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    caller: str = ""
    recovery_hint: Mapping[str, Any] = field(default_factory=dict)
    approval_request: Mapping[str, Any] = field(default_factory=dict)
    nested: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_use_id", _text(self.tool_use_id))
        object.__setattr__(self, "tool_name", _text(self.tool_name))
        object.__setattr__(self, "payload", _dict(self.payload))
        object.__setattr__(self, "caller", _text(self.caller))
        object.__setattr__(self, "recovery_hint", _dict(self.recovery_hint))
        object.__setattr__(self, "approval_request", _dict(self.approval_request))
        object.__setattr__(self, "nested", bool(self.nested))

    def as_nested_dict(self) -> dict[str, Any]:
        """Render the established parent-tool recovery protocol."""

        out: dict[str, Any] = {
            "nested_permission_pending": True,
            "nested_tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
            "caller": self.caller,
        }
        if self.recovery_hint:
            out["recovery_hint"] = dict(self.recovery_hint)
        if self.approval_request:
            out["approval_request"] = dict(self.approval_request)
        return out


def approval_pause_from_result(result: ToolResult) -> ApprovalPause | None:
    """Project a typed executor result without re-evaluating permission policy."""

    if not result.is_error or result.error is None:
        return None
    kind = result.error.kind
    if isinstance(kind, ToolErrorKind):
        if kind is not ToolErrorKind.PERMISSION_PENDING:
            return None
    elif not is_permission_pending_marker(kind):
        return None

    recovery = _dict(result.error.recovery_hint)
    nested = recovery.get("nested_permission_pending") is True
    raw_tool_use_id = (
        recovery.get("nested_tool_use_id")
        if nested
        else result.tool_use_id or recovery.get("tool_use_id")
    )
    approval_request = _dict(result.metadata.get("approval_request"))
    if not approval_request:
        approval_request = _dict(recovery.get("approval_request"))
    return ApprovalPause(
        tool_use_id=_text(raw_tool_use_id),
        tool_name=_text(recovery.get("tool_name") or result.name),
        payload=_dict(recovery.get("payload")),
        caller=_text(recovery.get("caller")),
        recovery_hint=recovery,
        approval_request=approval_request,
        nested=nested,
    )


def approval_pause_from_block(block: Mapping[str, Any]) -> ApprovalPause | None:
    """Project a canonical ``tool_result``/trace block by exact marker only."""

    if block.get("ok") is True:
        return None
    if not is_permission_pending_marker(block.get("error_kind")):
        return None

    recovery = _dict(block.get("recovery")) or _dict(block.get("recovery_hint"))
    nested = recovery.get("nested_permission_pending") is True
    raw_tool_use_id = (
        recovery.get("nested_tool_use_id")
        if nested
        else block.get("call_id")
        or block.get("tool_use_id")
        or recovery.get("tool_use_id")
    )
    payload = _dict(block.get("payload")) or _dict(recovery.get("payload"))
    approval_request = _dict(block.get("approval_request"))
    if not approval_request:
        approval_request = _dict(recovery.get("approval_request"))
    return ApprovalPause(
        tool_use_id=_text(raw_tool_use_id),
        tool_name=_text(
            recovery.get("tool_name")
            or block.get("action")
            or block.get("name")
            or block.get("skill_id")
        ),
        payload=payload,
        caller=_text(recovery.get("caller") or block.get("caller")),
        recovery_hint=recovery,
        approval_request=approval_request,
        nested=nested,
    )


def approval_pause_from_rejected_record(
    record: Mapping[str, Any],
) -> ApprovalPause | None:
    """Project one child ``metrics.rejected_actions`` entry."""

    if not is_permission_pending_marker(record.get("error_kind")):
        return None
    entry = _dict(record.get("entry"))
    recovery = _dict(record.get("recovery_hint"))
    payload = _dict(record.get("payload")) or _dict(recovery.get("payload"))
    return ApprovalPause(
        tool_use_id=_text(
            record.get("tool_use_id")
            or record.get("call_id")
            or entry.get("tool_use_id")
            or entry.get("call_id")
            or recovery.get("nested_tool_use_id")
            or recovery.get("tool_use_id")
        ),
        tool_name=_text(
            record.get("tool_name")
            or record.get("skill")
            or recovery.get("tool_name")
        ),
        payload=payload,
        caller=_text(
            record.get("caller")
            or entry.get("caller")
            or recovery.get("caller")
        ),
        recovery_hint=recovery,
        approval_request=_dict(record.get("approval_request")),
        nested=True,
    )


def nested_approval_pause_from_envelope(
    envelope: Mapping[str, Any],
) -> ApprovalPause | None:
    """Return the first structured child-native approval pause."""

    metrics = _dict(envelope.get("metrics"))
    records = metrics.get("rejected_actions")
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        pause = approval_pause_from_rejected_record(record)
        if pause is not None:
            return pause
    return None


def first_approval_pause(
    results: Iterable[ToolResult],
) -> ApprovalPause | None:
    for result in results:
        pause = approval_pause_from_result(result)
        if pause is not None:
            return pause
    return None


__all__ = [
    "ApprovalPause",
    "approval_pause_from_block",
    "approval_pause_from_rejected_record",
    "approval_pause_from_result",
    "first_approval_pause",
    "nested_approval_pause_from_envelope",
]
