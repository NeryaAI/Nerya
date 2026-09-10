"""Dedicated RiskGate tests — each money cap pinned independently.

The submit pipeline exercises the gate end-to-end, but a cap regression
(e.g. a refactor silently dropping a check) should fail HERE, with the
specific reason string, not deep inside an integration flow.
"""

from copy import deepcopy

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


def _build_workspace(
    tmp_root,
    *,
    venue: str = "bybit_perpetual",
    market: str = "SOL/USDT:USDT",
    limits: dict | None = None,
    accounts: list[str] | None = None,
    kill_switch: bool = False,
):
    cfg = Config(paths=WorkspacePaths(root=tmp_root), data=deepcopy(DEFAULT_CONFIG))
    paths = cfg.paths
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    if kill_switch:
        cfg.data["runtime"]["kill_switch"] = True
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [
                {
                    "id": acct,
                    "exchange": venue, "venue": venue, "mode": "paper",
                    "status": "active", "initial_balance_usd": 50_000,
                    "permissions": {
                        "read_balances": True, "place_order": True,
                        "cancel_order": True,
                    },
                }
                for acct in (accounts or ["acct_1"])
            ],
        },
    )
    yaml_io.dump(
        paths.strategy("alpha") / "strategy.yml",
        {
            "id": "alpha", "status": "paper", "account_id": "acct_1",
            "markets": [market],
            "paper_trading_enabled": True, "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        paths.strategy("alpha") / "limits.yml",
        {
            "allowed_markets": [market],
            "min_confidence": 0, "max_stale_seconds": 60,
            "approval_threshold_usd": 0,
            "max_single_order_usd": 100_000,
            **(limits or {}),
        },
    )
    return cfg


def _evaluate(
    cfg,
    *,
    account_id: str = "acct_1",
    market: str = "SOL/USDT:USDT",
    size: float = 1_000,
    size_unit: str = "usd",
):
    from nerya.trading.intents import TradeIntent
    from nerya.trading.risk import RiskGate

    intent = TradeIntent.new(
        strategy_id="alpha", account_id=account_id,
        market=market, side="buy",
        size=size, size_unit=size_unit, order_type="market",
        confidence=1.0,
        source="strategy_runtime",
    )
    return RiskGate(cfg).evaluate(
        intent,
        market_snapshot={"price": 100.0, "age_s": 1, "source": "test"},
    )


@pytest.mark.smoke
def test_max_single_order_cap_rejects_over_cap(tmp_path):
    cfg = _build_workspace(tmp_path / "cap", limits={"max_single_order_usd": 500})

    decision = _evaluate(cfg, size=1_000)

    assert decision.decision == "reject"
    assert any(
        r.startswith("max_single_order_exceeded:1000.00>500.00")
        for r in decision.reasons
    ), decision.reasons


@pytest.mark.smoke
def test_max_single_order_cap_allows_under_cap(tmp_path):
    cfg = _build_workspace(tmp_path / "under", limits={"max_single_order_usd": 500})

    decision = _evaluate(cfg, size=400)

    assert decision.decision == "allow", decision.reasons


@pytest.mark.smoke
def test_daily_notional_cap_rejects_first_exceeding_order(tmp_path):
    cfg = _build_workspace(tmp_path / "daily", limits={"max_daily_notional_usd": 500})

    decision = _evaluate(cfg, size=1_000)

    assert decision.decision == "reject"
    assert any(
        r.startswith("max_daily_notional_exceeded:0.00+1000.00>500.00")
        for r in decision.reasons
    ), decision.reasons


@pytest.mark.smoke
def test_strategy_daily_notional_starts_at_zero(tmp_path):
    from nerya.trading.risk import _strategy_daily_notional

    cfg = _build_workspace(tmp_path / "ledger")

    spent = _strategy_daily_notional(cfg.paths, "alpha")

    assert spent == 0.0


@pytest.mark.smoke
def test_venue_prefix_mismatch_rejected_on_real_venue(tmp_path):
    # A BINANCE-prefixed market must not execute on a bybit account:
    # the connector would strip the prefix and every cap/snapshot would
    # silently refer to the wrong venue.
    cfg = _build_workspace(
        tmp_path / "mismatch", venue="bybit", market="BINANCE:SOL/USDT",
    )

    decision = _evaluate(cfg, market="BINANCE:SOL/USDT")

    assert decision.decision == "reject"
    assert any(
        r.startswith("market_venue_account_mismatch:BINANCE:SOL/USDT!=bybit")
        for r in decision.reasons
    ), decision.reasons


@pytest.mark.smoke
def test_mock_account_exempt_from_venue_mismatch(tmp_path):
    # Paper rehearsal on the mock venue may carry any market prefix.
    cfg = _build_workspace(
        tmp_path / "mock", venue="mock", market="BINANCE:SOL/USDT",
    )

    decision = _evaluate(cfg, market="BINANCE:SOL/USDT")

    assert decision.decision == "allow", decision.reasons


@pytest.mark.smoke
def test_account_binding_mismatch_rejected(tmp_path):
    # The strategy is bound to acct_1; an intent routed at acct_2 must be
    # rejected before any snapshot/ledger on the wrong book is touched.
    cfg = _build_workspace(tmp_path / "binding", accounts=["acct_1", "acct_2"])

    decision = _evaluate(cfg, account_id="acct_2")

    assert decision.decision == "reject"
    assert any(
        r.startswith("account_binding_mismatch:strategy=acct_1!=intent=acct_2")
        for r in decision.reasons
    ), decision.reasons


@pytest.mark.smoke
def test_unknown_account_rejected(tmp_path):
    cfg = _build_workspace(tmp_path / "unknown")

    decision = _evaluate(cfg, account_id="acct_404")

    assert decision.decision == "reject"
    assert "account_unknown" in decision.reasons


@pytest.mark.smoke
def test_kill_switch_rejects_everything(tmp_path):
    cfg = _build_workspace(tmp_path / "kill", kill_switch=True)

    decision = _evaluate(cfg, size=1)

    assert decision.decision == "reject"
    assert "kill_switch_enabled" in decision.reasons


@pytest.mark.smoke
def test_approval_threshold_escalates(tmp_path):
    cfg = _build_workspace(
        tmp_path / "approval", limits={"approval_threshold_usd": 500},
    )

    decision = _evaluate(cfg, size=1_000)

    assert decision.decision == "escalate"
    assert any(
        r.startswith("approval_required_threshold:1000.00>=500.00")
        for r in decision.reasons
    ), decision.reasons
