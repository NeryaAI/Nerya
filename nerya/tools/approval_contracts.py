"""Canonical protocol markers for approval-paused tool execution.

``permission_pending`` is a tool-error kind: the requested operation has not
executed because the operator still owes a decision. ``approval_pending`` is a
turn close/transition reason: the agent loop must stop so the approval remains
actionable. Keeping both at one boundary prevents persistence, event projection,
subagents, and the loop from growing subtly different string sets.
"""

from __future__ import annotations

from typing import Any

from .types import ToolErrorKind


PERMISSION_PENDING_ERROR_KIND = ToolErrorKind.PERMISSION_PENDING.value
APPROVAL_PENDING_REASON = "approval_pending"
APPROVAL_PENDING_MARKERS = frozenset({
    PERMISSION_PENDING_ERROR_KIND,
    APPROVAL_PENDING_REASON,
})


def _normalise_marker(value: Any) -> str:
    return str(value or "").strip().lower()


def is_permission_pending_marker(value: Any) -> bool:
    return _normalise_marker(value) == PERMISSION_PENDING_ERROR_KIND


def is_approval_pending_marker(value: Any) -> bool:
    """Accept the tool-error and turn-reason forms at persistence boundaries."""

    return _normalise_marker(value) in APPROVAL_PENDING_MARKERS


__all__ = [
    "APPROVAL_PENDING_MARKERS",
    "APPROVAL_PENDING_REASON",
    "PERMISSION_PENDING_ERROR_KIND",
    "is_approval_pending_marker",
    "is_permission_pending_marker",
]
