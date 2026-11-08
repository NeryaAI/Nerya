from __future__ import annotations

import pytest

from nerya.agent.artifact_index import build_artifact_index, render_final_report
from nerya.agent.execution_state import build_execution_state


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


def test_artifact_index_uses_verifier_validation_catalogue() -> None:
    blocks = [
        {
            "block": {
                "kind": "tool_use",
                "call_id": "call_lint",
                "action": "run_shell",
                "payload": {"command": "ruff check nerya"},
            },
        },
        {
            "block": {
                "kind": "tool_result",
                "call_id": "call_lint",
                "action": "run_shell",
                "ok": True,
                "output": {"exit_code": 0},
            },
        },
    ]

    index = build_artifact_index(blocks)

    assert index.tests_run == [
        {"command": "ruff check nerya", "exit_code": 0, "ok": True}
    ]


def test_artifact_index_uses_source_bound_producer_receipt_after_last_edit() -> None:
    from copy import deepcopy
    from nerya.agent.tool_projection import project_tool_results
    from nerya.tools.types import ToolResult
    from nerya.tools.verification import attach_verification, verification_receipt

    root = "evolution/proposals/prp_fixture/after/strategies/candidate"
    path = root + "/main.py"
    edit = [
        {"kind": "tool_use", "call_id": "edit", "action": "edit_file", "payload": {"path": path}},
        {"kind": "tool_result", "call_id": "edit", "action": "edit_file", "ok": True},
    ]
    result = ToolResult.from_json(tool_use_id="check", name="validate_package", data={"ok": True})
    attach_verification(result, verification_receipt(
        artifact_id="package:candidate", revision="a" * 64, scope="Saved source package",
        checks=[{"check_id": "source_binding", "status": "passed", "evidence_ref": root},
                {"check_id": "contract", "status": "passed"}]))
    producer = list(project_tool_results([result],
        render_tool_result=lambda value: {"content": value.text()},
        rendered_tool_result_text=lambda block: block["content"]).event_blocks)
    blocks = edit + [{"kind": "tool_use", "call_id": "check", "action": "validate_package"}] + producer
    assert build_artifact_index(blocks).unverified_risks == []

    late_edit = deepcopy(edit)
    for block in late_edit:
        block["call_id"] = "late-edit"
    assert build_artifact_index(blocks + late_edit).unverified_risks[0]["kind"] == "edit_without_verification"
    unrelated = deepcopy(edit)
    unrelated[0]["payload"]["path"] = root + "-other/main.py"
    assert build_artifact_index(unrelated + blocks[2:]).unverified_risks
    for status in ("failed", "pending", "missing", "stale"):
        failed = deepcopy(blocks)
        failed[-1]["verification"]["checks"][1]["status"] = status
        assert build_artifact_index(failed).unverified_risks
    unverified = deepcopy(blocks)
    unverified[-1].pop("verification")  # JSON content/ok:true cannot certify source.
    assert build_artifact_index(unverified).unverified_risks
    reread = [{"kind": "tool_use", "action": "read_file", "payload": {"path": path}}]
    assert build_artifact_index(edit + reread).unverified_risks


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
