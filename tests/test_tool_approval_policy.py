from __future__ import annotations

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.tools.native.file_ops import classify_file_mutation_risk
from nerya.tools.native.shell import classify_shell_risk
from nerya.tools.native.task import TaskState, exit_plan_mode_handler
from nerya.tools.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
)
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall, ToolDescriptor


pytestmark = pytest.mark.smoke


def _descriptor(
    *,
    name: str = "run_shell",
    risk: RiskLevel = RiskLevel.EXEC,
    scope: PermissionScope = PermissionScope.WORKSPACE,
    risk_classifier=None,
    auto_approve: bool = False,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="test descriptor",
        input_schema={"type": "object", "properties": {}},
        handler=lambda _call: None,
        risk=risk,
        permission_scope=scope,
        risk_classifier=risk_classifier,
        auto_approve=auto_approve,
    )


def _decision(descriptor: ToolDescriptor, payload: dict, mode: PermissionMode):
    return PermissionEngine().evaluate(
        PermissionRequest(descriptor=descriptor, payload=payload),
        PermissionContext(mode=mode),
    )


def test_shell_research_commands_are_read_risk():
    assert classify_shell_risk({"command": "rg -n approval nerya"}) is RiskLevel.READ
    assert classify_shell_risk({"command": "find . -name '*.py'"}) is RiskLevel.READ
    assert (
        classify_shell_risk({"command": "git diff -- nerya/tools/permissions.py"})
        is RiskLevel.READ
    )


def test_shell_delete_and_sensitive_config_writes_are_dangerous():
    assert classify_shell_risk({"command": "rm notes.txt"}) is RiskLevel.DANGEROUS
    assert classify_shell_risk({"command": "  rm notes.txt"}) is RiskLevel.DANGEROUS
    assert classify_shell_risk({"command": "echo live > nerya.yml"}) is RiskLevel.DANGEROUS
    assert (
        classify_shell_risk({"command": "find . -name '*.tmp' -delete"})
        is RiskLevel.DANGEROUS
    )


def test_default_mode_allows_research_shell_but_asks_on_dangerous_shell():
    descriptor = _descriptor(risk_classifier=classify_shell_risk)

    read_decision = _decision(
        descriptor,
        {"command": "rg -n PermissionEngine nerya/tools"},
        PermissionMode.DEFAULT,
    )
    assert read_decision.is_allow()

    delete_decision = _decision(
        descriptor,
        {"command": "rm notes.txt"},
        PermissionMode.DEFAULT,
    )
    assert delete_decision.is_ask()
    assert delete_decision.requires_approval is True


def test_yolo_still_asks_for_dangerous_operations():
    descriptor = _descriptor(risk_classifier=classify_shell_risk)

    decision = _decision(
        descriptor,
        {"command": "rm notes.txt"},
        PermissionMode.YOLO,
    )

    assert decision.is_ask()
    assert decision.requires_approval is True


def test_sensitive_config_writes_escalate_but_code_edits_remain_fluid():
    descriptor = _descriptor(
        name="write_file",
        risk=RiskLevel.WRITE,
        risk_classifier=classify_file_mutation_risk,
    )

    code_edit = _decision(
        descriptor,
        {"path": "nerya/tools/permissions.py"},
        PermissionMode.DEFAULT,
    )
    assert code_edit.is_allow()
    assert code_edit.risk is RiskLevel.WRITE

    config_edit = _decision(
        descriptor,
        {"path": "strategies/s1/limits.yml"},
        PermissionMode.DEFAULT,
    )
    assert config_edit.is_ask()
    assert config_edit.risk is RiskLevel.DANGEROUS


def test_plan_mode_allows_auto_approved_research_exec_tools():
    descriptor = _descriptor(
        name="llm_classify",
        risk=RiskLevel.EXEC,
        scope=PermissionScope.NETWORK,
        auto_approve=True,
    )

    decision = _decision(descriptor, {"text": "classify this"}, PermissionMode.PLAN)

    assert decision.is_allow()


def test_exit_plan_mode_auto_approves_inside_yolo_mode():
    state = TaskState()
    call = ToolCall(
        name="exit_plan_mode",
        arguments={"plan": "Do the low-risk implementation work."},
    )

    result = exit_plan_mode_handler(
        call,
        task_state=state,
        permission_mode="yolo",
    )

    assert result.is_error is False
    assert result.content[1].data["status"] == "approved"
    assert result.content[1].data["auto_approved"] is True
    assert state.plan_decision == "approved"
