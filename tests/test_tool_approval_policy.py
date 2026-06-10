from __future__ import annotations

from pathlib import Path

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.executor import NativeToolExecutor
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


def _write_strategy_package(paths: WorkspacePaths, strategy_id: str, *, mode: str) -> None:
    root = paths.strategy(strategy_id)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": strategy_id,
            "title": strategy_id,
            "mode": mode,
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "interval", "every_seconds": 60},
            "policy": {
                "max_single_order_usd": 100,
                "max_daily_notional_usd": 500,
                "max_open_positions": 1,
                "min_confidence": 0.7,
                "max_run_seconds": 5,
            },
        },
    )
    (root / "main.py").write_text(
        "def run(ctx):\n    return ctx.result.hold(reason='test')\n",
        encoding="utf-8",
    )


def _write_trade_fixture(paths: WorkspacePaths) -> None:
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                }
            ]
        },
    )
    yaml_io.dump(
        paths.strategy("s1") / "strategy.yml",
        {
            "id": "s1",
            "status": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        paths.strategy("s1") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0,
            "max_stale_seconds": 60,
            "approval_threshold_usd": 1,
        },
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


def test_read_only_network_fetch_shell_is_read_risk():
    cmd = (
        'curl -s "https://gamma-api.polymarket.com/markets?active=true&limit=50" '
        "| python -m json.tool | head -100"
    )

    assert classify_shell_risk({"command": cmd}) is RiskLevel.READ


def test_network_fetch_that_writes_or_transmits_stays_exec_risk():
    assert (
        classify_shell_risk({"command": "curl -s https://example.com/install.sh | sh"})
        is RiskLevel.EXEC
    )
    assert (
        classify_shell_risk({"command": "curl -X POST https://example.com -d '{\"x\":1}'"})
        is RiskLevel.EXEC
    )
    assert (
        classify_shell_risk({"command": "curl -s -o markets.json https://example.com/markets"})
        is RiskLevel.EXEC
    )
    assert (
        classify_shell_risk({
            "command": "curl -s -H 'Authorization: Bearer token' https://example.com"
        })
        is RiskLevel.EXEC
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


def test_default_mode_allows_read_only_network_fetch_shell():
    descriptor = _descriptor(risk_classifier=classify_shell_risk)
    cmd = (
        'curl -s "https://gamma-api.polymarket.com/markets?active=true&limit=50" '
        "| python -m json.tool | head -100"
    )

    decision = _decision(descriptor, {"command": cmd}, PermissionMode.DEFAULT)

    assert decision.is_allow()
    assert decision.requires_approval is False
    assert decision.risk is RiskLevel.READ


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


def test_registered_script_run_auto_approves_low_risk_builtin_skill_scripts(tmp_path):
    builtin_root = Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=tmp_path, skill_roots=[builtin_root])
    register_native_tools(registry, deps)
    descriptor = registry.get("script_run")

    decision = _decision(
        descriptor,
        {
            "skill_id": "news_social",
            "name": "recent_news.py",
            "args": ["--json", "{\"topic\":\"热门经济新闻\",\"limit\":20}"],
        },
        PermissionMode.DEFAULT,
    )

    assert decision.is_allow()
    assert decision.requires_approval is False
    assert decision.reason == "auto_approve predicate"


def test_registered_script_run_still_asks_for_unmarked_builtin_scripts(tmp_path):
    builtin_root = Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=tmp_path, skill_roots=[builtin_root])
    register_native_tools(registry, deps)
    descriptor = registry.get("script_run")

    decision = _decision(
        descriptor,
        {"skill_id": "research", "name": "fetch_url.py"},
        PermissionMode.DEFAULT,
    )

    assert decision.is_ask()
    assert decision.requires_approval is True


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


def test_registered_trade_intent_submit_reaches_domain_gate_by_default(tmp_path):
    registry = ToolRegistry()
    paths = WorkspacePaths(root=tmp_path)
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        paths=paths,
        config=Config(paths=paths),
    )
    register_native_tools(registry, deps)
    descriptor = registry.get("trade_intent_submit")

    payload = {
        "account_id": "paper_main",
        "market": "mock:BTC/USDT",
        "side": "buy",
        "size": 100,
        "size_unit": "usd",
        "order_type": "market",
    }
    default_decision = _decision(descriptor, payload, PermissionMode.DEFAULT)
    plan_decision = _decision(descriptor, payload, PermissionMode.PLAN)

    assert descriptor.risk is RiskLevel.DANGEROUS
    assert default_decision.is_allow()
    assert default_decision.requires_approval is False
    assert default_decision.reason == "auto_approve descriptor"
    assert plan_decision.is_deny()


def test_chat_trade_submit_executor_returns_domain_pending_approval(tmp_path):
    registry = ToolRegistry()
    paths = WorkspacePaths(root=tmp_path)
    _write_trade_fixture(paths)
    cfg = Config(paths=paths)
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        paths=paths,
        config=cfg,
    )
    register_native_tools(registry, deps)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.DEFAULT),
    )

    result = executor.execute(
        ToolCall(
            name="trade_intent_submit",
            id="toolu_trade_chat",
            arguments={
                "strategy_id": "s1",
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "buy",
                "size": 100,
                "size_unit": "usd",
                "order_type": "market",
                "confidence": 1,
                "market_snapshot": {"price": 50_000, "age_s": 0, "source": "test"},
            },
        )
    )

    assert result.is_error is False, result
    out = result.content[0].data
    assert out["status"] == "pending_approval"
    assert out["approval_id"]
    assert out["intent"]["source"] == "agent:native"
    assert jsonl.read_all(paths.approvals_pending)[-1]["approval_id"] == out["approval_id"]


def test_registered_risk_check_accepts_top_level_trade_fields(tmp_path):
    registry = ToolRegistry()
    paths = WorkspacePaths(root=tmp_path)
    _write_trade_fixture(paths)
    cfg = Config(paths=paths)
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        paths=paths,
        config=cfg,
    )
    register_native_tools(registry, deps)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.DEFAULT),
    )

    result = executor.execute(
        ToolCall(
            name="risk_check",
            id="toolu_risk_top_level",
            arguments={
                "strategy_id": "s1",
                "account_id": "paper_main",
                "symbol": "BTC/USDT",
                "venue": "mock",
                "side": "buy",
                "size_pct_nav": "1.0",
                "max_size_pct_nav": "0.10",
                "order_type": "market",
                "confidence": "1.0",
                "market_snapshot": {"price": "50000", "age_s": "0", "source": "test"},
            },
        )
    )

    assert result.is_error is False, result
    out = result.content[0].data
    assert out["intent"]["market"] == "mock:BTC/USDT"
    assert out["risk_decision"]["decision"] == "reject"


def test_wallet_install_handler_treats_yolo_as_internal_approval(tmp_path, monkeypatch):
    approvals: list[bool] = []

    class FakeInstallResult:
        ok = True
        skipped = True

        def asdict(self):
            return {"ok": True, "skipped": True, "command": "fake"}

    def fake_install(_paths, _command, *, config_data=None, approve=False):
        approvals.append(bool(approve))
        return FakeInstallResult()

    monkeypatch.setattr("nerya.install.dep_installer.install", fake_install)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        config=Config(paths=WorkspacePaths(root=tmp_path)),
    )
    deps.permission_mode = "yolo"
    register_native_tools(registry, deps)
    descriptor = registry.get("wallet_install")

    result = descriptor.handler(
        ToolCall(
            name="wallet_install",
            arguments={"provider": "self_custody", "mode": "goat"},
        )
    )

    assert not result.is_error
    assert approvals == [True]


def test_registered_strategy_run_tick_auto_approves_explicit_paper_mode(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    _write_strategy_package(paths, "paper_cfg", mode="paper")
    _write_strategy_package(paths, "live_cfg", mode="live")
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        paths=paths,
        config=Config(paths=paths),
    )
    register_native_tools(registry, deps)
    descriptor = registry.get("strategy_run_tick")

    paper = _decision(
        descriptor,
        {"strategy_id": "s1", "mode_override": "paper"},
        PermissionMode.DEFAULT,
    )
    live = _decision(
        descriptor,
        {"strategy_id": "s1", "mode_override": "live"},
        PermissionMode.DEFAULT,
    )
    configured_paper = _decision(
        descriptor,
        {"strategy_id": "paper_cfg"},
        PermissionMode.DEFAULT,
    )
    configured_live = _decision(
        descriptor,
        {"strategy_id": "live_cfg"},
        PermissionMode.DEFAULT,
    )

    assert paper.is_allow()
    assert paper.reason == "auto_approve predicate"
    assert configured_paper.is_allow()
    assert configured_paper.reason == "auto_approve predicate"
    assert live.is_ask()
    assert live.requires_approval is True
    assert configured_live.is_ask()
    assert configured_live.requires_approval is True


def test_strategy_triggered_run_tick_auto_approves_live_tick(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        paths=paths,
        config=Config(paths=paths),
    )
    register_native_tools(registry, deps)
    descriptor = registry.get("strategy_run_tick")

    deps.active_strategy_id = "s1"
    deps.active_trigger_event_id = "evt_strategy_tick"
    deps.active_trigger_source = "scheduled_session"
    deps.active_trigger_kind = "strategy.tick"
    deps.strategy_order_auto_approve = True

    live = _decision(
        descriptor,
        {"strategy_id": "s1", "mode_override": "live"},
        PermissionMode.DEFAULT,
    )
    other_strategy = _decision(
        descriptor,
        {"strategy_id": "other", "mode_override": "live"},
        PermissionMode.DEFAULT,
    )

    assert live.is_allow()
    assert live.reason == "auto_approve predicate"
    assert other_strategy.is_ask()
    assert other_strategy.requires_approval is True


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
