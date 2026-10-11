"""Units must survive linear, inverse and option markets without dollar assumptions."""
from decimal import Decimal

import pytest

from nerya.core.errors import TradingError
from nerya.trading.account_state import AccountState, CollectionEvidence
from nerya.trading.instruments import Instrument, Greeks, number
from test_trading_kernel_safety import snapshot

pytestmark = pytest.mark.smoke


def test_linear_contract_quantity_and_cash_payoff():
    instrument = Instrument.from_ccxt("bybit", {"symbol": "BTC/USDT:USDT", "base": "BTC", "quote": "USDT",
                                               "settle": "USDT", "contract": True, "contractSize": "0.01"})
    assert instrument.base_quantity(10, 60000) == Decimal("0.1")
    assert instrument.order_quantity("0.1", 60000) == 10
    assert instrument.quote_value(10, 60000) == 6000
    assert instrument.pnl(10, 60000, 61000) == 100


def test_inverse_amount_and_pnl_are_not_linear_or_usd_collateral():
    instrument = Instrument.from_ccxt("deribit", {"symbol": "BTC/USD:BTC", "base": "BTC", "quote": "USD",
                                                  "settle": "BTC", "contract": True, "inverse": True, "contractSize": 10})
    assert instrument.base_quantity(100, 20000) == Decimal("0.05")
    assert instrument.order_quantity("0.05", 20000) == 100
    assert instrument.quote_value(100, 20000) == 1000
    assert instrument.pnl(100, 10000, 20000) == Decimal("0.05")
    assert instrument.pnl(-100, 10000, 20000) == Decimal("-0.05")


def test_option_premium_remains_in_quote_currency_and_contract_terms_are_required():
    raw = {"symbol": "ETH/USD:ETH-261030-3000-C", "base": "ETH", "quote": "ETH", "settle": "ETH",
           "option": True, "contract": True, "contractSize": "0.1", "expiry": 1793366400000,
           "strike": "3000", "optionType": "call","info":{"quote_currency":"ETH"}}
    option = Instrument.from_ccxt("deribit", raw)
    assert option.quote_value(10, "0.025") == Decimal("0.025")
    assert option.pnl(-10, "0.025", "0.04") == Decimal("-0.015")
    assert option.pnl_currency == "ETH"
    assert Instrument.from_ccxt("deribit", {**raw, "quote": "USD"}).price_currency == "ETH"
    with pytest.raises(TradingError, match="incomplete_option_contract"):
        Instrument.from_ccxt("deribit", {**raw, "expiry": None})
    with pytest.raises(TradingError,match="premium_currency_unavailable"):
        Instrument.from_ccxt("deribit",{**raw,"info":{}})


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", float("inf")])
def test_nonfinite_risk_inputs_are_rejected(value):
    with pytest.raises(TradingError):
        number(value)


def test_greeks_account_for_short_direction_and_quantity():
    assert Greeks("0.5", "0.01", 2, "-0.3").scaled(-2).asdict() == {
        "delta": "-1.0", "gamma": "-0.02", "vega": "-4", "theta": "0.6"}


def test_unknown_and_unsupported_collections_never_claim_empty_fresh_positions():
    state = AccountState.from_snapshot(snapshot())
    with pytest.raises(TradingError, match="positions"):
        state.require(("positions",), max_age=60)
    evidence = CollectionEvidence("positions", "unsupported", None, "venue")
    assert not evidence.fresh(60)
    with pytest.raises(TradingError, match="provenance"):
        CollectionEvidence("positions", "ok", None, "venue")


def test_live_collection_has_independent_age_and_coverage():
    state = AccountState.from_snapshot(snapshot(meta={"collections": {
        "balances": {"status": "ok", "as_of": 100, "source": "venue"},
        "positions": {"status": "ok", "as_of": 90, "source": "venue", "coverage": ["BTC"]}}}))
    state.require(("balances",), max_age=5, now=103)
    with pytest.raises(TradingError, match="positions"):
        state.require(("positions",), max_age=5, now=103)
