"""Execute the exact skill snippets against the SDK; no network/model calls.

These are fixture branch checks, not Prompt-generation or profit evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import TradingError
from nerya.core.paths import WorkspacePaths
from nerya.strategies.context import StrategyState
from nerya.strategies.package import StrategySchedule, load_package
from nerya.strategies.prompt_io import StrategyPromptIO
from nerya.strategies.result import ResultBuilder
from nerya.strategies.scheduler_bridge import compile_trading_schedule, compile_tuning_schedule
from nerya.strategies.validator import validate_proposal_files
from nerya.strategies.workflow_graph import build_workflows
from nerya.triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor
from nerya.workspace.state_store import StateStore

pytestmark = pytest.mark.smoke
GUIDE = Path(__file__).parents[1] / "nerya/skills/builtin/strategy_author/references/workflows.md"
TEXT = GUIDE.read_text(encoding="utf-8")
BLOCKS = dict(re.findall(r"<!-- example:(\w+) -->\s*```python\n(.*?)\n```", TEXT, re.S))
BASE = 1_700_000_100


def source(kind):
    return (BLOCKS["common"] + "\n" if kind != "scheduled_agent" else "") + BLOCKS[kind] + "\n"


def manifest(kind):
    # Select executable manifests by their contract, not the ordinal position
    # of unrelated documentation snippets added above them.
    manifests = [yaml_io.loads(block) for block in re.findall(r"```yaml\n(.*?)\n```", TEXT, re.S)]
    schedule_type = "cron" if kind == "scheduled_agent" else "interval"
    raw = next(value for value in manifests if value.get("version") == 1 and value.get("schedule", {}).get("type") == schedule_type)
    raw["strategy_id"] = "skill_contract_" + kind
    raw["execution_mode"] = "script" if kind == "script" else "agent"
    raw["agent_task"] = {"enabled": kind != "script"}
    if kind == "macd_agent":
        raw.update(next(value for value in manifests if "agent_profile" in value and "version" not in value))
    return raw


def load_run(kind):
    scope = {}
    exec(compile(source(kind), str(GUIDE) + ":" + kind, "exec"), scope)
    return scope["run"]


def context(tmp_path, values, *, extra_open=None):
    calls = []
    forbidden = []
    rows = [{"ts": BASE + i * 900, "close": value} for i, value in enumerate(values)]
    now = BASE + len(values) * 900 + 1
    if extra_open is not None:
        rows.append({"ts": BASE + len(values) * 900, "close": extra_open})
    def candles(market, *, timeframe, limit):
        calls.append((market, timeframe, limit))
        return rows
    class Deny:
        def __getattr__(self, name):
            forbidden.append(name)
            raise AssertionError("Unexpected model/trade invocation: " + name)
    return SimpleNamespace(
        config=SimpleNamespace(markets=("BINANCE:BTCUSDT",), extras={"data_sources": deepcopy(manifest("script")["data_sources"])}),
        market=SimpleNamespace(candles=candles),
        clock=SimpleNamespace(now_ms=lambda: now * 1000, now_iso=lambda: datetime.fromtimestamp(now, timezone.utc).isoformat()),
        state=StrategyState(StateStore(tmp_path / "state.json")),
        prompt=StrategyPromptIO(strategy_root=tmp_path, run_id="fixture"), result=ResultBuilder(),
        llm=Deny(), trading=Deny(), subagents=Deny(), team=Deny(),
        calls=calls, forbidden=forbidden, rows=rows,
    )


@pytest.mark.parametrize("kind", ["script", "macd_agent", "scheduled_agent"])
def test_skill_code_validates_and_projects_real_cards(kind, tmp_path):
    root = tmp_path / "strategies" / manifest(kind)["strategy_id"]
    yaml_io.dump(root / "strategy.yml", manifest(kind))
    (root / "main.py").write_text(source(kind))
    (root / "strategy.md").write_text("Fixture contract: observe without orders; not a performance backtest.")
    files = {p.name: p.read_text() for p in root.iterdir()}
    report = validate_proposal_files(strategy_id=manifest(kind)["strategy_id"], files=files)
    assert report.ok, report.asdict()
    graph = build_workflows(files)["strategy"]
    ids = {n["id"] for n in graph["nodes"]}
    assert {"script:main.py", "source:data_sources/bars", "scheduler:trading"} <= ids
    assert ("agent:runtime" in ids) is (kind != "script")
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])
    compiled = compile_trading_schedule(load_package(WorkspacePaths(tmp_path), manifest(kind)["strategy_id"]))
    assert compiled.target == ("skill:strategy.run_tick" if kind == "script" else "skill:strategy.agent_task")
    assert compiled.enabled is False


def test_script_closed_bar_dedupe_and_no_ai(tmp_path):
    run = load_run("script")
    ctx = context(tmp_path, [100] * 19 + [110], extra_open=1)
    first = run(ctx)
    assert first.reason == "观察提醒：收盘价高于均线"
    assert first.metadata["close"] == 110
    ctx.state = StrategyState(StateStore(tmp_path / "state.json"))
    assert run(ctx).reason == "duplicate_bar"
    assert ctx.forbidden == []
    assert ctx.calls[0] == ("BINANCE:BTCUSDT", "15m", 160)


def test_source_card_parameter_edit_changes_actual_logic(tmp_path):
    run = load_run("script")
    ctx = context(tmp_path, [100] * 18 + [50, 90])
    assert run(ctx).reason == "below_sma"
    ctx.config.extras["data_sources"][0]["parameters"]["sma_window"] = 2
    ctx.config.extras["data_sources"][0]["limit"] = 80
    assert run(ctx).reason == "观察提醒：收盘价高于均线"
    assert ctx.calls[-1][-1] == 80
    assert not ctx.forbidden


def test_macd_no_signal_and_open_signal_do_not_dispatch(tmp_path):
    run = load_run("macd_agent")
    ctx = context(tmp_path, [100] * 120, extra_open=110)
    result = run(ctx)
    assert result.status == "skip" and result.reason == "no_golden_cross"
    assert not result.prompt and not ctx.forbidden


def test_macd_closed_signal_dispatches_once_across_state_reload(tmp_path):
    run = load_run("macd_agent")
    ctx = context(tmp_path, [100] * 120 + [110])
    first = run(ctx)
    assert first.status == "dispatch"
    assert first.metadata["histogram"][0] <= 0 < first.metadata["histogram"][1]
    ctx.state = StrategyState(StateStore(tmp_path / "state.json"))
    assert run(ctx).reason == "duplicate_bar"
    assert ctx.forbidden == []


@pytest.mark.parametrize("kind", ["script", "macd_agent"])
@pytest.mark.parametrize("fault", ["empty", "short", "stale", "gap", "nan", "provider", "parameters", "exception"])
def test_bad_inputs_never_call_model_or_trade(kind, fault, tmp_path):
    ctx = context(tmp_path, [100] * 120 + [110])
    if fault == "empty":
        ctx.rows.clear()
    elif fault == "short":
        ctx.rows[:] = ctx.rows[-2:]
    elif fault == "stale":
        for row in ctx.rows:
            row["ts"] -= 86400
    elif fault == "gap":
        ctx.rows.pop(20)
    elif fault == "nan":
        ctx.rows[-1]["close"] = float("nan")
    elif fault == "provider":
        ctx.config.extras["data_sources"][0]["provider"] = "unknown"
    elif fault == "parameters":
        ctx.config.extras["data_sources"][0]["parameters"] = {"sma_window": -1, "fast": 26, "slow": 12, "signal": 9}
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("source offline")
        ctx.market.candles = fail
    result = load_run(kind)(ctx)
    assert result.status != "dispatch"
    assert result.reason != "观察提醒：收盘价高于均线"
    assert not ctx.forbidden


def test_scheduled_adapter_does_not_prefilter(tmp_path):
    ctx = context(tmp_path, [])
    result = load_run("scheduled_agent")(ctx)
    assert result.status == "dispatch"
    assert result.metadata["sources"] == ctx.config.extras["data_sources"]
    assert not ctx.calls and not ctx.forbidden


@pytest.mark.parametrize("zone,date,hour", [("Asia/Shanghai", "2026-09-12", 1), ("America/New_York", "2026-01-12", 14), ("America/New_York", "2026-07-12", 13)])
def test_manifest_timezone_survives_roundtrip_and_compilation(zone, date, hour, tmp_path):
    raw = manifest("scheduled_agent")
    raw["schedule"] = {"type": "cron", "cron": "0 9 * * *", "timezone": zone, "enabled": True}
    root = tmp_path / "strategies" / raw["strategy_id"]
    yaml_io.dump(root / "strategy.yml", raw)
    (root / "main.py").write_text(source("scheduled_agent"))
    package = load_package(WorkspacePaths(tmp_path), raw["strategy_id"])
    assert StrategySchedule.from_dict(package.manifest.schedule.asdict(), where="test").timezone == zone
    entry = compile_trading_schedule(package)
    when = datetime.fromisoformat(f"{date}T{hour:02d}:00:00+00:00")
    assert entry.is_due(now=when, last_fired=None)
    assert not entry.is_due(now=when.replace(hour=(hour + 1) % 24), last_fired=None)
    assert not entry.is_due(now=when, last_fired=when)
    tuning = replace(package.manifest.tuning, enabled=True, schedule=package.manifest.schedule)
    reviewed = replace(package, manifest=replace(package.manifest, tuning=tuning))
    assert compile_tuning_schedule(reviewed).timezone == zone


def test_missing_timezone_legacy_and_invalid_timezone():
    raw = {"type": "cron", "cron": "0 9 * * *"}
    assert "timezone" not in StrategySchedule.from_dict(raw, where="test").asdict()
    with pytest.raises(TradingError, match="timezone"):
        StrategySchedule.from_dict({**raw, "timezone": "not/a/zone"}, where="test")


def test_explicit_readonly_agent_profile_does_not_gain_trading_tools(tmp_path):
    raw = manifest("scheduled_agent")
    root = tmp_path / "strategies" / raw["strategy_id"]
    yaml_io.dump(root / "strategy.yml", raw)
    (root / "main.py").write_text(source("scheduled_agent"))
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    profile = StrategyAgentTaskExecutor(cfg)._profile_for(load_package(cfg.paths, raw["strategy_id"]))
    assert profile["allowed_tools"] == ["market_data"]
    assert profile["attached_skills"] == ["markets"]
    assert profile["role"] == raw["agent_profile"]["role"]
    assert not any("team_run" in rule for rule in profile["order_rules"])


def test_backtest_prompt_formatting_matches_sdk_but_denies_artifact_writes(tmp_path):
    from nerya.skills.builtin.backtest.scripts.mock_ctx import BacktestPromptIO, BacktestUnsupportedSurfaceError
    formatter = BacktestPromptIO()
    sdk = StrategyPromptIO(strategy_root=tmp_path, run_id="fixture")
    rows = [{"market": "BTC", "close": 100}]
    for method in ("csv", "markdown_table", "json_block", "truncate_csv"):
        assert getattr(formatter, method)(rows) == getattr(sdk, method)(rows)
    with pytest.raises(BacktestUnsupportedSurfaceError, match="prompt.artifact"):
        formatter.artifact("not-written.txt", "fixture")
    assert not list(tmp_path.iterdir())


def test_scheduled_skill_adapter_can_replay_without_running_agent_or_orders():
    from nerya.skills.builtin.backtest.scripts.config import load_config
    from nerya.skills.builtin.backtest.scripts.engine import run_backtest
    cfg = load_config(preset="default", markets=["BINANCE:BTCUSDT"], overrides={"tf": "1d", "window_days": 30, "warmup_bars": 2})
    bars = [{"ts": BASE + i * 86400, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10} for i in range(10)]
    result = run_backtest(None, cfg, candles_by_market={"BINANCE:BTCUSDT": bars}, run_fn=load_run("scheduled_agent"), strategy_config=manifest("scheduled_agent"))
    assert result.decisions, "The fixture must actually exercise the adapter"
    assert result.trades == [] and result.rejected_signals == []


def test_authoring_loop_keeps_workspace_identity_without_changing_budgets(tmp_path):
    from nerya.agent.kernel import _loop_config_from_config
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    loop = _loop_config_from_config(cfg)
    assert loop.workspace_root == str(tmp_path)
    assert loop.max_total_tool_calls == cfg.get("agent.native.max_total_tool_calls")
    assert loop.max_wall_seconds == cfg.get("agent.native.max_wall_seconds")
