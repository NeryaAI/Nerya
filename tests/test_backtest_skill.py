from __future__ import annotations

import json
from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.core.config import Config
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig, BacktestConfigError, load_config
from nerya.skills.builtin.backtest.scripts.data_cache import get_candles
from nerya.skills.builtin.backtest.scripts.engine import run_backtest
from nerya.skills.builtin.backtest.scripts.portfolio import PortfolioState
from nerya.skills.builtin.backtest.scripts.backtest_run import (
    _apply_coverage_gate,
    _discover_strategy_timeframes,
    run_strategy_backtest,
)
from nerya.skills.builtin.backtest.scripts.data_cache import NoHistoricalDataError
from nerya.skills.builtin.backtest.scripts.mock_ctx import MockMarket
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
    assert cfg.window_days == 45
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


def test_backtest_discovers_strategy_declared_default_timeframe(tmp_path: Path):
    strategy_root = tmp_path / "strategies" / "daily_team"
    strategy_root.mkdir(parents=True)
    (strategy_root / "main.py").write_text(
        '_DEFAULT_TIMEFRAME = "1d"\n',
        encoding="utf-8",
    )

    assert _discover_strategy_timeframes(strategy_root) == ["1d"]


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
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "| total_return_pct |" in report
    assert "| total_return_pct | " in report and "% |" in report
    chart = json.loads((out / "chart.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in chart["panels"]] == ["price", "equity", "drawdown", "rsi", "missed"]


def test_backtest_engine_exposes_requested_timeframes_and_policy():
    seen: list[dict] = []

    def strategy(ctx):
        market = ctx.config.markets[0]
        assert ctx.market_data is ctx.market
        assert ctx.logger.name.endswith(".backtest")
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


def test_backtest_run_accepts_in_flight_strategy_proposal(tmp_path: Path):
    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="proposal_backtest_smoke",
            title="Proposal Backtest Smoke",
            prompt="Smoke-test a proposal before promotion.",
            markets=("MOCK:BTCUSDT",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "\n".join([
                    "def run(ctx):",
                    "    return ctx.result.hold(reason='proposal backtest smoke')",
                ]),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )

    assert generated.proposal is not None
    out = run_strategy_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
        allow_mock=True,
    )

    assert out["ok"] is True
    assert out["strategy_id"] == "proposal_backtest_smoke"
    assert out["proposal_id"] == generated.proposal.id
    assert out["metrics_path"].endswith("metrics.json")
    assert out["report_path"].endswith("report.md")
    assert out["strategy_root"].endswith("proposal_backtest_smoke")
    assert out["main_path"].endswith("main.py")
    assert out["strategy_yml_path"].endswith("strategy.yml")
    assert "coverage_ok" in out
    assert "coverage_message" in out
    assert out["metric_units"]["*_pct"].startswith("percentage points")
    assert "metrics_display" in out
    assert "operator_summary" in out
    assert "operator_summary_text" in out
    assert "0.0274 means 0.0274%" in out["operator_summary_text"]
    assert out["operator_summary"]["unit_warning"].endswith(
        "never multiply them by 100."
    )
    out_dir = Path(out["out_dir"])
    assert "proposals" in out_dir.parts
    assert (out_dir / "metrics.json").exists()
    assert (out_dir / "report.md").exists()


def test_backtest_marks_insufficient_loaded_coverage() -> None:
    metrics = {
        "backtest_days": 20.79,
        "flags": [],
        "verdict": "PASS",
    }
    _apply_coverage_gate(metrics, BacktestConfig(min_backtest_days=30, window_days=45))

    assert metrics["coverage_ok"] is False
    assert metrics["verdict"] == "FAIL"
    assert "insufficient_backtest_window" in metrics["flags"]
    assert "one-month-plus backtest" in metrics["coverage_message"]


def test_strategy_backtest_handler_structures_missing_history(monkeypatch, tmp_path: Path) -> None:
    from nerya.skills.builtin.backtest.scripts import backtest_run
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler
    from nerya.tools.types import ToolCall

    def fail_missing_history(**_kwargs):
        raise NoHistoricalDataError("no historical candles for solana:USDC 1h")

    monkeypatch.setattr(backtest_run, "run_strategy_backtest", fail_missing_history)
    result = strategy_backtest_handler(
        ToolCall(
            name="strategy_backtest",
            arguments={"proposal_id": "prp_missing_history", "allow_mock": False},
        ),
        config=Config(paths=WorkspacePaths(root=tmp_path)),
    )

    assert result.is_error is False
    data = result.content[0].data
    assert data["ok"] is False
    assert data["reason"] == "no_historical_data"
    assert data["coverage_ok"] is False
    assert "Do not retry with mock" in data["next_required_action"]["message"]


def test_strategy_backtest_handler_returns_display_metrics_to_model(monkeypatch, tmp_path: Path) -> None:
    from nerya.skills.builtin.backtest.scripts import backtest_run
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler
    from nerya.tools.types import ToolCall

    def fake_backtest(**_kwargs):
        return {
            "ok": True,
            "strategy_id": "s1",
            "proposal_id": "prp_123",
            "backtest_ts": "20260517_010203",
            "metrics_path": str(tmp_path / "metrics.json"),
            "total_return_pct": 0.0274,
            "max_drawdown_pct": 0.0282,
            "metrics": {"total_return_pct": 0.0274, "max_drawdown_pct": 0.0282},
            "metrics_display": {
                "total_return_pct": "0.0274%",
                "max_drawdown_pct": "0.0282%",
            },
            "operator_summary_text": "total_return_pct: 0.0274%",
        }

    monkeypatch.setattr(backtest_run, "run_strategy_backtest", fake_backtest)
    result = strategy_backtest_handler(
        ToolCall(name="strategy_backtest", arguments={"proposal_id": "prp_123"}),
        config=Config(paths=WorkspacePaths(root=tmp_path)),
    )

    data = result.content[0].data
    assert "total_return_pct" not in data
    assert "max_drawdown_pct" not in data
    assert data["metrics"]["total_return_pct"] == "0.0274%"
    assert data["metrics_are_display_strings"] is True
    assert data["raw_metrics_file"].endswith("metrics.json")


def test_backtest_mock_market_exposes_common_aliases() -> None:
    rows = [
        {"open": 99, "high": 101, "low": 98, "close": 100, "volume": 1},
        {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 2},
    ]
    market = MockMarket("MOCK:BTCUSDT", {"MOCK:BTCUSDT": rows})

    assert market.get_candles("MOCK:BTCUSDT", interval="1m", count=1)[0]["close"] == 101
    assert market.get_ticker("MOCK:BTCUSDT")["mid"] == 101


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
