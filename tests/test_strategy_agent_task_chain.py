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
from nerya.strategies.package import load_package
from nerya.strategies.scheduler_bridge import compile_trading_schedule
from nerya.tools.native.bootstrap import (
    _wrap_strategy_run_tick,
    _wrap_trade_intent_submit,
    build_native_tool_deps,
)
from nerya.tools.types import ToolCall, ToolErrorKind, ToolResult
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


def _write_legacy_team_strategy(cfg: Config) -> None:
    root = cfg.paths.strategy("amzn_daily_team_long")
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "amzn_daily_team_long",
            "title": "AMZN daily Agent Team long",
            "description": (
                "Legacy generated team strategy with direct subagent calls "
                "and fundamental analysis intent."
            ),
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:AMZN"],
            "accounts": ["paper_main"],
            "schedule": {"type": "cron", "cron": "0 14 * * 1-5"},
            "policy": {
                "max_single_order_usd": 120,
                "max_daily_notional_usd": 500,
                "max_open_positions": 1,
                "min_confidence": 0.7,
                "max_run_seconds": 5,
            },
            "llm_policy": {"default_tier": "medium", "allowed_tiers": ["medium"]},
            "subagents": [
                "technical_analyst",
                "fundamentals_analyst",
                "news_interpreter",
                "risk_critic",
            ],
        },
    )
    (root / "main.py").write_text(
        "\n".join(
            [
                "def run(ctx):",
                "    raise RuntimeError('legacy direct tick path should not run')",
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


def test_legacy_agent_team_schedule_targets_agent_task_and_builds_team_prompt(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    _write_legacy_team_strategy(cfg)
    package = load_package(cfg.paths, "amzn_daily_team_long")
    entry = compile_trading_schedule(package)
    fake = _FakeKernel()
    monkeypatch.setattr(
        "nerya.tools.native.agents.team_run_handler",
        lambda call, *, config, skills, tool_registry=None: ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "team_run_id": "team-default",
                "roles_requested": ["technical_analyst", "fundamentals_analyst"],
                "roles_succeeded": ["technical_analyst", "fundamentals_analyst"],
                "roles_failed": [],
                "results": [],
                "aggregated": {},
            },
        ),
    )
    monkeypatch.setattr(
        StrategyAgentTaskExecutor,
        "_team_context_payload",
        lambda self, package, event, task, task_id: {
            "strategy_id": package.strategy_id,
            "markets": list(package.manifest.markets),
            "timeframe": "1d",
            "data_policy": "live only in test",
        },
    )

    assert entry.target == TARGET
    assert entry.payload["agent_task"] is True

    runtime = TriggerRuntime(
        config=cfg,
        router=TriggerRuntime.boot(cfg).router,
        agent_task_executor_factory=lambda config: StrategyAgentTaskExecutor(
            config=config,
            kernel_factory=lambda _config: fake,
        ),
    )
    result = runtime.emit(
        runtime.from_payload(
            {
                "source": "schedule",
                "kind": "strategy.tick",
                "payload": dict(entry.payload),
                "target": entry.target,
                "strategy_id": "amzn_daily_team_long",
            }
        )
    )

    assert result.status == "executed"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["strategy_id"] == "amzn_daily_team_long"
    assert call["attached_skills"] == [
        "team",
        "trading",
        "market_research",
        "research",
        "market_data_routing",
    ]
    prompt = call["trigger"]["payload"]["text"]
    assert "team_run" in prompt
    assert "technical_analyst" in prompt
    assert "fundamentals_analyst" in prompt
    assert "Final response contract" in prompt
    assert result.result["actions"][0]["action"] == "team_run"
    profile = SessionStore(cfg.paths.root).load(call["session_id"]).meta[
        "strategy_agent_profile"
    ]["profile"]
    assert "team_run" in profile["allowed_tools"]
    assert "trade_intent_submit" in profile["allowed_tools"]


def test_agent_team_task_executes_required_team_run_before_final_decision(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    _write_legacy_team_strategy(cfg)
    package = load_package(cfg.paths, "amzn_daily_team_long")
    entry = compile_trading_schedule(package)
    fake = _FakeKernel()
    team_calls: list[ToolCall] = []

    def fake_team_run(call, *, config, skills, tool_registry=None):
        team_calls.append(call)
        assert call.arguments["strategy_id"] == "amzn_daily_team_long"
        assert call.arguments["shared_payload"]["data_policy"] == "live only in test"
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "team_run_id": "team-test",
                "roles_requested": ["technical_analyst", "risk_critic"],
                "roles_succeeded": ["technical_analyst", "risk_critic"],
                "roles_failed": [],
                "results": [
                    {
                        "subagent": "technical_analyst",
                        "ok": True,
                        "tier": "medium",
                        "steps": [{"prompt": "large raw trace should not be persisted"}],
                        "output": {"recommendation": "hold", "confidence": 0.4},
                    }
                ],
                "aggregated": {"recommendation": "hold"},
            },
        )

    monkeypatch.setattr(
        "nerya.tools.native.agents.team_run_handler",
        fake_team_run,
    )
    monkeypatch.setattr(
        StrategyAgentTaskExecutor,
        "_team_context_payload",
        lambda self, package, event, task, task_id: {
            "strategy_id": package.strategy_id,
            "markets": list(package.manifest.markets),
            "timeframe": "1d",
            "data_policy": "live only in test",
        },
    )

    runtime = TriggerRuntime(
        config=cfg,
        router=TriggerRuntime.boot(cfg).router,
        agent_task_executor_factory=lambda config: StrategyAgentTaskExecutor(
            config=config,
            kernel_factory=lambda _config: fake,
        ),
    )
    result = runtime.emit(
        runtime.from_payload(
            {
                "source": "schedule",
                "kind": "strategy.tick",
                "payload": dict(entry.payload),
                "target": entry.target,
                "strategy_id": "amzn_daily_team_long",
            }
        )
    )

    assert result.status == "executed"
    assert len(team_calls) == 1
    assert result.result["required_team_run"]["team_run_id"] == "team-test"
    assert result.result["actions"][0]["action"] == "team_run"
    assert result.result["actions"][0]["forced_by"] == "strategy_agent_task_executor"
    assert "steps" not in result.result["actions"][0]["result"]["results"][0]
    assert "shared_payload" in result.result["tool_trace"][0]["call"]["arguments"]
    prompt = fake.calls[0]["trigger"]["payload"]["text"]
    assert "runtime already executed the required `team_run`" in prompt
    assert "team-test" in prompt


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


def test_strategy_agent_run_tick_wrapper_pins_strategy_and_trigger(tmp_path):
    cfg = _config(tmp_path)
    _write_tick_strategy(cfg, "s1")
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
    )
    deps.active_strategy_id = "s1"
    deps.active_trigger_event_id = "evt_macd"
    deps.active_trigger_source = "scheduled_session"
    deps.strategy_order_auto_approve = True
    handler = _wrap_strategy_run_tick(deps)

    result = handler(
        ToolCall(
            name="strategy_run_tick",
            id="toolu_tick_other",
            arguments={"strategy_id": "other"},
        )
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert result.error.detail["requested_strategy_id"] == "other"

    result = handler(
        ToolCall(
            name="strategy_run_tick",
            id="toolu_tick",
            arguments={"strategy_id": "s1"},
        )
    )

    assert result.is_error is False
    out = result.content[0].data
    assert out["strategy_id"] == "s1"
    assert out["trigger_event_id"] == "evt_macd"
    assert out["inputs"]["operator"] == "scheduled_session"


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


def test_chat_trade_call_in_strategy_session_still_uses_domain_approval(tmp_path):
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
            "risk_limits": {"max_single_order_usd": 1},
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
    deps.strategy_order_auto_approve = False

    result = _wrap_trade_intent_submit(deps)(
        ToolCall(
            name="trade_intent_submit",
            id="toolu_trade_chat_strategy_context",
            arguments={
                "account_id": "paper_main",
                "market": "mock:BTC/USDT",
                "side": "buy",
                "size": 100,
                "size_unit": "usd",
                "order_type": "market",
                "confidence": 0.9,
                "market_snapshot": {"price": 50_000, "age_s": 0, "source": "test"},
                "meta": {"operator_text_locale": "zh-CN"},
            },
        )
    )

    assert result.is_error is False, result
    out = result.content[0].data
    assert out["status"] == "pending_approval"
    assert out["approval_id"]
    assert out["intent"]["strategy_id"] == "s1"
    assert out["intent"]["source"] == "agent:native"
    assert out["intent"]["meta"]["operator_text_locale"] == "zh-CN"
    assert jsonl.read_all(cfg.paths.approvals_approved) == []
    assert jsonl.read_all(cfg.paths.approvals_pending)[-1]["approval_id"] == out["approval_id"]


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
