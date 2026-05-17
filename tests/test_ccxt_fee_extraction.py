"""Live-order fee extraction for the CCXT adapter.

Previously :meth:`CcxtConnector.place_order` returned ``OrderAck`` with
``fee_usd=None`` and the execution engine hardcoded ``fee_usd=0.0`` on
the resulting :class:`Fill` — so every live fill silently lost its
fee. These tests pin the new extractor:

* USD-stable fee passes through as-is
* Quote-currency fee on a ``BTC/USDT`` pair is dollars
* Base-currency fee converts via the order's ``avg_price``
* Non-base / non-quote fee (e.g. BNB discount on a non-BNB pair) walks
  the ticker fallback
* Plural ``fees`` list aggregates across maker + taker + funding
* Broker that omits ``fee``/``fees`` entirely → ``fee_usd is None`` so
  callers know it's "swallowed" rather than literally zero
"""

from __future__ import annotations

import pytest

from nerya.connectors.ccxt_adapter import CcxtConnector
from nerya.connectors.cex_base import CEXCredentials


pytestmark = pytest.mark.smoke


class _StubClient:
    """Drop-in replacement for the ccxt client instance.

    Only implements the surface ``_extract_fee_usd`` touches:
    ``fetch_ticker``. ``place_order`` / ``fetch_order`` are exercised
    via raw payloads passed through directly.
    """

    def __init__(self, tickers: dict[str, float] | None = None) -> None:
        self.tickers = tickers or {}
        self.fetch_ticker_calls: list[str] = []

    def fetch_ticker(self, symbol: str) -> dict[str, float | None]:
        self.fetch_ticker_calls.append(symbol)
        price = self.tickers.get(symbol)
        return {"last": price} if price is not None else {"last": None}


def _connector(client: _StubClient | None = None) -> CcxtConnector:
    conn = CcxtConnector(
        exchange_id="binance",
        credentials=CEXCredentials(api_key="k", api_secret="s"),
        live=True,
    )
    conn._client = client if client is not None else _StubClient()
    return conn


def test_fee_in_usdt_passes_through_as_dollars():
    conn = _connector()
    fee_usd, breakdown = conn._extract_fee_usd(
        {"fee": {"currency": "USDT", "cost": 1.50}},
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd == pytest.approx(1.50)
    assert breakdown == {"USDT": 1.50}


def test_fee_in_base_currency_converts_via_avg_price():
    """Bought BTC at 50k, fee paid in BTC → fee_usd = 0.0001 * 50000 = $5."""
    conn = _connector()
    fee_usd, breakdown = conn._extract_fee_usd(
        {"fee": {"currency": "BTC", "cost": 0.0001}},
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd == pytest.approx(5.0)
    assert breakdown == {"BTC": 0.0001}


def test_fee_in_bnb_walks_ticker_fallback():
    client = _StubClient(tickers={"BNB/USDT": 600.0})
    conn = _connector(client)
    fee_usd, breakdown = conn._extract_fee_usd(
        {"fee": {"currency": "BNB", "cost": 0.005}},
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd == pytest.approx(0.005 * 600.0)
    assert breakdown == {"BNB": 0.005}
    assert client.fetch_ticker_calls == ["BNB/USDT"]


def test_fees_list_aggregates_across_maker_taker_funding():
    conn = _connector()
    fee_usd, breakdown = conn._extract_fee_usd(
        {
            "fees": [
                {"currency": "USDT", "cost": 0.75},  # maker fee in quote
                {"currency": "USDT", "cost": 0.25},  # taker fee in quote
            ],
        },
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd == pytest.approx(1.0)
    assert breakdown == {"USDT": 1.0}


def test_fees_single_and_plural_combined():
    """ccxt occasionally returns BOTH ``fee`` and ``fees`` populated.

    We must take the union and not double-count the same currency by
    silently dropping one of them.
    """
    conn = _connector()
    fee_usd, breakdown = conn._extract_fee_usd(
        {
            "fee": {"currency": "USDT", "cost": 1.0},
            "fees": [{"currency": "USDT", "cost": 2.0}],
        },
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd == pytest.approx(3.0)
    assert breakdown == {"USDT": 3.0}


def test_missing_fee_returns_none_not_zero():
    """Broker that doesn't report any fee → ``None``.

    The execution engine reads ``None`` differently from ``0`` so the
    reconciliation job knows to backfill from the fills endpoint
    instead of trusting the value.
    """
    conn = _connector()
    fee_usd, breakdown = conn._extract_fee_usd(
        {"id": "abc", "status": "closed"},
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd is None
    assert breakdown == {}


def test_unconvertible_fee_currency_returns_none_with_partial_breakdown():
    """Fee in an obscure asset the ticker fallback can't price → we
    surface ``None`` USD so callers don't anchor on a wrong number,
    but the asset still shows up in the breakdown for audit.
    """
    client = _StubClient(tickers={})  # no ticker prices available
    conn = _connector(client)
    fee_usd, breakdown = conn._extract_fee_usd(
        {"fee": {"currency": "OBSCURE", "cost": 0.5}},
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd is None
    assert breakdown == {"OBSCURE": 0.5}


def test_zero_cost_entries_skipped():
    """A fee entry with ``cost == 0`` is informational noise — must
    not trigger a ticker lookup or pollute the breakdown.
    """
    client = _StubClient(tickers={})
    conn = _connector(client)
    fee_usd, breakdown = conn._extract_fee_usd(
        {
            "fee": {"currency": "BNB", "cost": 0},
            "fees": [{"currency": "USDT", "cost": 0.5}],
        },
        market="binance:BTC/USDT",
        avg_price=50_000.0,
    )
    assert fee_usd == pytest.approx(0.5)
    assert breakdown == {"USDT": 0.5}
    assert client.fetch_ticker_calls == []
