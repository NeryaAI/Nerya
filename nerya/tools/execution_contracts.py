"""Execution invariants shared by dispatch and agent phases.

This module does not import an agent, executor or orchestrator. A returned
observation is evidence only for the exact call identity it belongs to.
"""
from __future__ import annotations

import math
from ..harness.cancellation import is_cancelled
from typing import TYPE_CHECKING

from .types import RiskLevel, ToolCall, ToolError, ToolErrorKind, ToolResult

if TYPE_CHECKING:
    from .registry import ToolRegistry


def is_read_only_tool(name: str, registry: ToolRegistry) -> bool:
    descriptor = registry.find(name)
    return bool(descriptor and descriptor.read_only and descriptor.risk == RiskLevel.READ)


def execution_unknown_result(call: ToolCall, reason: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id, name=call.name,
        error=ToolError(
            kind=ToolErrorKind.ABORTED,
            message="Tool execution outcome is unknown: " + reason,
            detail={"reason": "executor_contract_violation", "execution_state": "unknown"},
            retryable=False,
            recovery_hint={"action": "inspect_state_before_retry"},
        ),
    )


def pair_executed_results(calls: list[ToolCall], results: list[ToolResult]) -> list[ToolResult]:
    """One result per input call, in input order; never guess from list positions."""
    by_id: dict[str, list[ToolResult]] = {}
    for result in results:
        if isinstance(result, ToolResult):
            by_id.setdefault(result.tool_use_id, []).append(result)
    paired: list[ToolResult] = []
    for call in calls:
        candidates = by_id.get(call.id, [])
        paired.append(
            candidates[0] if len(candidates) == 1 and candidates[0].name == call.name
            else execution_unknown_result(call, "missing, duplicate or mismatched result")
        )
    return paired


def dispatch_stop_reason(call: ToolCall, *, now: float) -> str:
    """Host-owned cancellation/deadline must be checked before every attempt."""
    if is_cancelled(call.metadata.get("cancel_token")):
        return "cancelled"
    deadline = call.metadata.get("turn_deadline_epoch")
    if deadline is not None:
        try:
            deadline = float(deadline)
        except (TypeError, ValueError):
            return "invalid_deadline"
        if not math.isfinite(deadline):
            return "invalid_deadline"
        if now >= deadline:
            return "timeout"
    return ""


def skipped_before_dispatch(call: ToolCall, reason: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id, name=call.name,
        error=ToolError(
            kind=ToolErrorKind.ABORTED,
            message="Tool was not started: " + reason,
            detail={"reason": reason, "execution_state": "not_started"},
            retryable=False,
        ),
    )
