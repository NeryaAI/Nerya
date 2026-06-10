from __future__ import annotations

import pytest

from nerya.agent.artifact_index import build_artifact_index, render_final_report
from nerya.agent.execution_state import build_execution_state
from nerya.agent.kernel import _captured_domain_approval_from_tool_result_block


pytestmark = pytest.mark.smoke


def test_execution_state_routes_blocks_and_activity_by_surface() -> None:
    state = build_execution_state(
        turn_id="turn_1",
        blocks=[
            {
                "block": {
                    "kind": "approval_request",
                    "approval_id": "approval_1",
                    "call_id": "call_approval",
                    "prompt": {"text": "Approve this tool call?"},
                    "status": "pending",
                },
            },
            {
                "block": {
                    "kind": "tool_use",
                    "call_id": "call_read",
                    "skill_id": "native",
                    "action": "read_file",
                    "payload": {"path": "README.md"},
                },
            },
            {
                "block": {
                    "kind": "tool_result",
                    "call_id": "call_read",
                    "skill_id": "native",
                    "action": "read_file",
                    "ok": True,
                    "result": "contents",
                },
            },
        ],
        activity_events=[
            {
                "kind": "team.member.end",
                "team_run_id": "team_1",
                "team_task_id": "task_1",
                "subagent": "analyst",
                "output": {"summary": "done"},
            }
        ],
        stop_reason="end_turn",
        transition_reason="model_done",
        aborted=False,
    )

    assert state["version"] == 1
    assert state["counters"]["approval_plan"] == 1
    assert state["counters"]["tool_progress"] == 2
    assert state["counters"]["task_progress"] == 1
    assert state["counters"]["status"] == 1

    approval = state["surfaces"]["approval_plan"][0]
    assert approval["status"] == "pending"
    assert approval["item_id"] == "approval_1"

    tool_items = state["surfaces"]["tool_progress"]
    assert [item["status"] for item in tool_items] == ["started", "completed"]
    assert tool_items[0]["tool"] == "native.read_file"

    task = state["surfaces"]["task_progress"][0]
    assert task["status"] == "completed"
    assert task["parent_id"] == "team_1"


def test_trade_pending_approval_result_becomes_approval_request_block() -> None:
    captured = _captured_domain_approval_from_tool_result_block(
        {
            "kind": "tool_result",
            "call_id": "call_trade",
            "skill_id": "native",
            "action": "trade_intent_submit",
            "ok": True,
            "result": {
                "status": "pending_approval",
                "approval_id": "approval_trade_1",
                "risk_decision": {"decision": "escalate"},
            },
        }
    )

    assert captured is not None
    call_id, block = captured
    assert call_id == "call_trade"
    assert block["kind"] == "approval_request"
    assert block["approval_id"] == "approval_trade_1"
    assert block["action"] == "trade_intent_submit"
    assert block["record"]["status"] == "pending"


def test_tool_redirect_is_state_guidance_not_failed_progress() -> None:
    state = build_execution_state(
        turn_id="turn_redirect",
        blocks=[
            {
                "turn_id": "turn_redirect",
                "block": {
                    "kind": "tool_result",
                    "call_id": "call_shell",
                    "skill_id": "native",
                    "action": "run_shell",
                    "ok": False,
                    "error_kind": "permission_denied",
                    "error": "use native file tools",
                    "recovery": {
                        "reason": "tool_redirect",
                        "preferred_tools": ["glob", "list_dir", "read_file"],
                    },
                },
            }
        ],
    )

    item = state["surfaces"]["tool_progress"][0]
    assert item["status"] == "redirected"
    assert item["message"] == "native.run_shell redirected to a native tool lane"
    assert item["payload"]["recovery"]["reason"] == "tool_redirect"


def test_artifact_index_excludes_tool_redirect_from_commands_and_errors() -> None:
    blocks = [
        {
            "turn_id": "turn_redirect",
            "block": {
                "kind": "tool_use",
                "call_id": "call_shell",
                "skill_id": "native",
                "action": "run_shell",
                "payload": {"command": "ls -la ."},
            },
        },
        {
            "turn_id": "turn_redirect",
            "block": {
                "kind": "tool_result",
                "call_id": "call_shell",
                "skill_id": "native",
                "action": "run_shell",
                "ok": False,
                "error_kind": "permission_denied",
                "error": "use native file tools",
                "recovery": {
                    "reason": "tool_redirect",
                    "preferred_tools": ["glob", "list_dir", "read_file"],
                },
            },
        },
    ]

    index = build_artifact_index(blocks)
    report = render_final_report(index)

    assert index.commands == []
    assert index.errors == []
    assert report["headline"] == "no file changes, no commands, no errors"


def test_artifact_index_keeps_real_permission_denied_errors() -> None:
    blocks = [
        {
            "block": {
                "kind": "tool_use",
                "call_id": "call_shell",
                "skill_id": "native",
                "action": "run_shell",
                "payload": {"command": "cat /etc/passwd"},
            },
        },
        {
            "block": {
                "kind": "tool_result",
                "call_id": "call_shell",
                "skill_id": "native",
                "action": "run_shell",
                "ok": False,
                "error_kind": "permission_denied",
                "error": {
                    "kind": "permission_denied",
                    "message": "workspace sandbox blocked an absolute path",
                },
            },
        },
    ]

    index = build_artifact_index(blocks)

    assert len(index.commands) == 1
    assert len(index.errors) == 1
    assert index.errors[0]["kind"] == "permission_denied"


def test_artifact_index_moves_recovered_tool_errors_out_of_final_errors() -> None:
    blocks = [
        {
            "block": {
                "kind": "tool_use",
                "call_id": "call_task_bad",
                "skill_id": "native",
                "action": "task_create",
                "payload": {"task_id": "daily_review"},
            },
        },
        {
            "block": {
                "kind": "tool_result",
                "call_id": "call_task_bad",
                "skill_id": "native",
                "action": "task_create",
                "ok": False,
                "error": {
                    "kind": "schema_validation",
                    "message": "must set either every_seconds or cron",
                },
            },
        },
        {
            "block": {
                "kind": "tool_use",
                "call_id": "call_task_good",
                "skill_id": "native",
                "action": "task_create",
                "payload": {"task_id": "daily_review", "cron": "0 9 * * *"},
            },
        },
        {
            "block": {
                "kind": "tool_result",
                "call_id": "call_task_good",
                "skill_id": "native",
                "action": "task_create",
                "ok": True,
                "result": {"status": "created"},
            },
        },
    ]

    index = build_artifact_index(blocks)
    report = render_final_report(index)

    assert index.errors == []
    assert len(index.recovered_errors) == 1
    assert index.recovered_errors[0]["tool"] == "task_create"
    assert report["headline"] == "no file changes, no commands, no errors"
    assert report["recovered_errors"][0]["kind"] == "schema_validation"
