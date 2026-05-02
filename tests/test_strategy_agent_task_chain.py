from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.agent.session import SessionStore
from nerya.agent.session_profile import ensure_strategy_agent_profile
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.sdk.trigger_api import TriggerAPI
from nerya.sdk.strategy_api import StrategyAPI
from nerya.tools.native.bootstrap import build_native_tool_deps, _wrap_trade_intent_submit
from nerya.tools.types import ToolCall, ToolErrorKind
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.strategy_agent_task_executor import (
    TARGET,
    StrategyAgentTaskExecutor,
)


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
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
    return cfg


def _write_legacy_trade_strategy(cfg: Config, strategy_id: str = "s1") -> None:
    yaml_io.dump(
        cfg.paths.strategy(strategy_id) / "strategy.yml",
        {
            "id": strategy_id,
            "status": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy(strategy_id) / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0,
            "max_stale_seconds": 60,
            "approval_threshold_usd": 1,
        },
    )


def _write_agent_task_strategy(cfg: Config) -> None:
    root = cfg.paths.strategy("macd_agent")
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "macd_agent",
            "title": "MACD Agent Strategy",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "cron", "cron": "*/5 * * * *"},
            "policy": {
                "max_single_order_usd": 120,
                "max_daily_notional_usd": 500,
                "max_open_positions": 1,
                "min_confidence": 0.7,
                "max_run_seconds": 5,
            },
            "llm_policy": {"default_tier": "light", "allowed_tiers": ["light"]},
            "agent_session": {"policy": "per_strategy_market_timeframe"},
            "agent_profile": {
                "title": "MACD execution agent",
                "role": "Analyze strategy-built MACD prompts and submit guarded orders.",
                "order_rules": [
                    "Only trade when the prompt says macd_cross=golden.",
                    "Use paper_main and mock:BTC/USDT for this strategy.",
                ],
                "allowed_tools": [
                    "portfolio_summary",
                    "risk_check",
                    "trade_intent_submit",
                ],
                "attached_skills": ["trading"],
            },
        },
    )
    (root / "main.py").write_text(
        "\n".join(
            [
                "from nerya.strategies.agent_task import StrategyAgentTask",
                "",
                "def run(ctx):",
                "    rows = [",
                "        {'ts': 1, 'close': 100, 'macd': -0.3, 'signal': -0.2, 'macd_cross': 'none', 'custom_factor': 0.1},",
                "        {'ts': 2, 'close': 101, 'macd': 0.4, 'signal': 0.1, 'macd_cross': 'golden', 'custom_factor': 0.92},",
                "    ]",
                "    csv_text = ctx.prompt.csv(rows, columns=['ts', 'close', 'macd', 'signal', 'macd_cross', 'custom_factor'])",
                "    prompt = '\\n'.join([",
                "        'Strategy task: analyze MACD golden cross and trade if rules pass.',",
                "        'market=mock:BTC/USDT timeframe=1m account=paper_main',",
                "        'CSV:',",
                "        csv_text,",
                "    ])",
                "    return StrategyAgentTask.dispatch(",
                "        prompt=prompt,",
                "        session_key={'market': 'mock:BTC/USDT', 'timeframe': '1m'},",
                "        metadata={'market': 'mock:BTC/USDT', 'timeframe': '1m', 'signal': 'macd_golden_cross'},",
                "    )",
            ]
        ),
        encoding="utf-8",
    )


def _write_tick_strategy(cfg: Config, strategy_id: str = "cron_tick") -> None:
    root = cfg.paths.strategy(strategy_id)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": strategy_id,
            "title": "Cron Tick Strategy",
            "mode": "paper",
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
        "\n".join(
            [
                "def run(ctx):",
                "    return ctx.result.hold(reason='scheduled tick executed')",
            ]
        ),
        encoding="utf-8",
    )


class _FakeKernel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            turn_id=f"turn_{len(self.calls)}",
            decision={"action": "send_message", "text": "ok"},
            actions=[{"action": "send_message", "ok": True}],
            tool_trace=[],
            stopped_reason="end_turn",
        )


def test_trigger_runtime_dispatches_strategy_built_prompt_to_stable_agent_session(tmp_path):
    cfg = _config(tmp_path)
    _write_agent_task_strategy(cfg)
    yaml_io.dump(
        cfg.paths.triggers_routes_file,
        {
            "routes": [
                {
                    "id": "macd_agent_task",
                    "match": {"kind": "price.macd_cross"},
                    "target": TARGET,
                    "strategy_id": "macd_agent",
                }
            ]
        },
    )
    fake = _FakeKernel()
    runtime = TriggerRuntime(
        config=cfg,
        router=TriggerRuntime.boot(cfg).router,
        agent_task_executor_factory=lambda config: StrategyAgentTaskExecutor(
            config=config,
            kernel_factory=lambda _config: fake,
        ),
    )

    first = runtime.emit(
        runtime.from_payload(
            {
                "source": "price",
                "kind": "price.macd_cross",
                "payload": {"market": "mock:BTC/USDT", "timeframe": "1m"},
                "target": TARGET,
                "strategy_id": "macd_agent",
            }
        )
    )
    second = runtime.emit(
        runtime.from_payload(
            {
                "source": "price",
                "kind": "price.macd_cross",
                "payload": {"market": "mock:BTC/USDT", "timeframe": "1m"},
                "target": TARGET,
                "strategy_id": "macd_agent",
            }
        )
    )

    assert first.status == "executed"
    assert second.status == "executed"
    assert len(fake.calls) == 2
    assert fake.calls[0]["session_id"] == fake.calls[1]["session_id"]
    assert fake.calls[0]["strategy_id"] == "macd_agent"
    assert fake.calls[0]["attached_skills"] == ["trading"]

    prompt = fake.calls[0]["trigger"]["payload"]["text"]
    assert "macd_cross" in prompt
    assert "golden" in prompt
    assert "custom_factor" in prompt
    assert "Strategy task: analyze MACD golden cross" in prompt

    session = SessionStore(cfg.paths.root).load(fake.calls[0]["session_id"])
    assert session is not None
    profile = session.meta["strategy_agent_profile"]["profile"]
    assert profile["risk_limits"]["max_single_order_usd"] == 120
    assert profile["accounts"] == ["paper_main"]
    assert profile["markets"] == ["mock:BTC/USDT"]

    task_rows = jsonl.read_all(cfg.paths.strategy_history("macd_agent") / "agent_tasks.jsonl")
    assert len(task_rows) == 2
    artifact = cfg.paths.strategy("macd_agent") / task_rows[0]["task"]["prompt_artifact"]
    assert artifact.read_text(encoding="utf-8").replace("\r\n", "\n") == prompt.replace("\r\n", "\n")

    strategy_api = StrategyAPI(config=cfg, skills=None)  # type: ignore[arg-type]
    listed = strategy_api.agent_tasks("macd_agent")
    assert listed["count"] == 2
    detail = strategy_api.agent_task("macd_agent", first.task_id)
    assert detail["ok"] is True
    assert "custom_factor" in detail["prompt"]
    assert detail["session"]["profile"]["profile"]["title"] == "MACD execution agent"
    assert strategy_api.history("macd_agent")["ledgers"]["agent_tasks"]["count"] == 2


def test_trigger_runtime_executes_strategy_run_tick_targets(tmp_path):
    cfg = _config(tmp_path)
    _write_tick_strategy(cfg)
    runtime = TriggerRuntime.boot(cfg)

    result = runtime.emit(
        runtime.from_payload(
            {
                "source": "schedule",
                "kind": "strategy.tick",
                "payload": {
                    "strategy_id": "cron_tick",
                    "mode": "paper",
                    "reason": "cron",
                },
                "target": "skill:strategy.run_tick",
                "strategy_id": "cron_tick",
            }
        )
    )

    assert result.status == "executed"
    assert result.strategy_id == "cron_tick"
    assert result.result["status"] == "hold"
    assert result.result["reason"] == "scheduled tick executed"
    assert result.result["trigger_event_id"] == result.event_id
    assert len(list((cfg.paths.strategy("cron_tick") / "runs").glob("*.json"))) == 1


def test_strategy_agent_trade_wrapper_pins_strategy_source_and_trigger_context(tmp_path):
    cfg = _config(tmp_path)
    _write_legacy_trade_strategy(cfg)
    ensure_strategy_agent_profile(
        paths=cfg.paths,
        session_id="strat_agent_test",
        strategy_id="s1",
        profile={
            "allowed_tools": ["trade_intent_submit"],
            "accounts": ["paper_main"],
            "markets": ["mock:BTC/USDT"],
            "min_confidence_to_trade": 0.6,
            "risk_limits": {"max_single_order_usd": 150},
        },
        session_key={"market": "mock:BTC/USDT"},
        policy="per_strategy",
    )
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
    )
    deps.active_strategy_id = "s1"
    deps.active_session_id = "strat_agent_test"
    deps.active_trigger_event_id = "evt_macd"
    deps.strategy_order_auto_approve = True

    result = _wrap_trade_intent_submit(deps)(
        ToolCall(
            name="trade_intent_submit",
            id="toolu_trade",
            arguments={
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "buy",
                "size": 100,
                "size_unit": "usd",
                "order_type": "market",
                "confidence": 0.9,
                "market_snapshot": {"price": 50_000, "age_s": 0, "source": "test"},
            },
        )
    )

    assert result.is_error is False
    out = result.content[0].data
    assert out["status"] == "filled"
    approved = jsonl.read_all(cfg.paths.approvals_approved)
    assert approved[-1]["intent"]["strategy_id"] == "s1"
    assert approved[-1]["intent"]["source"] == "strategy_agent"
    assert approved[-1]["intent"]["trigger_event_id"] == "evt_macd"
    assert approved[-1]["intent"]["meta"]["agent_session_id"] == "strat_agent_test"


def test_strategy_agent_trade_wrapper_rejects_profile_violations(tmp_path):
    cfg = _config(tmp_path)
    _write_legacy_trade_strategy(cfg)
    ensure_strategy_agent_profile(
        paths=cfg.paths,
        session_id="strat_agent_test",
        strategy_id="s1",
        profile={
            "allowed_tools": ["risk_check"],
            "accounts": ["paper_main"],
            "markets": ["mock:BTC/USDT"],
            "risk_limits": {"max_single_order_usd": 50, "min_confidence": 0.8},
        },
        session_key={},
        policy="per_strategy",
    )
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
    )
    deps.active_strategy_id = "s1"
    deps.active_session_id = "strat_agent_test"
    deps.strategy_order_auto_approve = True
    handler = _wrap_trade_intent_submit(deps)

    result = handler(
        ToolCall(
            name="trade_intent_submit",
            id="toolu_blocked",
            arguments={
                "strategy_id": "other",
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "buy",
                "size": 100,
                "size_unit": "usd",
                "order_type": "market",
                "confidence": 0.9,
            },
        )
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert result.error.detail["requested_strategy_id"] == "other"

    result = handler(
        ToolCall(
            name="trade_intent_submit",
            id="toolu_tool_blocked",
            arguments={
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "buy",
                "size": 10,
                "size_unit": "usd",
                "order_type": "market",
                "confidence": 0.9,
            },
        )
    )
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert "does not allow" in result.error.message


def test_trigger_api_updates_the_requested_schedule_not_the_tail(tmp_path):
    cfg = _config(tmp_path)
    runtime = TriggerRuntime.boot(cfg)
    api = TriggerAPI(config=cfg, runtime=runtime)
    api.add_schedule(id="first", kind="alpha.tick", every_seconds=60)
    api.add_schedule(id="second", kind="beta.tick", every_seconds=120)

    updated = api.update_schedule(id="first", every_seconds=30)

    assert updated["ok"] is True
    assert updated["schedule"]["id"] == "first"
    assert updated["schedule"]["every_seconds"] == 30
    schedules = {row["id"]: row for row in api.list_schedules()}
    assert schedules["first"]["every_seconds"] == 30
    assert schedules["second"]["every_seconds"] == 120
