from __future__ import annotations

import json
from pathlib import Path

import pytest

from nerya.skills.builtin.backtest.scripts.config import BacktestConfigError, load_config
from nerya.skills.builtin.backtest.scripts.data_cache import get_candles
from nerya.skills.builtin.backtest.scripts.engine import run_backtest
from nerya.skills.builtin.backtest.scripts.portfolio import PortfolioState
from nerya.strategies.backtest_bridge import backtest_replay


pytestmark = pytest.mark.smoke


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


def _bar(ts: int, close: float) -> dict:
    return {"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1}


def test_backtest_config_default_and_validation():
    cfg = load_config(preset="default", markets=["MOCK:BTCUSDT"])
    assert cfg.initial_capital_usd == 10000
    assert cfg.timeframes == ["1h"]
    assert cfg.markets == ["MOCK:BTCUSDT"]
    cfg = load_config(preset="default", markets=["MOCK:BTCUSDT"], overrides={"tf": "5m", "timeframes": ["1h"]})
    assert cfg.tf == "5m"
    assert cfg.timeframes == ["5m", "1h"]
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


def test_backtest_engine_exposes_requested_timeframes_and_policy():
    seen: list[dict] = []

    def strategy(ctx):
        market = ctx.config.markets[0]
        candles_5m = ctx.market.candles(market, timeframe="5m", limit=3)
        candles_1h = ctx.market.candles(market, timeframe="1h", limit=3)
        seen.append({
            "fast": len(candles_5m),
            "trend": len(candles_1h),
            "order_usd": ctx.policy.default_order_usd,
            "positions": ctx.portfolio.positions(market),
        })
        if not ctx.portfolio.positions(market):
            return ctx.trading.submit_intent(
                market=market,
                side="buy",
                size=ctx.policy.default_order_usd,
                size_unit="usd",
                order_type="market",
                reasoning="enter from multi timeframe",
            )
        return ctx.trading.submit_intent(
            market=market,
            side="sell",
            size=0,
            size_unit="base",
            order_type="market",
            reasoning="exit from portfolio mirror",
        )

    cfg = load_config(
        preset="default",
        markets=["MOCK:BTCUSDT"],
        overrides={"tf": "5m", "timeframes": ["1h"], "warmup_bars": 0, "window_days": 30},
    )
    fast = [_bar(1_700_000_000 + i * 300, 100 + i) for i in range(6)]
    trend = [_bar(1_700_000_000 + i * 3600, 100 + i * 2) for i in range(2)]
    result = run_backtest(
        None,
        cfg,
        candles_by_market={"MOCK:BTCUSDT": fast},
        timeframe_candles_by_market={"MOCK:BTCUSDT": {"5m": fast, "1h": trend}},
        run_fn=strategy,
        strategy_config={
            "strategy_id": "multi_tf",
            "markets": ["MOCK:BTCUSDT"],
            "policy": {"default_order_usd": 50},
        },
    )

    assert seen
    assert max(row["trend"] for row in seen) >= 1
    assert seen[0]["order_usd"] == 50
    assert result.trades[0]["reason"] == "enter from multi timeframe"
    assert any(row["positions"] for row in seen[1:])


def test_backtest_engine_imports_dataclass_strategy_module(tmp_path: Path):
    strategy_root = tmp_path / "strategies" / "dataclass_strategy"
    strategy_root.mkdir(parents=True)
    (strategy_root / "main.py").write_text(
        "\n".join([
            "from __future__ import annotations",
            "from dataclasses import dataclass",
            "",
            "@dataclass",
            "class Signal:",
            "    reason: str",
            "",
            "def run(ctx):",
            "    signal = Signal('dataclass import ok')",
            "    return ctx.result.hold(reason=signal.reason)",
        ]),
        encoding="utf-8",
    )
    cfg = load_config(
        preset="default",
        markets=["MOCK:BTCUSDT"],
        overrides={"warmup_bars": 0, "window_days": 30},
    )
    rows = [_bar(1_700_000_000 + i * 3600, 100 + i) for i in range(3)]

    result = run_backtest(
        strategy_root,
        cfg,
        candles_by_market={"MOCK:BTCUSDT": rows},
        strategy_config={"strategy_id": "dataclass_strategy", "markets": ["MOCK:BTCUSDT"]},
    )

    assert result.decisions
    assert result.decisions[0]["reason"] == "dataclass import ok"


def test_portfolio_sell_fill_adds_cash():
    portfolio = PortfolioState(1000)
    portfolio.apply_fill({"market": "MOCK:BTCUSDT", "side": "buy", "qty": 1, "price": 100, "fee": 0})
    portfolio.apply_fill({"market": "MOCK:BTCUSDT", "side": "sell", "qty": 1, "price": 110, "fee": 0})
    portfolio.mark_to_market(1, {"MOCK:BTCUSDT": 110})
    assert portfolio.cash == 1010
    assert portfolio.realized_pnl == 10
    assert portfolio.snapshot()["equity"] == 1010


def test_binance_vision_cache_fallback_reads_daily_zip(monkeypatch, tmp_path: Path):
    import io
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr(
            "BTCUSDT-5m-2026-05-01.csv",
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
            "1777593600000,100,101,99,100.5,10,1777593899999,0,0,0,0,0\n"
            "1777593900000,100.5,102,100,101.5,11,1777594199999,0,0,0,0,0\n",
        )

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return payload.getvalue()

    monkeypatch.setattr(
        "nerya.skills.builtin.backtest.scripts.data_cache.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Resp(),
    )
    rows = get_candles(
        "BINANCE:BTCUSDT",
        "5m",
        1_777_593_600,
        1_777_594_200,
        tmp_path,
    )
    assert [row["close"] for row in rows] == [100.5, 101.5]
    assert (tmp_path / "candles" / "BINANCE" / "BTCUSDT" / "5m" / "1777593600_1777594200.parquet").exists()


def test_binance_perpetual_uses_usdm_archive(monkeypatch, tmp_path: Path):
    import io
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr(
            "ETHUSDT-1m-2026-05-01.csv",
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
            "1777593600000,3000,3010,2990,3005,100,1777593659999,0,0,0,0,0\n",
        )
    seen_urls: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return payload.getvalue()

    def _fake_urlopen(url, *_args, **_kwargs):
        seen_urls.append(str(url))
        return _Resp()

    monkeypatch.setattr(
        "nerya.skills.builtin.backtest.scripts.data_cache.urllib.request.urlopen",
        _fake_urlopen,
    )

    rows = get_candles(
        "binance_perpetual:ETHUSDT",
        "1m",
        1_777_593_600,
        1_777_593_660,
        tmp_path,
    )

    assert rows[0]["close"] == 3005
    assert seen_urls
    assert "/data/futures/um/daily/klines/ETHUSDT/1m/" in seen_urls[0]

