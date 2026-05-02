from __future__ import annotations

import json
from pathlib import Path

import pytest

from nerya.skills.builtin.backtest.scripts.config import BacktestConfigError, load_config
from nerya.strategies.backtest_bridge import backtest_replay


def _strategy(ctx):
    market = ctx.config.markets[0]
    candles = ctx.market.candles(market, timeframe="1h", limit=20)
    if len(candles) == 10 and not ctx.state.get(f"position:{market}"):
        return ctx.trading.submit_intent(
            market=market,
            side="buy",
            size=1000,
            size_unit="usd",
            order_type="market",
            reasoning="fixture_entry",
        )
    if len(candles) == 20 and ctx.state.get(f"position:{market}"):
        return ctx.trading.submit_intent(
            market=market,
            side="sell",
            size=0,
            size_unit="usd",
            order_type="market",
            reasoning="fixture_exit",
        )
    return ctx.result.hold(reason="wait")


def test_backtest_config_default_and_validation():
    cfg = load_config(preset="default", markets=["MOCK:BTCUSDT"])
    assert cfg.initial_capital_usd == 10000
    assert cfg.markets == ["MOCK:BTCUSDT"]
    with pytest.raises(BacktestConfigError):
        load_config(preset="default", overrides={"window_days": 10})
    with pytest.raises(BacktestConfigError):
        load_config(
            preset="default",
            overrides={"mock_surfaces": {"news": {"mode": "bad"}}},
        )


def test_backtest_replay_returns_metrics_and_artifacts(tmp_path: Path):
    out = tmp_path / "bt"
    stats = backtest_replay(
        _strategy,
        markets=["MOCK:BTCUSDT"],
        window_days=30,
        tf="1h",
        artefacts_dir=out,
    )
    assert stats["initial_capital_usd"] == 10000
    assert "total_return_pct" in stats
    assert (out / "metrics.json").exists()
    assert (out / "report.md").exists()
    assert (out / "chart.json").exists()
    chart = json.loads((out / "chart.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in chart["panels"]] == ["price", "equity", "drawdown", "rsi", "missed"]

