from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.core.config import Config
from nerya.evolution.patch_proposal import create_proposal
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
from nerya.skills.builtin.backtest.scripts.mock_ctx import MockMarket, MockState, MockCtx
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
    assert cfg.window_days == 180
    assert cfg.short_lived_window_days == 7
    assert cfg.timeframes == ["1h"]
    assert cfg.markets == ["MOCK:BTCUSDT"]
    cfg = load_config(preset="default", markets=["MOCK:BTCUSDT"], overrides={"tf": "5m", "timeframes": ["1h"]})
    assert cfg.tf == "5m"
    assert cfg.timeframes == ["5m", "1h"]
    cfg = load_config(preset="default", overrides={"window_days": 10})
    assert cfg.window_days == 10
    assert cfg.min_backtest_days == 0
    with pytest.raises(BacktestConfigError):
        load_config(preset="default", overrides={"min_backtest_days": -1})
    with pytest.raises(BacktestConfigError):
        load_config(preset="default", overrides={"short_lived_window_days": 0})
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


def test_backtest_engine_awaits_async_strategy_run():
    async def strategy(ctx):
        return ctx.result.hold(reason="async run ok")

    cfg = load_config(
        preset="default",
        markets=["MOCK:BTCUSDT"],
        overrides={"warmup_bars": 0, "window_days": 30},
    )
    rows = [_bar(1_700_000_000 + i * 3600, 100 + i) for i in range(3)]

    result = run_backtest(
        None,
        cfg,
        candles_by_market={"MOCK:BTCUSDT": rows},
        run_fn=strategy,
        strategy_config={"strategy_id": "async_strategy", "markets": ["MOCK:BTCUSDT"]},
    )

    assert result.decisions
    assert result.decisions[0]["reason"] == "async run ok"


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
    assert out["primary_timeframe"]
    assert out["primary_timeframe"] in out["operator_summary_text"]
    assert out["primary_timeframe"] in out["timeframes"]
    assert out["metric_units"]["*_pct"].startswith("percentage points")
    assert "metrics_display" in out
    assert "operator_summary" in out
    assert "operator_summary_text" in out
    # operator_summary_text must be clean, copy-safe display values only:
    # internal meta-instructions used to leak verbatim into the final reply.
    assert "Copy these display values exactly" not in out["operator_summary_text"]
    assert "0.0274 means 0.0274%" not in out["operator_summary_text"]
    assert "Primary timeframe:" in out["operator_summary_text"]
    # Unit/formatting guidance for the model lives in dedicated fields instead.
    assert "0.0274 is 0.0274%" in out["unit_warning"]
    assert out["operator_summary"]["unit_warning"].endswith(
        "never multiply them by 100."
    )
    out_dir = Path(out["out_dir"])
    assert "proposals" in out_dir.parts
    assert (out_dir / "metrics.json").exists()
    assert (out_dir / "report.md").exists()


def test_backtest_rejects_unsupported_explicit_market_before_fetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts import data_cache

    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="Aster perp cash-and-carry",
        extra_files={
            "after/strategies/aster_cash_carry/strategy.yml": (
                "strategy_id: aster_cash_carry\n"
                "markets:\n"
                "  - aster:BTCUSDT-PERP\n"
                "accounts:\n"
                "  - paper\n"
                "schedule:\n"
                "  type: cron\n"
                "  cron: '0 * * * *'\n"
                "  enabled: true\n"
            ),
            "after/strategies/aster_cash_carry/main.py": (
                "def run(ctx):\n"
                "    return ctx.result.hold(reason='data gap')\n"
            ),
        },
    )

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("unsupported explicit market should fail before fetch")

    monkeypatch.setattr(data_cache, "fetch_candles", fail_fetch)

    with pytest.raises(NoHistoricalDataError, match="unsupported historical data venue"):
        run_strategy_backtest(
            proposal_id=proposal.id,
            workspace=tmp_path,
            allow_mock=False,
        )


def test_backtest_accepts_equity_alias_as_supported_yahoo_history(tmp_path: Path) -> None:
    from nerya.skills.builtin.backtest.scripts.backtest_run import (
        _unsupported_explicit_historical_markets,
    )

    cfg = Config(paths=WorkspacePaths(root=tmp_path))

    assert _unsupported_explicit_historical_markets(
        ["equities:TSLA", "stock:NVDA", "stocks:AAPL"],
        config_obj=cfg,
    ) == []


def test_backtest_marks_short_loaded_coverage_as_recommendation() -> None:
    metrics = {
        "backtest_days": 20.79,
        "flags": [],
        "verdict": "PASS",
    }
    _apply_coverage_gate(metrics, BacktestConfig(min_backtest_days=30, window_days=45))

    assert metrics["coverage_ok"] is True
    assert metrics["recommended_coverage_ok"] is False
    assert metrics["verdict"] == "PASS"
    assert "below_recommended_backtest_window" in metrics["flags"]
    assert "short-window real-data backtest" in metrics["coverage_message"]


def test_backtest_default_has_no_minimum_coverage_gate() -> None:
    metrics = {
        "backtest_days": 7.0,
        "flags": [],
        "verdict": "PASS",
    }
    _apply_coverage_gate(metrics, BacktestConfig(min_backtest_days=0, window_days=180))

    assert metrics["coverage_ok"] is True
    assert metrics["recommended_coverage_ok"] is True
    assert metrics["verdict"] == "PASS"
    assert metrics["flags"] == []
    assert "Loaded 7.00d of real candle coverage" in metrics["coverage_message"]


def test_backtest_falls_back_to_available_short_real_timeframe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts import data_cache

    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="byreal_short_window_fallback",
            title="Byreal Short Window Fallback",
            prompt="Backtest a short-window on-chain meme strategy.",
            markets=("BYREAL_ONCHAIN:solana",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "\n".join(
                    [
                        "def run(ctx):",
                        "    return ctx.result.hold(reason='short window smoke')",
                    ]
                ),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )
    assert generated.proposal is not None

    seen_intervals: list[str] = []
    seen_counts: list[int] = []
    seen_starts: list[int] = []

    def fake_fetch_candles(
        market,
        *,
        count,
        interval,
        allow_mock,
        config_like=None,
        **_kwargs,
    ):
        del market, allow_mock, config_like
        seen_intervals.append(interval)
        seen_counts.append(int(count))
        if _kwargs.get("start") is not None:
            seen_starts.append(int(_kwargs["start"]))
        if interval == "1h":
            return []
        if interval == "5m":
            start = int(time.time()) - (299 * 300)
            return [_bar(start + (i * 300), 1.0 + i * 0.001) for i in range(300)]
        return []

    monkeypatch.setattr(data_cache, "fetch_candles", fake_fetch_candles)

    out = run_strategy_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
        allow_mock=False,
    )

    assert out["ok"] is True
    assert out["coverage_ok"] is True
    assert out["recommended_coverage_ok"] is True
    assert out["primary_timeframe"] == "5m"
    assert out["requested_primary_timeframe"] == "1h"
    assert out["attempted_timeframes"] == ["1h", "5m"]
    assert out["timeframe_fallback"] is True
    assert seen_intervals == ["1h", "5m"]
    assert seen_counts[0] < 300
    assert seen_starts
    assert out["requested_window_days"] == 7.0
    assert "Short-lived meme/on-chain window policy applied" in out["coverage_message"]
    assert "Requested primary timeframe 1h" in out["coverage_message"]


def test_backtest_falls_back_when_primary_timeframe_cannot_pass_warmup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts import data_cache

    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="byreal_short_window_warmup",
            title="Byreal Short Window Warmup",
            prompt="Backtest a short-window on-chain meme strategy.",
            markets=("BYREAL_ONCHAIN:solana:token",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "\n".join(
                    [
                        "def run(ctx):",
                        "    return ctx.result.hold(reason='short window smoke')",
                    ]
                ),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )
    assert generated.proposal is not None

    seen_intervals: list[str] = []

    def fake_fetch_candles(
        market,
        *,
        count,
        interval,
        allow_mock,
        config_like=None,
        **_kwargs,
    ):
        del market, count, allow_mock, config_like
        seen_intervals.append(interval)
        start = int(time.time()) - (200 * 300)
        if interval == "1h":
            return [_bar(start + (i * 3600), 1.0 + i * 0.01) for i in range(12)]
        if interval == "5m":
            return [_bar(start + (i * 300), 1.0 + i * 0.001) for i in range(180)]
        return []

    monkeypatch.setattr(data_cache, "fetch_candles", fake_fetch_candles)

    out = run_strategy_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
        allow_mock=False,
    )

    assert out["ok"] is True
    assert out["primary_timeframe"] == "5m"
    assert out["requested_primary_timeframe"] == "1h"
    assert out["attempted_timeframes"] == ["1h", "5m"]
    assert out["timeframe_fallback"] is True
    assert seen_intervals == ["1h", "5m"]


def test_backtest_candle_cache_passes_workspace_config_to_wallet_sources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts import data_cache

    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={"wallet": {"providers": {}}})
    seen: dict[str, object] = {}

    def fake_fetch_candles(market, *, count, interval, allow_mock, config_like=None, **_kwargs):
        seen.update(
            {
                "market": market,
                "count": count,
                "interval": interval,
                "allow_mock": allow_mock,
                "config_like": config_like,
            }
        )
        return [
            {"ts": 1_700_000_000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
            {"ts": 1_700_003_600, "open": 2, "high": 3, "low": 2, "close": 3, "volume": 11},
        ]

    monkeypatch.setattr(data_cache, "fetch_candles", fake_fetch_candles)

    rows = data_cache.get_candles(
        "BYREAL_ONCHAIN:solana:token",
        "1h",
        1_699_999_999,
        1_700_004_000,
        tmp_path / "cache",
        allow_mock=False,
        config_like=cfg,
    )

    assert len(rows) == 2
    assert seen["config_like"] is cfg
    assert seen["allow_mock"] is False


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


def test_strategy_backtest_handler_flags_generic_onchain_market_before_waiver(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts import backtest_run
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler
    from nerya.tools.types import ToolCall

    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="generic onchain scanner",
        extra_files={
            "after/strategies/s1/strategy.yml": (
                "strategy_id: s1\n"
                "markets:\n"
                "  - BYREAL_ONCHAIN:solana\n"
            ),
            "after/strategies/s1/strategy.md": "Solana meme scanner with short K-line evidence.",
            "after/strategies/s1/main.py": "def run(ctx):\n    return ctx.result.hold(reason='smoke')\n",
        },
    )

    def fail_missing_history(**_kwargs):
        raise NoHistoricalDataError("no historical candles for BYREAL_ONCHAIN:solana")

    monkeypatch.setattr(backtest_run, "run_strategy_backtest", fail_missing_history)
    result = strategy_backtest_handler(
        ToolCall(
            name="strategy_backtest",
            arguments={"proposal_id": proposal.id, "allow_mock": False},
        ),
        config=Config(paths=paths),
    )

    data = result.content[0].data
    assert data["ok"] is False
    assert data["next_required_action"]["type"] == "repair_concrete_market_and_rerun"
    assert "not proof that standard OHLCV is unavailable" in data["next_required_action"]["message"]
    assert "BYREAL_ONCHAIN:solana:<pool_address>" in data["next_required_action"]["repair_hint"]


def test_backtest_cli_structures_missing_history(monkeypatch, capsys) -> None:
    from nerya.skills.builtin.backtest.scripts import backtest_run

    def fail_missing_history(**_kwargs):
        raise NoHistoricalDataError("no historical candles for solana:USDC 1h")

    monkeypatch.setattr(backtest_run, "run_strategy_backtest", fail_missing_history)
    code = backtest_run.main(["--proposal-id", "prp_missing_history"])

    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["reason"] == "no_historical_data"
    assert data["proposal_id"] == "prp_missing_history"
    assert data["coverage_ok"] is False
    assert "standard-backtest waiver" in data["next_required_action"]["message"]


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


def test_strategy_backtest_handler_surfaces_custom_replay_report(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts import backtest_run
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler
    from nerya.tools.types import ToolCall

    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="meme smart money strategy",
        extra_files={
            "after/strategies/s1/strategy.yml": (
                "strategy_id: s1\n"
                "strategy_class: agent\n"
                "markets:\n"
                "  - BYREAL_ONCHAIN:solana:token\n"
            ),
            "after/strategies/s1/strategy.md": "Solana meme smart-money strategy.",
            "after/strategies/s1/main.py": "# uses StrategyAgentTask for smart_money replay\n",
        },
    )
    strategy_root = paths.proposals / proposal.id / "after" / "strategies" / "s1"
    (strategy_root / "custom_replay_report.json").write_text(
        json.dumps(
            {
                "data_source": "byreal_onchain",
                "results": [
                    {
                        "symbol": "SPCX",
                        "decision": "BUY",
                        "score": 100,
                        "risk_level": 1,
                        "liquidity_usd": 252102.31,
                        "top10_hold_pct": 15.73,
                        "unique_traders": 178,
                        "smart_money_inflow_usd": 1306.03,
                    },
                    {"symbol": "ASTEROID", "decision": "SKIP", "score": 65},
                ],
                "summary": {"buy": 1, "watch": 0, "skip": 1},
            }
        ),
        encoding="utf-8",
    )

    def fake_backtest(**_kwargs):
        return {
            "ok": True,
            "strategy_id": "s1",
            "proposal_id": proposal.id,
            "verdict": "FAIL",
            "reason": "no_trades",
            "metrics_path": str(strategy_root / "backtests" / "run" / "metrics.json"),
            "metrics_display": {"total_return_pct": "0.0000%"},
        }

    monkeypatch.setattr(backtest_run, "run_strategy_backtest", fake_backtest)
    result = strategy_backtest_handler(
        ToolCall(name="strategy_backtest", arguments={"proposal_id": proposal.id}),
        config=Config(paths=paths),
    )

    data = result.content[0].data
    replay = data["nonstandard_backtests"][0]
    assert replay["kind"] == "custom_replay"
    assert replay["ok"] is True
    assert replay["report_path"].endswith("custom_replay_report.json")
    assert replay["events_seen"] == 2
    assert replay["signals"] == 1
    assert replay["simulated_trades"] == 1
    assert replay["decision_sample"][0]["symbol"] == "SPCX"
    assert data["paper_review_allowed"] is True
    assert data["paper_review_basis"] == "real_kline_standard_backtest_plus_custom_event_replay"
    assert data["shadow_live_requires_user_approval"] is True
    assert "FAIL/no_trades" in data["paper_review_note"]
    assert data["review_gate"]["paper_review_allowed"] is True
    assert data["review_gate"]["shadow_live_requires_user_approval"] is True
    assert "trend/scalping" in data["review_gate"]["message"]


def test_strategy_backtest_handler_prefers_freeform_sdk_backtest(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import (
        _proposal_backtest_artifacts,
        strategy_backtest_handler,
    )
    from nerya.tools.types import ToolCall

    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="meme smart money strategy with SDK replay",
        extra_files={
            "after/strategies/s1/strategy.yml": (
                "version: 1\n"
                "strategy_id: s1\n"
                "title: SDK replay smoke\n"
                "mode: paper\n"
                "entrypoint: main.py:run\n"
                "markets:\n"
                "  - BYREAL_ONCHAIN:solana:token\n"
                "accounts:\n"
                "  - paper_main\n"
                "schedule:\n"
                "  type: cron\n"
                "  cron: '*/5 * * * *'\n"
            ),
            "after/strategies/s1/strategy.md": (
                "Solana meme smart-money strategy using provider SDK history."
            ),
            "after/strategies/s1/main.py": (
                "def run(ctx):\n"
                "    return ctx.result.hold(reason='sdk replay owns research evidence')\n"
            ),
            "after/strategies/s1/backtests/research_backtest.py": (
                "from pathlib import Path\n"
                "import os\n"
                "out = Path(os.environ['NERYA_BACKTEST_OUT_DIR'])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "(out / 'equity.csv').write_text(\n"
                "    'ts,equity\\n1700000000,10000\\n1700000300,10500\\n',\n"
                "    encoding='utf-8',\n"
                ")\n"
                "(out / 'trades.csv').write_text(\n"
                "    'ts,side,price,size,reason\\n'\n"
                "    '1700000100,buy,1.00,100,smart_wallet_inflow\\n'\n"
                "    '1700000200,sell,1.05,100,target\\n',\n"
                "    encoding='utf-8',\n"
                ")\n"
            ),
        },
    )

    result = strategy_backtest_handler(
        ToolCall(name="strategy_backtest", arguments={"proposal_id": proposal.id}),
        config=Config(paths=paths),
    )

    data = result.content[0].data
    assert data["ok"] is True
    assert data["kind"] == "freeform_backtest"
    assert data["paper_review_basis"] == "freeform_sdk_backtest"
    assert data["coverage_ok"] is True
    assert data["metrics"]["total_return_pct"] == "5.0000%"
    assert data["chart_panels"] == 1
    assert data["nonstandard_backtests"][0]["kind"] == "freeform_backtest"
    assert data["nonstandard_backtests"][0]["has_equity_curve"] is True
    assert data["nonstandard_backtests"][0]["has_trade_details"] is True
    raw_metrics = json.loads(Path(data["raw_metrics_file"]).read_text(encoding="utf-8"))
    assert raw_metrics["backtest_kind"] == "freeform_backtest"
    assert raw_metrics["total_trades"] == 2
    assert _proposal_backtest_artifacts(paths, proposal.id, "s1") == []


def test_freeform_backtest_accepts_stdout_json_payload(tmp_path: Path) -> None:
    from nerya.skills.builtin.backtest.scripts.freeform_run import run_freeform_backtest

    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="stdout_freeform_smoke",
            title="Stdout Freeform Smoke",
            prompt="Emit freeform result over stdout.",
            markets=("BYREAL_ONCHAIN:solana:token",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "def run(ctx):\n    return ctx.result.hold(reason='paper')\n",
                "strategy.md": "freeform stdout smoke",
                "backtests/research_backtest.py": "\n".join(
                    [
                        "import json",
                        "payload = {",
                        "  'initial_capital_usd': 10000,",
                        "  'equity_curve': [",
                        "    {'ts': '2026-01-01T00:00:00.123456', 'equity': 10000},",
                        "    {'ts': '2026-01-01T00:05:00.654321', 'equity': 10020},",
                        "  ],",
                        "  'trades': [",
                        "    {'ts': '2026-01-01T00:05:00', 'side': 'buy', 'price': 1, 'size': 20, 'equity': 10020, 'reason': 'smart_money_buy'},",
                        "  ],",
                        "}",
                        "print('log line before payload')",
                        "print('NERYA_FREEFORM_RESULT_JSON=' + json.dumps(payload))",
                    ]
                ),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )
    assert generated.proposal is not None

    out = run_freeform_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
    )

    assert out["ok"] is True
    assert out["equity_points"] == 2
    assert out["total_trades"] == 1
    out_dir = Path(out["out_dir"])
    assert (out_dir / "equity.csv").exists()
    assert (out_dir / "trades.csv").exists()
    chart = json.loads((out_dir / "chart.json").read_text(encoding="utf-8"))
    assert chart["panels"][0]["series"][0]["data"][0]["time"] > 0
    assert chart["panels"][0]["series"][0]["data"][1]["value"] == 10020


def test_freeform_backtest_accepts_pretty_stdout_numeric_equity_curve(
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts.freeform_run import run_freeform_backtest

    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="pretty_stdout_freeform_smoke",
            title="Pretty Stdout Freeform Smoke",
            prompt="Emit pretty JSON over stdout.",
            markets=("BYREAL_ONCHAIN:solana:token",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "def run(ctx):\n    return ctx.result.hold(reason='paper')\n",
                "strategy.md": "freeform pretty stdout smoke",
                "backtests/research_backtest.py": "\n".join(
                    [
                        "import json",
                        "payload = {",
                        "  'initial_capital_usd': 10000,",
                        "  'equity_curve': [10000, 10005, 10020],",
                        "  'trades': [",
                        "    {'ts': 1, 'side': 'buy', 'price': 1, 'size': 1, 'reason': 'signal'},",
                        "  ],",
                        "}",
                        "print(json.dumps(payload, indent=2))",
                    ]
                ),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )
    assert generated.proposal is not None

    out = run_freeform_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
    )

    assert out["ok"] is True
    assert out["equity_points"] == 3
    out_dir = Path(out["out_dir"])
    rows = (out_dir / "equity.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "ts,equity"
    assert rows[1] == "0,10000"


def test_freeform_backtest_accepts_fresh_script_dir_artifacts(
    tmp_path: Path,
) -> None:
    from nerya.skills.builtin.backtest.scripts.freeform_run import run_freeform_backtest

    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="script_dir_freeform_smoke",
            title="Script Dir Freeform Smoke",
            prompt="Write freeform artifacts beside the script.",
            markets=("BYREAL_ONCHAIN:solana:token",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "def run(ctx):\n    return ctx.result.hold(reason='paper')\n",
                "strategy.md": "freeform script dir smoke",
                "backtests/research_backtest.py": "\n".join(
                    [
                        "import json",
                        "from pathlib import Path",
                        "out = Path(__file__).parent",
                        "(out / 'result.json').write_text(json.dumps({",
                        "  'initial_capital_usd': 10000,",
                        "  'equity_curve': [10000, 10025],",
                        "  'trades': [{'ts': 1, 'side': 'buy', 'price': 1, 'size': 1}],",
                        "}), encoding='utf-8')",
                    ]
                ),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )
    assert generated.proposal is not None

    out = run_freeform_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
    )

    assert out["ok"] is True
    assert out["equity_points"] == 2
    assert Path(out["equity_path"]).parent == Path(out["out_dir"])


def test_freeform_backtest_closes_stdin_for_scripts_that_read(tmp_path: Path) -> None:
    from nerya.skills.builtin.backtest.scripts.freeform_run import run_freeform_backtest

    paths = WorkspacePaths(root=tmp_path)
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="stdin_freeform_smoke",
            title="Stdin Freeform Smoke",
            prompt="Read stdin before emitting a freeform result.",
            markets=("BYREAL_ONCHAIN:solana:token",),
            accounts=("paper_main",),
            schedule_cron="*/5 * * * *",
            files={
                "main.py": "def run(ctx):\n    return ctx.result.hold(reason='paper')\n",
                "strategy.md": "freeform stdin smoke",
                "backtests/research_backtest.py": "\n".join(
                    [
                        "import json",
                        "import sys",
                        "sys.stdin.read()",
                        "payload = {",
                        "  'initial_capital_usd': 10000,",
                        "  'equity_curve': [",
                        "    {'ts': '2026-01-01T00:00:00', 'equity': 10000},",
                        "    {'ts': '2026-01-01T00:05:00', 'equity': 10010},",
                        "  ],",
                        "  'trades': [],",
                        "}",
                        "print('NERYA_FREEFORM_RESULT_JSON=' + json.dumps(payload))",
                    ]
                ),
            },
        ),
        validate=True,
        create_proposal_record=True,
    )
    assert generated.proposal is not None

    out = run_freeform_backtest(
        proposal_id=generated.proposal.id,
        workspace=tmp_path,
        timeout_seconds=2,
    )

    assert out["ok"] is True
    assert out["equity_points"] == 2


def test_strategy_generate_proposal_handler_returns_manifest_execution_mode(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import strategy_generate_proposal_handler
    from nerya.tools.types import ToolCall

    cfg = Config(paths=WorkspacePaths(root=tmp_path))

    generated = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_trend_payload",
                "title": "BTC Trend Payload",
                "strategy_class": "trend",
                "execution_mode": "script",
                "markets": ["BINANCE:BTC/USDT"],
                "accounts": ["paper"],
            },
        ),
        config=cfg,
    )

    assert generated.is_error is False
    data = generated.content[0].data
    assert data["strategy_class"] == "trend"
    assert data["execution_mode"] == "script"


def test_strategy_generate_proposal_handler_accepts_agent_team_decision_strategy(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import strategy_generate_proposal_handler
    from nerya.tools.types import ToolCall

    cfg = Config(paths=WorkspacePaths(root=tmp_path))

    generated = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "nvda_premarket_brief",
                "title": "NVDA premarket AgentTeam brief",
                "description": (
                    "Daily AgentTeam research brief with a score and position "
                    "suggestion before the market opens."
                ),
                "prompt": (
                    "Use an AgentTeam to analyze NVDA every morning, judge the "
                    "setup, score conviction, size risk, and decide whether to "
                    "skip when data is unavailable."
                ),
                "strategy_class": "agent_team",
                "execution_mode": "agent_team",
                "markets": ["YAHOO:NVDA"],
                "accounts": ["paper_main"],
                "mode": "paper",
                "schedule_cron": "0 13 * * 1-5",
            },
            metadata={
                "original_user_prompt": (
                    "用 AgentTeam 长期分析 NVDA，每天开盘前给我评分和仓位建议"
                )
            },
        ),
        config=cfg,
    )

    assert generated.is_error is False, generated.text()
    data = generated.content[0].data
    assert data["strategy_class"] == "agent_team"
    assert data["execution_mode"] == "agent_team"
    assert data["validation"]["ok"] is True
    assert data["proposal_id"]


def test_strategy_generate_proposal_handler_recovers_truncated_raw_payload(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import strategy_generate_proposal_handler
    from nerya.tools.types import ToolCall

    cfg = Config(paths=WorkspacePaths(root=tmp_path))
    raw = (
        '{"strategy_id":"binance_aster_cash_carry",'
        '"title":"Binance Spot + Aster Perp Cash-and-Carry Arbitrage",'
        '"description":"Cross-venue cash-and-carry strategy using Binance spot '
        'and Aster perpetual basis/funding.",'
        '"prompt":"Use Binance spot and Aster perpetual markets for a '
        'cash-and-carry basis strategy; report data gaps honestly.",'
        '"strategy_class":"custom",'
        '"execution_mode":"script",'
        '"mode":"paper",'
        '"markets":["binance:BTCUSDT","aster:BTCUSDT-PERP"],'
        '"accounts":["binance_paper","paper_main"],'
        '"files": '
    )

    generated = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={"_raw": raw},
            metadata={
                "original_user_prompt": (
                    "做一个 Binance 现货 + Aster 永续的 cash-and-carry 套利策略"
                )
            },
        ),
        config=cfg,
    )

    assert generated.is_error is False, generated.text()
    data = generated.content[0].data
    assert data["strategy_id"] == "binance_aster_cash_carry"
    assert data["proposal_id"]
    proposal_root = (
        tmp_path / "evolution" / "proposals" / data["proposal_id"]
    )
    proposal_text = (proposal_root / "proposal.yml").read_text(encoding="utf-8")
    strategy_text = (
        proposal_root
        / "after"
        / "strategies"
        / "binance_aster_cash_carry"
        / "strategy.yml"
    ).read_text(encoding="utf-8")
    combined = f"{proposal_text}\n{strategy_text}".lower()
    for needle in ("cash", "carry", "aster", "binance"):
        assert needle in combined


def test_strategy_generate_proposal_handler_reports_truncated_custom_files_payload(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import strategy_generate_proposal_handler
    from nerya.tools.types import ToolCall

    cfg = Config(paths=WorkspacePaths(root=tmp_path))
    raw = (
        '{"strategy_id":"bsc_meme_whale_copytrade",'
        '"title":"BSC Meme Whale Net-Inflow Copy-Trade",'
        '"description":"Use BSC wallet/on-chain whale inflow evidence.",'
        '"prompt":"When whale net inflow rises in a 5m window, dispatch an Agent.",'
        '"strategy_class":"agent",'
        '"execution_mode":"agent_task",'
        '"mode":"paper",'
        '"markets":["ONCHAIN:bsc:0x<meme_token_contract>"],'
        '"accounts":["paper_main"],'
        '"files": '
    )

    generated = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={"_raw": raw},
            metadata={
                "original_user_prompt": (
                    "BSC 上某 meme 币，0x 大鲸地址（>$1M）净流入 "
                    "5 分钟内增加时让 Agent 决定是否跟单"
                )
            },
        ),
        config=cfg,
    )

    assert generated.is_error is True
    text = generated.text()
    assert "truncated" in text
    assert "files.main.py" in text
    assert "files.strategy.md" in text
    assert "compact" in text
    assert not list(tmp_path.glob("evolution/proposals/prp_*"))


def test_polymarket_strategy_prompt_path_generates_and_backtests_freeform(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import (
        strategy_backtest_handler,
        strategy_generate_proposal_handler,
    )
    from nerya.tools.types import ToolCall

    cfg = Config(paths=WorkspacePaths(root=tmp_path))
    files = {
        "main.py": "\n".join(
            [
                "def run(ctx):",
                "    market = ctx.config.markets[0]",
                "    if not str(market).upper().startswith('POLYMARKET:'):",
                "        return ctx.result.hold(reason='unsupported market scope')",
                "    return ctx.result.hold(",
                "        reason='paper review uses the strategy-local CLOB event replay'",
                "    )",
            ]
        ),
        "strategy.md": "\n".join(
            [
                "# Polymarket Headline Edge",
                "",
                "Market scope assumption: use the configured Polymarket CLOB market.",
                "The strategy watches event probability and headline context, "
                "then stays paper-only.",
                "The review backtest is a strategy-local replay of archived "
                "midpoint observations.",
            ]
        ),
        "backtests/research_backtest.py": "\n".join(
            [
                "import json",
                "payload = {",
                "  'initial_capital_usd': 10000,",
                "  'equity_curve': [",
                "    {'ts': '2026-05-20T00:00:00Z', 'equity': 10000},",
                "    {'ts': '2026-05-20T06:00:00Z', 'equity': 10080},",
                "    {'ts': '2026-05-20T12:00:00Z', 'equity': 10150},",
                "  ],",
                "  'trades': [",
                (
                    "    {'ts': '2026-05-20T03:00:00Z', 'side': 'buy_yes', "
                    "'price': 0.42, 'size': 100, 'equity': 10080, "
                    "'reason': 'yes_mid_breakout'},"
                ),
                (
                    "    {'ts': '2026-05-20T10:00:00Z', 'side': 'sell_yes', "
                    "'price': 0.48, 'size': 100, 'equity': 10150, "
                    "'reason': 'risk_trim'},"
                ),
                "  ],",
                (
                    "  'limitations': ['Archived midpoint replay; final event "
                    "resolution is not modeled.'],"
                ),
                "}",
                "print('NERYA_FREEFORM_RESULT_JSON=' + json.dumps(payload))",
            ]
        ),
    }

    generated = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "polymarket_headline_edge",
                "title": "Polymarket Headline Edge",
                "description": "Create a Polymarket strategy and backtest it.",
                "prompt": "你能帮我创建polymarket策略并回测吗",
                "strategy_class": "news",
                "markets": ["POLYMARKET:event-slug"],
                "accounts": ["paper_polymarket"],
                "files": files,
            },
        ),
        config=cfg,
    )

    assert generated.is_error is False
    proposal_id = generated.content[0].data["proposal_id"]
    assert generated.content[0].data["backtest_required"] is True

    backtest = strategy_backtest_handler(
        ToolCall(
            name="strategy_backtest",
            arguments={
                "proposal_id": proposal_id,
                "preset": "default",
                "allow_mock": False,
            },
        ),
        config=cfg,
    )

    data = backtest.content[0].data
    assert data["ok"] is True
    assert data["strategy_id"] == "polymarket_headline_edge"
    assert data["proposal_id"] == proposal_id
    assert data["kind"] == "freeform_backtest"
    assert data["coverage_ok"] is True
    assert data["total_trades"] == 2
    assert data["metrics_are_display_strings"] is True
    assert Path(data["raw_metrics_file"]).exists()
    assert Path(data["chart_path"]).exists()


def test_strategy_backtest_handler_rejects_placeholder_promoted_strategy_when_proposal_matches(
    tmp_path: Path,
) -> None:
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler
    from nerya.tools.types import ToolCall

    paths = WorkspacePaths(root=tmp_path)
    strategy_root = paths.strategies / "solana_smart_money_meme"
    strategy_root.mkdir(parents=True, exist_ok=True)
    (strategy_root / "strategy.yml").write_text(
        "strategy_id: solana_smart_money_meme\n"
        "markets:\n"
        "  - BYREAL_ONCHAIN:solana:unknown\n",
        encoding="utf-8",
    )
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="proposal target",
        extra_files={
            "after/strategies/solana_smartmoney_meme/strategy.yml": (
                "strategy_id: solana_smartmoney_meme\n"
                "markets:\n"
                "  - BYREAL_ONCHAIN:solana:real-token\n"
            ),
        },
    )

    result = strategy_backtest_handler(
        ToolCall(
            name="strategy_backtest",
            arguments={"strategy_id": "solana_smart_money_meme"},
        ),
        config=Config(paths=paths),
    )

    data = result.content[0].data
    assert data["ok"] is False
    assert data["reason"] == "proposal_id_required_for_matching_inflight_proposal"
    assert data["matching_proposals"][0]["proposal_id"] == proposal.id
    assert data["matching_proposals"][0]["strategy_id"] == "solana_smartmoney_meme"
    assert "proposal_id" in data["message"]


def test_backtest_mock_market_exposes_common_aliases() -> None:
    rows = [
        {"open": 99, "high": 101, "low": 98, "close": 100, "volume": 1},
        {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 2},
    ]
    market = MockMarket("MOCK:BTCUSDT", {"MOCK:BTCUSDT": rows})

    assert market.get_candles("MOCK:BTCUSDT", interval="1m", count=1)[0]["close"] == 101
    assert market.candles("MOCK:BTCUSDT", "1m", 1)[0]["close"] == 101
    assert market.candles("1m", limit=1)[0]["close"] == 101
    assert market.candles("MOCK:BTCUSDT", symbol="MOCK:BTCUSDT", timeframe="1m", limit=1)[0]["close"] == 101
    assert market.get_ticker("MOCK:BTCUSDT")["mid"] == 101


def test_backtest_mock_context_exposes_current_symbol_and_timeframe() -> None:
    cfg = BacktestConfig(tf="15m", markets=["MOCK:BTCUSDT"])
    rows = [
        {"ts": 1, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1},
        {"ts": 2, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 2},
    ]
    ctx = MockCtx(
        strategy_id="agent_task_strategy",
        market_name="MOCK:BTCUSDT",
        bars_by_market={"MOCK:BTCUSDT": rows},
        current_bar={"ts": 1, "close": 100},
        pending_orders=[],
        config_obj=cfg,
        state=MockState(),
    )

    assert ctx.symbol == "MOCK:BTCUSDT"
    assert ctx.timeframe == "15m"
    assert ctx.ohlcv("MOCK:BTCUSDT", "15m", 1)[0]["close"] == 101
    assert ctx.get_ohlcv("MOCK:BTCUSDT", "15m", 1)[0]["close"] == 101
    assert ctx.get_candles("MOCK:BTCUSDT", timeframe="15m", limit=1)[0]["close"] == 101
    assert ctx.klines("MOCK:BTCUSDT", timeframe="15m", limit=1)[0]["close"] == 101
    assert ctx.market.get_ohlcv("MOCK:BTCUSDT", "15m", 1)[0]["close"] == 101
    assert ctx.market.klines("MOCK:BTCUSDT", timeframe="15m", limit=1)[0]["close"] == 101
    assert ctx.history("MOCK:BTCUSDT", "15m", "close", length=2) == [100.0, 101.0]


def test_portfolio_sell_fill_adds_cash():
    portfolio = PortfolioState(1000)
    portfolio.apply_fill({"market": "MOCK:BTCUSDT", "side": "buy", "qty": 1, "price": 100, "fee": 0})
    portfolio.apply_fill({"market": "MOCK:BTCUSDT", "side": "sell", "qty": 1, "price": 110, "fee": 0})
    portfolio.mark_to_market(1, {"MOCK:BTCUSDT": 110})
    assert portfolio.cash == 1010
    assert portfolio.realized_pnl == 10
    assert portfolio.snapshot()["equity"] == 1010


def test_backtest_default_preset_allows_short():
    """A faithful long/short backtest must permit shorts out of the box."""
    cfg = load_config(preset="default", markets=["MOCK:BTCUSDT"])
    assert cfg.allow_short is True
    assert BacktestConfig().allow_short is True


def _flat_rows(n: int = 4, price: float = 100.0) -> list[dict]:
    return [_bar(1_700_000_000 + i * 3600, price) for i in range(n)]


def test_backtest_pct_nav_entry_sizes_against_nav():
    """``sizing={'method':'pct_nav'}`` must resolve to a fraction of NAV.

    Regression: the control-plane shim used to drop pct_nav to size=0,
    and the engine then over-sized to 100% of cash and rejected the
    entry with ``insufficient_cash``.
    """

    def strat(ctx):
        market = ctx.config.markets[0]
        if not ctx.portfolio.positions(market):
            return ctx.trading.open_position(
                market=market,
                side="long",
                sizing={"method": "pct_nav", "pct_nav": 0.25},
                confidence=0.6,
                reasoning_ref="pct entry",
            )
        return ctx.result.hold(reason="hold")

    cfg = load_config(
        preset="default",
        markets=["MOCK:BTCUSDT"],
        overrides={"warmup_bars": 0, "window_days": 30},
    )
    result = run_backtest(
        None,
        cfg,
        candles_by_market={"MOCK:BTCUSDT": _flat_rows()},
        run_fn=strat,
        strategy_config={"strategy_id": "pct_long", "markets": ["MOCK:BTCUSDT"]},
    )

    entries = [t for t in result.trades if t["side"] == "buy" and not t.get("forced_close")]
    assert entries, [r.get("reject_reason") for r in result.rejected_signals]
    # 25% of the $10,000 starting NAV.
    assert entries[0]["notional"] == pytest.approx(2500.0, rel=0.02)
    reject_reasons = {r.get("reject_reason") for r in result.rejected_signals}
    assert "insufficient_cash" not in reject_reasons
    assert "zero_size" not in reject_reasons


def test_backtest_control_plane_short_opens_and_is_visible():
    """``open_position(side='short')`` must open a real short the strategy can see."""

    seen_positions: list[list[dict]] = []

    def strat(ctx):
        market = ctx.config.markets[0]
        pos = ctx.portfolio.positions(market)
        seen_positions.append(list(pos))
        if not pos:
            return ctx.trading.open_position(
                market=market,
                side="short",
                sizing={"method": "pct_nav", "pct_nav": 0.2},
                confidence=0.6,
                reasoning_ref="short entry",
            )
        return ctx.result.hold(reason="holding short")

    cfg = load_config(
        preset="default",
        markets=["MOCK:BTCUSDT"],
        overrides={"warmup_bars": 0, "window_days": 30},
    )
    result = run_backtest(
        None,
        cfg,
        candles_by_market={"MOCK:BTCUSDT": _flat_rows()},
        run_fn=strat,
        strategy_config={"strategy_id": "short_strat", "markets": ["MOCK:BTCUSDT"]},
    )

    short_entries = [t for t in result.trades if t["side"] == "sell" and not t.get("forced_close")]
    assert short_entries, [r.get("reject_reason") for r in result.rejected_signals]
    assert short_entries[0]["qty"] > 0
    assert short_entries[0]["notional"] == pytest.approx(2000.0, rel=0.02)

    visible = [p for p in seen_positions if p]
    assert visible, "strategy never saw its open short position"
    assert visible[0][0]["side"] == "short"
    assert visible[0][0]["size"] < 0  # signed: negative = short

    reject_reasons = {r.get("reject_reason") for r in result.rejected_signals}
    assert "no_open_position" not in reject_reasons
    assert "short_not_allowed" not in reject_reasons


def test_backtest_short_rejected_when_allow_short_disabled():
    """With allow_short=false a short entry is rejected with a clear reason."""

    def strat(ctx):
        market = ctx.config.markets[0]
        if not ctx.portfolio.positions(market):
            return ctx.trading.open_position(
                market=market,
                side="short",
                sizing={"method": "pct_nav", "pct_nav": 0.2},
                reasoning_ref="short entry",
            )
        return ctx.result.hold(reason="hold")

    cfg = load_config(
        preset="default",
        markets=["MOCK:BTCUSDT"],
        overrides={"warmup_bars": 0, "window_days": 30, "allow_short": False},
    )
    result = run_backtest(
        None,
        cfg,
        candles_by_market={"MOCK:BTCUSDT": _flat_rows()},
        run_fn=strat,
        strategy_config={"strategy_id": "short_blocked", "markets": ["MOCK:BTCUSDT"]},
    )

    assert not [t for t in result.trades if not t.get("forced_close")]
    assert "short_not_allowed" in [r.get("reject_reason") for r in result.rejected_signals]


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

    monkeypatch.setattr(
        "nerya.skills.builtin.backtest.scripts.data_cache._download_binance_vision_payload",
        lambda *_args, **_kwargs: payload.getvalue(),
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


def test_binance_vision_uses_bounded_request_timeout_and_source_fallback(
    monkeypatch,
    tmp_path: Path,
):
    from nerya.skills.builtin.backtest.scripts import data_cache

    seen_timeouts: list[float] = []

    def fake_download(_url, *, timeout):
        seen_timeouts.append(timeout)
        return None

    def fake_fetch_candles(market, *, count, interval, allow_mock, config_like=None, **_kwargs):
        del market, count, interval, allow_mock, config_like
        return [
            {"ts": 1_700_000_000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
            {"ts": 1_700_003_600, "open": 2, "high": 3, "low": 2, "close": 3, "volume": 11},
        ]

    monkeypatch.setenv("NERYA_BACKTEST_BINANCE_VISION_REQUEST_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("NERYA_BACKTEST_BINANCE_VISION_TOTAL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setattr(data_cache, "_download_binance_vision_payload", fake_download)
    monkeypatch.setattr(data_cache, "fetch_candles", fake_fetch_candles)

    rows = get_candles(
        "BINANCE:BTCUSDT",
        "1h",
        1_700_000_000,
        1_700_004_000,
        tmp_path,
        allow_mock=False,
    )

    assert seen_timeouts
    assert set(seen_timeouts) == {0.5}
    assert [row["close"] for row in rows] == [2, 3]


def test_binance_vision_total_timeout_bounds_multi_day_archive_scan(monkeypatch):
    from nerya.skills.builtin.backtest.scripts import data_cache

    opened_urls: list[str] = []
    ticks = iter([0.0, 0.0, 0.2])

    def fake_monotonic():
        return next(ticks, 0.2)

    def fake_download(url, **_kwargs):
        opened_urls.append(str(url))
        return None

    monkeypatch.setenv("NERYA_BACKTEST_BINANCE_VISION_TOTAL_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(data_cache.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(data_cache, "_download_binance_vision_payload", fake_download)

    rows = data_cache._fetch_binance_vision(
        "BINANCE:BTCUSDT",
        tf="1h",
        start=1_700_000_000,
        end=1_700_000_000 + (10 * 86400),
    )

    assert rows == []
    assert len(opened_urls) == 1


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

    def _fake_download(url, *_args, **_kwargs):
        seen_urls.append(str(url))
        return payload.getvalue()

    monkeypatch.setattr(
        "nerya.skills.builtin.backtest.scripts.data_cache._download_binance_vision_payload",
        _fake_download,
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
