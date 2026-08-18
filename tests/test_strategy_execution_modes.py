from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.strategies.context import (
    StrategyPolicyView,
    StrategyRuntimeError,
    StrategyTrading,
)
from nerya.strategies.runner import StrategyRunner
from nerya.trading.executors.orchestrator import ExecutorOrchestrator
from nerya.trading.executors.market_order import MarketOrderExecutor
from nerya.trading.order_intents import SizingPolicy, TradeEntry, TradePlan
from nerya.trading.position_book import PositionBook
from nerya.trading.submit import submit_trade_plan


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
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
                },
                {
                    "id": "live_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "live",
                    "status": "active",
                    "live_trading_enabled": True,
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                },
            ]
        },
    )
    return cfg


def _policy() -> StrategyPolicyView:
    return StrategyPolicyView(
        max_single_order_usd=1_000,
        max_daily_notional_usd=10_000,
        max_open_positions=10,
        min_confidence=0,
        allow_direct_order=True,
        require_subagent_before_order=False,
        default_order_usd=100,
        max_run_seconds=30,
        default_tier="light",
        allowed_tiers=("light",),
        max_calls_per_run=1,
        raw_policy={},
        raw_llm_policy={},
    )


def _trading(cfg: Config, *, mode: str, account: str) -> StrategyTrading:
    return StrategyTrading(
        config=cfg,
        strategy_id="mode_guard",
        policy=_policy(),
        accounts=(account,),
        execution_mode=mode,
        session_id="ses_mode_guard",
    )


def _write_paper_strategy(cfg: Config) -> None:
    yaml_io.dump(
        cfg.paths.strategy("mode_guard") / "strategy.yml",
        {
            "id": "mode_guard",
            "status": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("mode_guard") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0,
            "max_stale_seconds": 60,
        },
    )


def test_paper_run_cannot_reach_real_money_account(tmp_path) -> None:
    trading = _trading(_config(tmp_path), mode="paper", account="live_main")

    with pytest.raises(StrategyRuntimeError, match="paper run cannot submit"):
        trading.submit_intent(
            market="mock:BTC/USDT",
            side="buy",
            size=100,
            market_snapshot={"price": 50_000, "age_s": 0},
        )


def test_live_run_cannot_silently_fall_back_to_paper_account(tmp_path) -> None:
    trading = _trading(_config(tmp_path), mode="live", account="paper_main")

    with pytest.raises(StrategyRuntimeError, match="live run requires a canary/live"):
        trading.submit_intent(
            market="mock:BTC/USDT",
            side="buy",
            size=100,
            market_snapshot={"price": 50_000, "age_s": 0},
        )


def test_shadow_submission_never_calls_trading_kernel(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    trading = _trading(cfg, mode="shadow", account="paper_main")

    def _unexpected_submit(*_args, **_kwargs):
        raise AssertionError("shadow mode reached submit_trade_intent")

    monkeypatch.setattr(
        "nerya.trading.submit.submit_trade_intent",
        _unexpected_submit,
    )

    out = trading.submit_intent(
        market="mock:BTC/USDT",
        side="buy",
        size=100,
        order_type="stop_limit",
        limit_price=49_900,
        stop_price=50_100,
        market_snapshot={"price": 50_000, "age_s": 0},
    )

    assert out["status"] == "submitted"
    assert out["risk_decision"]["decision"] == "shadow"
    assert out["intent"]["order_type"] == "stop_limit"
    assert out["intent"]["limit_price"] == pytest.approx(49_900)
    assert out["intent"]["stop_price"] == pytest.approx(50_100)


def test_runner_threads_mode_override_into_context_before_submission(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    root = cfg.paths.strategy("mode_guard")
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "mode_guard",
            "title": "Mode guard",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "interval", "every_seconds": 60},
            "policy": {
                "max_single_order_usd": 1_000,
                "max_daily_notional_usd": 10_000,
                "max_open_positions": 10,
                "min_confidence": 0,
                "allow_direct_order": True,
                "max_run_seconds": 5,
            },
        },
    )
    (root / "main.py").write_text(
        "\n".join(
            [
                "def run(ctx):",
                "    assert ctx.mode == 'shadow'",
                "    assert ctx.trading.execution_mode == 'shadow'",
                "    execution = ctx.trading.submit_intent(",
                "        market='mock:BTC/USDT', side='buy', size=100,",
                "        market_snapshot={'price': 50000, 'age_s': 0},",
                "    )",
                "    return {'execution': execution, 'reason': 'shadow checked'}",
            ]
        ),
        encoding="utf-8",
    )

    def _unexpected_submit(*_args, **_kwargs):
        raise AssertionError("runner shadow override reached trading kernel")

    monkeypatch.setattr(
        "nerya.trading.submit.submit_trade_intent",
        _unexpected_submit,
    )

    record = StrategyRunner(config=cfg).run_tick(
        "mode_guard",
        mode_override="shadow",
    )

    assert record.mode == "shadow"
    assert record.status == "ok"
    result = record.outputs["result"]
    assert result["metadata"]["shadow"] is True
    assert result["metadata"]["original_intent"]["source"] == "strategy_runtime"


def test_paper_limit_and_stop_limit_orders_wait_for_real_mark_crossing(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    _write_paper_strategy(cfg)
    # The durable executor reloads this file while polling paper orders.
    yaml_io.dump(tmp_path / "nerya.yml", cfg.data)
    mark = [100.0]

    def _ticker(*_args, **_kwargs):
        return {"price": mark[0], "_envelope": {"mode": "paper", "source": "test"}}

    monkeypatch.setattr("nerya.data.candles.fetch_public_ticker", _ticker)

    out = submit_trade_plan(
        cfg,
        TradePlan(
            action="open_position",
            strategy_id="mode_guard",
            account_id="paper_main",
            market="mock:BTC/USDT",
            side="long",
            sizing=SizingPolicy(method="fixed_usd", fixed_usd=100),
            entry=TradeEntry(
                order_type="stop_limit",
                limit_price=95,
                stop_price=110,
            ),
            confidence=1.0,
            source="strategy_runtime",
        ),
        market_snapshot={"price": mark[0], "age_s": 0, "source": "test"},
    )

    assert out["status"] == "submitted"
    assert out["executor"]["state"] == "submitted"
    executor_id = out["executor_id"]
    assert PositionBook(cfg.paths).open_positions(account_id="paper_main") == []

    # Crossing the stop alone does not fill a stop-limit order if the limit
    # is still not marketable.
    mark[0] = 111
    assert ExecutorOrchestrator(cfg).run_once() == 1
    assert PositionBook(cfg.paths).open_positions(account_id="paper_main") == []

    # Once the price reaches the limit, the same durable executor fills once.
    mark[0] = 94
    assert ExecutorOrchestrator(cfg).run_once() == 1
    assert PositionBook(cfg.paths).open_positions(account_id="paper_main")
    assert ExecutorOrchestrator(cfg).run_once() == 0

    from nerya.trading.order_tracker import OrderTracker

    run = ExecutorOrchestrator(cfg)._load(executor_id)
    assert run is not None
    assert len(run.order_ids) == 1
    tracked = OrderTracker(cfg.paths).get(run.order_ids[0])
    assert tracked is not None
    assert tracked.state == "filled"


def test_paper_post_only_marketable_order_cancels_without_fill(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    _write_paper_strategy(cfg)
    yaml_io.dump(tmp_path / "nerya.yml", cfg.data)
    monkeypatch.setattr(
        "nerya.data.candles.fetch_public_ticker",
        lambda *_args, **_kwargs: {
            "price": 100.0,
            "_envelope": {"mode": "paper", "source": "test"},
        },
    )

    out = submit_trade_plan(
        cfg,
        TradePlan(
            action="open_position",
            strategy_id="mode_guard",
            account_id="paper_main",
            market="mock:BTC/USDT",
            side="long",
            sizing=SizingPolicy(method="fixed_usd", fixed_usd=100),
            entry=TradeEntry(
                order_type="limit",
                limit_price=105,
                time_in_force="post_only",
            ),
            confidence=1.0,
            source="strategy_runtime",
        ),
        market_snapshot={"price": 100.0, "age_s": 0, "source": "test"},
    )

    assert out["status"] == "canceled"
    assert PositionBook(cfg.paths).open_positions(account_id="paper_main") == []


def test_legacy_connector_without_bracket_parameters_fails_closed() -> None:
    class LegacyConnector:
        def place_order(
            self,
            *,
            market,
            side,
            order_type,
            size,
            price=None,
            client_order_id=None,
            time_in_force="gtc",
        ):
            return {"order_id": "legacy-1"}

    with pytest.raises(NotImplementedError, match="stop_loss"):
        MarketOrderExecutor._place_live_order(
            LegacyConnector(),
            market="mock:BTC/USDT",
            side="buy",
            order_type="market",
            size=1.0,
            price=None,
            client_order_id="coid",
            time_in_force="gtc",
            reduce_only=False,
            leverage=None,
            stop_loss=90.0,
            take_profit=None,
            trigger_price=None,
            extra_params=None,
        )
