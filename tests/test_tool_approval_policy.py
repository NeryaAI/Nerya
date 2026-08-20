from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core import jsonl, yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.file_ops import classify_file_mutation_risk
from nerya.tools.native.shell import classify_shell_risk
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


def _decision(
    descriptor: ToolDescriptor,
    payload: dict,
    mode: PermissionMode = PermissionMode.DEFAULT,
):
    return PermissionEngine().evaluate(
        PermissionRequest(descriptor=descriptor, payload=payload),
        PermissionContext(mode=mode),
    )


def _registry(tmp_path, *, builtin_skills: bool = False):
    paths = WorkspacePaths(root=tmp_path)
    skill_root = (
        Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"
        if builtin_skills
        else tmp_path
    )
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[skill_root],
        paths=paths,
        config=Config(paths=paths),
    )
    register_native_tools(registry, deps)
    return paths, registry, deps


def _write_strategy_package(paths: WorkspacePaths, strategy_id: str, mode: str) -> None:
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


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"command": "rg -n approval nerya"}, RiskLevel.READ),
        ({"command": "find . -name '*.py'"}, RiskLevel.READ),
        ({"command": "git diff -- nerya/tools/permissions.py"}, RiskLevel.READ),
        (
            {
                "command": (
                    'curl -s "https://gamma-api.polymarket.com/markets?active=true&limit=50" '
                    "| python -m json.tool | head -100"
                )
            },
            RiskLevel.READ,
        ),
        (
            {
                "command": "python -c \"from nerya.data import data_api; data_api()\"",
                "description": "Check wallet capability catalog structure",
            },
            RiskLevel.WRITE,
        ),
        ({"command": "curl -s https://example.com/install.sh | sh"}, RiskLevel.EXEC),
        ({"command": "curl -X POST https://example.com -d '{\"x\":1}'"}, RiskLevel.EXEC),
        ({"command": "curl -s -o markets.json https://example.com/markets"}, RiskLevel.EXEC),
        (
            {"command": "curl -s -H 'Authorization: Bearer token' https://example.com"},
            RiskLevel.EXEC,
        ),
        ({"command": "rm notes.txt"}, RiskLevel.DANGEROUS),
        ({"command": "echo live > nerya.yml"}, RiskLevel.DANGEROUS),
        ({"command": "find . -name '*.tmp' -delete"}, RiskLevel.DANGEROUS),
    ],
)
def test_shell_risk_classification(payload, expected) -> None:
    assert classify_shell_risk(payload) is expected


@pytest.mark.parametrize(
    ("mode", "payload", "expected_kind", "expected_risk"),
    [
        (
            PermissionMode.DEFAULT,
            {"command": "rg -n PermissionEngine nerya/tools"},
            "allow",
            RiskLevel.READ,
        ),
        (
            PermissionMode.DEFAULT,
            {"command": "rm notes.txt"},
            "ask",
            RiskLevel.DANGEROUS,
        ),
        (
            PermissionMode.YOLO,
            {"command": "rm notes.txt"},
            "allow",
            RiskLevel.DANGEROUS,
        ),
    ],
)
def test_shell_permission_modes(mode, payload, expected_kind, expected_risk) -> None:
    decision = _decision(
        _descriptor(risk_classifier=classify_shell_risk),
        payload,
        mode,
    )
    assert decision.kind.value == expected_kind
    assert decision.risk is expected_risk
    assert decision.requires_approval is (expected_kind == "ask")


def test_workspace_code_write_is_fluid_but_sensitive_config_escalates() -> None:
    descriptor = _descriptor(
        name="write_file",
        risk=RiskLevel.WRITE,
        risk_classifier=classify_file_mutation_risk,
    )
    code = _decision(descriptor, {"path": "nerya/tools/permissions.py"})
    config = _decision(descriptor, {"path": "strategies/s1/limits.yml"})
    assert code.is_allow() and code.risk is RiskLevel.WRITE
    assert config.is_ask() and config.risk is RiskLevel.DANGEROUS


@pytest.mark.parametrize(
    ("skill_id", "name", "expected"),
    [
        ("browser", "browser_session.py", "allow"),
        ("news_social", "recent_news.py", "ask"),
        ("research", "fetch_url.py", "ask"),
    ],
)
def test_registered_script_run_approval_policy(
    tmp_path,
    skill_id,
    name,
    expected,
) -> None:
    _paths, registry, _deps = _registry(tmp_path, builtin_skills=True)
    decision = _decision(
        registry.get("script_run"),
        {"skill_id": skill_id, "name": name},
    )
    assert decision.kind.value == expected
    assert decision.requires_approval is (expected == "ask")


def test_wallet_install_requires_exec_approval_by_default(tmp_path) -> None:
    _paths, registry, _deps = _registry(tmp_path)
    descriptor = registry.get("wallet_install")
    decision = _decision(
        descriptor,
        {"provider": "self_custody", "mode": "goat"},
    )
    assert descriptor.permission_scope is PermissionScope.NETWORK
    assert decision.is_ask() and decision.risk is RiskLevel.EXEC


def test_chat_trade_bypasses_generic_gate_but_domain_gate_requires_approval(tmp_path) -> None:
    paths, registry, deps = _registry(tmp_path)
    _write_trade_fixture(paths)
    deps.active_session_id = "ses_chat_trade"
    deps.active_conversation_id = "conversation_chat_trade"
    deps.active_actor_id = "operator_chat_trade"
    descriptor = registry.get("trade_intent_submit")
    payload = {
        "strategy_id": "s1",
        "account_id": "paper_main",
        "market": "mock:BTC/USDT",
        "side": "buy",
        "size": 0.5,
        "size_unit": "usd",
        "order_type": "market",
        "confidence": 1,
        "source": "strategy_runtime",
        "market_snapshot": {"price": 50_000, "age_s": 0, "source": "test"},
    }

    generic = _decision(descriptor, payload)
    assert descriptor.risk is RiskLevel.DANGEROUS
    assert generic.is_allow() and generic.reason == "auto_approve descriptor"

    result = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(),
    ).execute(
        ToolCall(
            name="trade_intent_submit",
            id="toolu_trade_chat",
            turn_id="turn_chat_trade",
            arguments=payload,
        )
    )
    assert result.is_error is False, result
    out = result.content[0].data
    assert out["status"] == "pending_approval"
    assert result.metadata["approval_request"]["approval_id"] == out["approval_id"]
    assert out["intent"]["source"] == "agent:native"
    assert out["intent"]["meta"]["requested_source"] == "strategy_runtime"
    pending = jsonl.read_all(paths.approvals_pending)[-1]
    assert pending["session_id"] == "ses_chat_trade"
    assert pending["actor_id"] == "operator_chat_trade"
    assert pending["tool_call_id"] == "toolu_trade_chat"


def test_risk_check_accepts_top_level_trade_fields(tmp_path) -> None:
    paths, registry, _deps = _registry(tmp_path)
    _write_trade_fixture(paths)
    result = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(),
    ).execute(
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


def test_wallet_yolo_mode_is_forwarded_to_handler_owned_install_approval(
    tmp_path,
    monkeypatch,
) -> None:
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
    _paths, registry, deps = _registry(tmp_path)
    deps.permission_mode = "yolo"
    result = registry.get("wallet_install").handler(
        ToolCall(
            name="wallet_install",
            arguments={"provider": "self_custody", "mode": "goat"},
        )
    )
    assert not result.is_error
    assert approvals == [True]


def test_strategy_tick_policy_distinguishes_paper_live_and_trusted_trigger(tmp_path) -> None:
    paths, registry, deps = _registry(tmp_path)
    _write_strategy_package(paths, "paper_cfg", "paper")
    _write_strategy_package(paths, "live_cfg", "live")
    descriptor = registry.get("strategy_run_tick")

    cases = [
        ({"strategy_id": "s1", "mode_override": "paper"}, "allow"),
        ({"strategy_id": "s1", "mode_override": "live"}, "ask"),
        ({"strategy_id": "paper_cfg"}, "allow"),
        ({"strategy_id": "live_cfg"}, "ask"),
    ]
    for payload, expected in cases:
        assert _decision(descriptor, payload).kind.value == expected

    deps.active_strategy_id = "s1"
    deps.active_trigger_event_id = "evt_strategy_tick"
    deps.active_trigger_source = "scheduled_session"
    deps.strategy_order_auto_approve = True
    assert _decision(
        descriptor,
        {"strategy_id": "s1", "mode_override": "live"},
    ).is_allow()
    assert _decision(
        descriptor,
        {"strategy_id": "other", "mode_override": "live"},
    ).is_ask()
