from __future__ import annotations

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.file_ops import classify_file_mutation_risk
from nerya.tools.native.shell import classify_shell_risk
from nerya.tools.native.skill import is_browser_skill_script_run
from nerya.tools.native.task import TaskState, exit_plan_mode_handler
from nerya.tools.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
)
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall, ToolDescriptor


pytestmark = pytest.mark.smoke


def _descriptor(
    *,
    name: str = "run_shell",
    risk: RiskLevel = RiskLevel.EXEC,
    scope: PermissionScope = PermissionScope.WORKSPACE,
    risk_classifier=None,
    auto_approve: bool = False,
    auto_approve_when=None,
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
        auto_approve_when=auto_approve_when,
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
        classify_shell_risk({
            "command": "python -c \"from nerya.data import data_api; data_api()\"",
            "description": "Check wallet capability catalog structure",
        })
        is RiskLevel.READ
    )
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


def test_yolo_allows_dangerous_native_tool_permissions():
    descriptor = _descriptor(risk_classifier=classify_shell_risk)

    decision = _decision(
        descriptor,
        {"command": "rm notes.txt"},
        PermissionMode.YOLO,
    )

    assert decision.is_allow()
    assert decision.requires_approval is False
    assert decision.risk is RiskLevel.DANGEROUS


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


def test_browser_skill_scripts_auto_approve_without_prompt():
    descriptor = _descriptor(
        name="script_run",
        risk=RiskLevel.EXEC,
        scope=PermissionScope.WORKSPACE,
        auto_approve_when=is_browser_skill_script_run,
    )

    browser_payload = {"skill_id": "browser", "name": "browser_session.py"}

    default_decision = _decision(
        descriptor,
        browser_payload,
        PermissionMode.DEFAULT,
    )
    assert default_decision.is_allow()
    assert default_decision.requires_approval is False
    assert default_decision.risk is RiskLevel.EXEC

    plan_decision = _decision(
        descriptor,
        browser_payload,
        PermissionMode.PLAN,
    )
    assert plan_decision.is_allow()
    assert plan_decision.requires_approval is False

    other_skill_decision = _decision(
        descriptor,
        {"skill_id": "research", "name": "fetch_url.py"},
        PermissionMode.DEFAULT,
    )
    assert other_skill_decision.is_ask()
    assert other_skill_decision.requires_approval is True


def test_registered_script_run_auto_approves_browser_skill_scripts(tmp_path):
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=tmp_path, skill_roots=[tmp_path])
    register_native_tools(registry, deps)
    descriptor = registry.get("script_run")

    decision = _decision(
        descriptor,
        {"skill_id": "browser", "name": "browser_session.py"},
        PermissionMode.DEFAULT,
    )

    assert decision.is_allow()
    assert decision.requires_approval is False
    assert decision.reason == "auto_approve predicate"


def test_registered_wallet_install_requires_exec_approval_by_default(tmp_path):
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=tmp_path, skill_roots=[tmp_path])
    register_native_tools(registry, deps)
    descriptor = registry.get("wallet_install")

    decision = _decision(
        descriptor,
        {"provider": "self_custody", "mode": "goat"},
        PermissionMode.DEFAULT,
    )

    assert descriptor.permission_scope is PermissionScope.NETWORK
    assert decision.is_ask()
    assert decision.requires_approval is True
    assert decision.risk is RiskLevel.EXEC


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
