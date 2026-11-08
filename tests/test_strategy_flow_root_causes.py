"""Capability and presentation regressions; no network or account orders."""
import json
import csv

import pytest

from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall
from nerya.skills.builtin.backtest.scripts.render_chart import (
    render_chart, _workspace_root_from_backtest_dir,
)

pytestmark = pytest.mark.smoke


def test_explicit_skill_asset_is_not_silently_ignored_on_load(tmp_path):
    base = tmp_path / "skills" / "flow_test"
    (base / "references").mkdir(parents=True)
    (base / "SKILL.md").write_text("---\nname: flow_test\ndescription: Test\n---\nMAIN_SENTINEL\n")
    reference = base / "references" / "example.md"
    reference.write_text("REFERENCE_SENTINEL\n")
    index = SkillIndex([base.parent])
    result = skill_tool_handler(ToolCall(name="Skill", arguments={
        "action": "load", "skill": "flow_test", "file": "references/example.md",
    }), skill_index=index)
    assert not result.is_error, result.text()
    assert "REFERENCE_SENTINEL" in result.text()
    assert "MAIN_SENTINEL" not in result.text()
    assert result.content[1].data["path"] == str(reference)


def test_proposal_chart_uses_actual_workspace_store(tmp_path):
    (tmp_path / "nerya.yml").write_text("{}\n")
    run = tmp_path / "evolution/proposals/prp_example/after/strategies/example/backtests/20260927_000000"
    run.mkdir(parents=True)
    assert _workspace_root_from_backtest_dir(run) == tmp_path


def test_full_year_report_keeps_all_candles_and_fills(tmp_path):
    run = tmp_path / "strategies/example/backtests/20260927_000000"
    run.mkdir(parents=True)
    (tmp_path / "nerya.yml").write_text("{}\n")
    start = 1735689600  # 2025-01-01 UTC, deterministic fixture.
    rows = [{"ts": start + i * 86400, "market": "MOCK:ASSET", "open": 100,
             "high": 102, "low": 99, "close": 101, "volume": 10, "equity": 10000} for i in range(365)]
    trades = [{"ts": start + (i // 2) * 86400, "market": "MOCK:ASSET", "side": "buy" if i % 2 == 0 else "sell",
               "price": 100, "qty": 1, "fee": 0, "reason": "fixture", "trade_id": f"fill-{i}"} for i in range(730)]
    for filename, records in [("ohlcv_indicators_portfolio.csv", rows), ("trades.csv", trades)]:
        with (run / filename).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0])); writer.writeheader(); writer.writerows(records)
    (run / "metrics.json").write_text(json.dumps({"markets": ["MOCK:ASSET"], "tf": "1d", "initial_capital_usd": 10000,
        "start_utc": "2025-01-01T00:00:00Z", "end_utc": "2025-12-31T00:00:00Z", "requested_window_days": 365,
        "requested_window_complete": True, "performance_evidence": False, "verdict": "WARN"}))
    chart = render_chart(run)
    table = next(t for t in chart["tables"] if t["id"] == "trades")
    assert len(table["rows"]) == 730
    assert chart["meta"]["trades_displayed"] == 730
    candles = next(s["data"] for p in chart["panels"] for s in p["series"] if s["kind"] == "candles")
    assert len(candles) == 365
    assert candles[0]["time"] == start
    assert candles[-1]["time"] == start + 364 * 86400
    assert chart["meta"]["requested_window_complete"] is True
    from nerya.skills.builtin.backtest.scripts.chart_artifacts import hydrate_market_details
    chart["meta"]["visualization_revision"] = 2
    table["rows"] = table["rows"][:500]
    original = (run / "metrics.json").read_bytes()
    upgraded = hydrate_market_details(chart, run)
    assert len(next(t for t in upgraded["tables"] if t["id"] == "trades")["rows"]) == 730
    assert upgraded["meta"]["requested_window_complete"] is True
    assert upgraded["panels"] == chart["panels"]
    assert original == (run / "metrics.json").read_bytes()


def test_one_call_settings_are_validated_and_saved_without_a_config_file(tmp_path, monkeypatch):
    from nerya.core import yaml_io
    from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest
    root = tmp_path / "strategies" / "inline_settings"
    root.mkdir(parents=True)
    yaml_io.dump(tmp_path / "nerya.yml", {"runtime": {"live_trading_enabled": False}})
    yaml_io.dump(root / "strategy.yml", {"version": 1, "strategy_id": root.name,
        "title": "Settings fixture", "mode": "paper", "entrypoint": "main.py:run",
        "markets": ["BINANCE:BTCUSDT"], "accounts": ["fixture"], "execution_mode": "script",
        "schedule": {"type": "none", "enabled": False}, "policy": {"allow_direct_order": True}})
    (root / "main.py").write_text("def run(ctx):\n    return ctx.result.hold(reason='fixture')\n")
    def forbidden(*a, **kw):
        pytest.fail("static preflight must not download data")
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run.get_candles", forbidden)
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run._discover_strategy_timeframes", lambda _: ["1d"])
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run._package_looks_short_lived", lambda _: True)
    result = run_strategy_backtest(strategy_id=root.name, workspace=tmp_path, preflight_only=True,
        settings={"window_days": 365, "tf": "1h", "warmup_bars": 100, "max_open_trades": 1})
    assert result["ok"]
    assert result["config"]["window_days"] == 365
    assert result["config"]["tf"] == "1h"
    assert result["config"]["coverage_policy"] == "allow_partial"
    assert result["config"]["max_open_trades"] == 1
    assert not (tmp_path / "backtest.yml").exists()
    daily = run_strategy_backtest(strategy_id=root.name, workspace=tmp_path, preflight_only=True,
        settings={"window_days": 365, "coverage_policy": "strict"})
    assert daily["config"]["coverage_policy"] == "strict"
    assert daily["config"]["tf"] == "1d", "an omitted timeframe still inherits the strategy"
    assert daily["config"]["window_days"] == 365
    with pytest.raises(ValueError):
        run_strategy_backtest(strategy_id=root.name, workspace=tmp_path, preflight_only=True,
            settings={"unknown_typo": 365})


def test_candidate_backtest_defaults_are_inherited_and_explicit_settings_win(tmp_path, monkeypatch):
    from nerya.core import yaml_io
    from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest
    root = tmp_path / "strategies" / "candidate_defaults"
    root.mkdir(parents=True)
    yaml_io.dump(tmp_path / "nerya.yml", {"runtime": {"live_trading_enabled": False}})
    yaml_io.dump(root / "strategy.yml", {
        "version": 1,
        "strategy_id": root.name,
        "title": "Candidate defaults fixture",
        "mode": "paper",
        "entrypoint": "main.py:run",
        "markets": ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"],
        "accounts": ["fixture"],
        "execution_mode": "script",
        "schedule": {"type": "none", "enabled": False},
        "policy": {"allow_direct_order": True, "max_open_positions": 7},
        "backtest": {
            "window_days": 365,
            "tf": "4h",
            "warmup_bars": 200,
            "fee_bps_by_venue": {"BINANCE": 10},
            "slip_bps_by_venue": {"BINANCE": 5},
            "coverage_policy": "strict",
            "data_mode": "local",
        },
    })
    (root / "main.py").write_text("def run(ctx):\n    return ctx.result.hold(reason='fixture')\n")
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run._discover_strategy_timeframes", lambda _: ["4h"])

    inherited = run_strategy_backtest(
        strategy_id=root.name,
        workspace=tmp_path,
        preflight_only=True,
    )
    cfg = inherited["config"]
    assert cfg["window_days"] == 365
    assert cfg["tf"] == "4h"
    assert cfg["warmup_bars"] == 200
    assert cfg["max_open_trades"] == 7, "policy.max_open_positions seeds replay concurrency"
    assert cfg["fee_bps_by_venue"]["BINANCE"] == 10
    assert cfg["slip_bps_by_venue"]["BINANCE"] == 5
    assert cfg["data_mode"] == "local"

    overridden = run_strategy_backtest(
        strategy_id=root.name,
        workspace=tmp_path,
        preflight_only=True,
        settings={"warmup_bars": 25, "max_open_trades": 2, "fee_bps_by_venue": {"BINANCE": 3}},
    )
    cfg2 = overridden["config"]
    assert cfg2["warmup_bars"] == 25
    assert cfg2["max_open_trades"] == 2
    assert cfg2["fee_bps_by_venue"]["BINANCE"] == 3
    assert cfg2["slip_bps_by_venue"]["BINANCE"] == 5, "unspecified nested defaults are preserved"


def test_compaction_keeps_requested_year_and_incomplete_evidence():
    from nerya.llm.tool_compaction import compact_tool_result
    result = compact_tool_result("strategy_backtest", {"ok": True, "strategy_id": "example",
        "proposal_id": "prp_example", "backtest_ts": "20260927_000000",
        "requested_window_days": 365, "requested_window_complete": False,
        "start_utc": "2025-01-01T00:00:00Z", "end_utc": "2025-01-30T23:00:00Z",
        "run_receipt": "strategies/example/backtests/20260927_000000/run.json",
        "replay": {"order_attempts": 0, "orders_submitted": 0, "orders_filled": 0},
        "metrics": {"total_return_pct": 0}}, size_threshold=0)
    assert result.kept["requested_window_days"] == 365
    assert result.kept["requested_window_complete"] is False
    assert result.kept["replay"]["order_attempts"] == 0
    assert result.kept["run_receipt"].endswith("/run.json")


def test_reference_read_compatibility_keeps_path_boundary(tmp_path):
    base = tmp_path / "skills" / "flow_test"
    base.mkdir(parents=True)
    (base / "SKILL.md").write_text("---\nname: flow_test\ndescription: Test\n---\nRead-only\n")
    (tmp_path / "outside.txt").write_text("OUTSIDE_SENTINEL")
    result = skill_tool_handler(ToolCall(name="Skill", arguments={"action": "load",
        "skill": "flow_test", "file": "../../outside.txt"}), skill_index=SkillIndex([base.parent]))
    assert result.is_error
    assert "OUTSIDE_SENTINEL" not in result.text()
