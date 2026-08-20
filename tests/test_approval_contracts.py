from __future__ import annotations

import pytest

from nerya.tools.approval_contracts import (
    APPROVAL_PENDING_REASON,
    PERMISSION_PENDING_ERROR_KIND,
    is_approval_pending_marker,
    is_permission_pending_marker,
)
from nerya.tools.approval_runtime import (
    approval_pause_from_block,
    approval_pause_from_result,
    nested_approval_pause_from_envelope,
)
from nerya.tools.types import ToolError, ToolErrorKind, ToolResult


pytestmark = pytest.mark.smoke


def _error_result(kind: ToolErrorKind) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=f"toolu_{kind.value}",
        name="approval_probe",
        error=ToolError(kind=kind, message=kind.value),
    )


def test_approval_markers_accept_tool_and_turn_protocol_forms() -> None:
    assert PERMISSION_PENDING_ERROR_KIND == ToolErrorKind.PERMISSION_PENDING.value
    assert is_permission_pending_marker(" PERMISSION_PENDING ") is True
    assert is_permission_pending_marker(APPROVAL_PENDING_REASON) is False
    assert is_approval_pending_marker(PERMISSION_PENDING_ERROR_KIND) is True
    assert is_approval_pending_marker(APPROVAL_PENDING_REASON) is True
    assert is_approval_pending_marker("permission_denied") is False


def test_typed_result_projects_one_structured_pause() -> None:
    result = _error_result(ToolErrorKind.PERMISSION_PENDING)
    assert result.error is not None
    result.error.recovery_hint.update({
        "tool_name": "run_shell",
        "payload": {"command": "echo safe"},
        "caller": "subagent:analyst",
    })
    result.metadata["approval_request"] = {"approval_id": "approval-1"}

    pause = approval_pause_from_result(result)

    assert pause is not None
    assert pause.tool_use_id == "toolu_permission_pending"
    assert pause.tool_name == "run_shell"
    assert pause.payload == {"command": "echo safe"}
    assert pause.caller == "subagent:analyst"
    assert pause.approval_request == {"approval_id": "approval-1"}
    assert approval_pause_from_result(
        _error_result(ToolErrorKind.PERMISSION_DENIED)
    ) is None
    assert approval_pause_from_result(ToolResult.from_json(
        tool_use_id="toolu_ok",
        name="approval_probe",
        data={"ok": True},
    )) is None


def test_canonical_block_requires_exact_error_kind_not_error_text() -> None:
    assert approval_pause_from_block({
        "kind": "tool_result",
        "ok": False,
        "error_kind": "permission_denied",
        "error": "message happens to mention permission_pending",
    }) is None

    pause = approval_pause_from_block({
        "kind": "tool_result",
        "call_id": "call-1",
        "action": "run_shell",
        "ok": False,
        "error_kind": PERMISSION_PENDING_ERROR_KIND,
        "payload": {"command": "echo safe"},
        "recovery": {"caller": "agent:root"},
    })

    assert pause is not None
    assert pause.tool_use_id == "call-1"
    assert pause.tool_name == "run_shell"
    assert pause.payload == {"command": "echo safe"}
    assert pause.caller == "agent:root"


def test_nested_envelope_projects_established_recovery_shape() -> None:
    pause = nested_approval_pause_from_envelope({
        "metrics": {
            "rejected_actions": [
                {
                    "error_kind": PERMISSION_PENDING_ERROR_KIND,
                    "tool_use_id": "child-call-1",
                    "tool_name": "write_file",
                    "payload": {"path": "README.md"},
                    "caller": "subagent:writer",
                    "recovery_hint": {"scope": "workspace"},
                    "approval_request": {"approval_id": "approval-child"},
                }
            ]
        }
    })

    assert pause is not None
    assert pause.nested is True
    assert pause.as_nested_dict() == {
        "nested_permission_pending": True,
        "nested_tool_use_id": "child-call-1",
        "tool_name": "write_file",
        "payload": {"path": "README.md"},
        "caller": "subagent:writer",
        "recovery_hint": {"scope": "workspace"},
        "approval_request": {"approval_id": "approval-child"},
    }
