"""Contract tests for the derivatives-aware ccxt connector.

These verify the P0.4 fixes: ``place_order`` now forwards
``reduceOnly``, leverage, margin mode, ``positionIdx``, and native
SL/TP bracket levels, plus precision rounding and contract-size
conversion. A fake ccxt client records every call so we can assert on
the exact params the venue would receive — no real network needed.

The fake client mimics ccxt 4.x's ``create_order`` / ``cancel_order`` /
``fetch_positions`` / ``fetch_open_orders`` / ``fetch_my_trades`` /
``load_markets`` / ``set_leverage`` / ``set_margin_mode`` shapes.
"""

from __future__ import annotations

from typing import Any

import pytest

from nerya.connectors.ccxt_adapter import (
    CcxtConnector,
    _extract_bracket_order_ids,
)
from nerya.connectors.cex_base import CEXCredentials
from nerya.core.errors import TradingError


class FakeCcxtClient:
    """Records calls and returns canned ccxt-shaped responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._markets = {
            "SOL/USDT:USDT": {
                "swap": True,
                "contract": True,
                "contractSize": 1.0,
                "precision": {"amount": 2, "price": 2},
                "limits": {"amount": {"min": 1.0, "step": 1.0}},
            },
            "BTC/USDT": {
                "spot": True,
                "precision": {"amount": 8, "price": 2},
                "limits": {"amount": {"min": 0.0001, "step": 0.0001}},
            },
        }
        # Canned responses, keyed by method. Tests can override.
        self._responses: dict[str, Any] = {
            "create_order": {
                "id": "ex-order-1",
                "clientOrderId": "nerya-coid-1",
                "status": "filled",
                "symbol": "SOL/USDT:USDT",
                "side": "buy",
                "price": 150.0,
                "amount": 10.0,
                "filled": 10.0,
                "average": 149.5,
                "fee": {"currency": "USDT", "cost": 0.7475},
            },
            "cancel_order": {"id": "ex-order-1", "status": "canceled"},
            "fetch_positions": [
                {
                    "symbol": "SOL/USDT:USDT",
                    "side": "long",
                    "contracts": 10.0,
                    "contractSize": 1.0,
                    "entryPrice": 149.5,
                    "markPrice": 152.0,
                    "notional": 1520.0,
                    "initialMargin": 152.0,
                    "unrealisedPnl": 25.0,
                    "leverage": 10.0,
                    "liquidationPrice": 135.0,
                }
            ],
            "fetch_open_orders": [],
            "fetch_my_trades": [],
        }

    def load_markets(self):
        self.calls.append(("load_markets", (), {}))
        return self._markets

    def set_leverage(self, leverage, symbol):
        self.calls.append(("set_leverage", (leverage, symbol), {}))

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("set_margin_mode", (mode, symbol), {}))

    def create_order(self, symbol, otype, side, amount, price, params):
        self.calls.append(
            ("create_order", (symbol, otype, side, amount, price), dict(params))
        )
        return dict(self._responses["create_order"])

    def cancel_order(self, order_id, symbol):
        self.calls.append(("cancel_order", (order_id, symbol), {}))
        return dict(self._responses["cancel_order"])

    def fetch_order(self, order_id, symbol):
        self.calls.append(("fetch_order", (order_id, symbol), {}))
        return dict(self._responses["create_order"])

    def fetch_positions(self, symbols=None):
        self.calls.append(("fetch_positions", (symbols,), {}))
        return [dict(p) for p in self._responses["fetch_positions"]]

    def fetch_open_orders(self, symbol=None):
        self.calls.append(("fetch_open_orders", (symbol,), {}))
        return list(self._responses["fetch_open_orders"])

    def fetch_my_trades(self, symbol=None, params=None):
        self.calls.append(("fetch_my_trades", (symbol,), dict(params or {})))
        return list(self._responses["fetch_my_trades"])

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"


def _deriv_connector() -> tuple[CcxtConnector, FakeCcxtClient]:
    fake = FakeCcxtClient()
    conn = CcxtConnector(
        exchange_id="bybit",
        credentials=CEXCredentials(api_key="k", api_secret="s"),
        live=True,
    )
    conn._client = fake  # bypass real ccxt
    return conn, fake


def test_place_order_forwards_reduce_only_leverage_and_bracket():
    """P0.4: a derivatives order carries reduceOnly, positionIdx, leverage,
    margin mode, and native SL/TP bracket prices in the ccxt params."""
    conn, fake = _deriv_connector()
    conn.place_order(
        market="BYBIT:SOL/USDT:USDT",
        side="buy",
        order_type="market",
        size=10.0,
        client_order_id="nerya-coid-1",
        reduce_only=False,
        leverage=10.0,
        margin_mode="isolated",
        position_idx=1,
        stop_loss=140.0,
        take_profit=170.0,
    )

    create_calls = [c for c in fake.calls if c[0] == "create_order"]
    assert len(create_calls) == 1
    _method, args, params = create_calls[0]
    symbol, otype, side, amount, price = args
    assert symbol == "SOL/USDT:USDT"
    assert otype == "market"
    assert side == "buy"
    # Leverage + margin mode set before the order.
    assert any(c[0] == "set_leverage" and c[1] == (10, "SOL/USDT:USDT") for c in fake.calls)
    assert any(
        c[0] == "set_margin_mode" and c[1] == ("isolated", "SOL/USDT:USDT")
        for c in fake.calls
    )
    # Bracket + reduce flags forwarded into params.
    assert params.get("stopLossPrice") == "140.00"
    assert params.get("takeProfitPrice") == "170.00"
    assert params.get("positionIdx") == 1
    # reduce_only=False on an open, so reduceOnly must NOT be set.
    assert "reduceOnly" not in params


def test_derivative_amount_converts_to_contracts_before_minimum_check():
    conn, fake = _deriv_connector()
    fake._markets["SOL/USDT:USDT"]["contractSize"] = 0.001

    conn.place_order(
        market="BYBIT:SOL/USDT:USDT",
        side="buy",
        order_type="market",
        size=0.01,
    )

    create_calls = [call for call in fake.calls if call[0] == "create_order"]
    assert len(create_calls) == 1
    assert create_calls[0][1][3] == 10.0


def test_below_minimum_amount_fails_without_creating_order():
    conn, fake = _deriv_connector()
    fake._markets["SOL/USDT:USDT"]["contractSize"] = 0.001

    with pytest.raises(TradingError, match="below the exchange minimum"):
        conn.place_order(
            market="BYBIT:SOL/USDT:USDT",
            side="buy",
            order_type="market",
            size=0.0005,
        )

    assert not any(call[0] == "create_order" for call in fake.calls)


def test_derivative_setup_failure_fails_without_creating_order(monkeypatch):
    conn, fake = _deriv_connector()

    def fail_set_leverage(leverage, symbol):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(fake, "set_leverage", fail_set_leverage)
    with pytest.raises(TradingError, match="failed to set leverage"):
        conn.place_order(
            market="BYBIT:SOL/USDT:USDT",
            side="buy",
            order_type="market",
            size=10.0,
            leverage=10.0,
        )

    assert not any(call[0] == "create_order" for call in fake.calls)


def test_place_order_reduce_only_close_sets_flag():
    """P0.4: a close (reduce_only=True) forwards reduceOnly so the venue
    treats it as a position reducer, not a reverse."""
    conn, fake = _deriv_connector()
    conn.place_order(
        market="BYBIT:SOL/USDT:USDT",
        side="sell",
        order_type="market",
        size=10.0,
        reduce_only=True,
    )
    create_calls = [c for c in fake.calls if c[0] == "create_order"]
    _method, _args, params = create_calls[0]
    assert params.get("reduceOnly") is True


def test_place_order_spot_does_not_forward_derivatives_params():
    """P0.4: a spot order must NOT carry reduceOnly/positionIdx/bracket —
    spot venues reject those."""
    conn, fake = _deriv_connector()
    conn.place_order(
        market="BYBIT:BTC/USDT",
        side="buy",
        order_type="market",
        size=0.001,
        reduce_only=True,
        leverage=5.0,
        stop_loss=40000.0,
    )
    create_calls = [c for c in fake.calls if c[0] == "create_order"]
    _method, _args, params = create_calls[0]
    assert "reduceOnly" not in params
    assert "stopLossPrice" not in params
    assert "positionIdx" not in params
    # No leverage/margin calls for spot.
    assert not any(c[0] == "set_leverage" for c in fake.calls)


def test_place_order_returns_bracket_order_ids():
    """P0.4/P0.6: when the venue attaches native SL/TP orders, their ids
    surface on the ack's ``attached_bracket_order_ids``."""
    conn, fake = _deriv_connector()
    fake._responses["create_order"] = {
        "id": "ex-order-1",
        "status": "filled",
        "symbol": "SOL/USDT:USDT",
        "side": "buy",
        "amount": 10.0,
        "filled": 10.0,
        "average": 149.5,
        "stopLoss": {"orderId": "sl-123"},
        "takeProfit": {"orderId": "tp-456"},
    }
    ack = conn.place_order(
        market="BYBIT:SOL/USDT:USDT",
        side="buy",
        order_type="market",
        size=10.0,
        stop_loss=140.0,
        take_profit=170.0,
    )
    assert ack.attached_bracket_order_ids == {"stop_loss": "sl-123", "take_profit": "tp-456"}


def test_fetch_positions_returns_contract_positions():
    """P0.7: ``fetch_positions`` maps ccxt rows to ContractPosition with
    margin, uPnL, and size_base folded from contractSize."""
    conn, fake = _deriv_connector()
    positions = conn.fetch_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.market == "bybit:SOL/USDT:USDT"
    assert p.side == "long"
    assert p.contracts == 10.0
    assert p.size_base == 10.0  # contractSize=1.0
    assert p.initial_margin_usd == 152.0
    assert p.unrealised_pnl_usd == 25.0
    assert p.leverage == 10.0
    assert p.liquidation_price == 135.0


def test_extract_bracket_order_ids_handles_bybit_v5_dict_shape():
    raw = {
        "id": "1",
        "stopLoss": {"orderId": "sl-1"},
        "takeProfit": {"orderId": "tp-1"},
    }
    assert _extract_bracket_order_ids(raw) == {"stop_loss": "sl-1", "take_profit": "tp-1"}


def test_extract_bracket_order_ids_handles_scalar_shape():
    raw = {"id": "1", "stopLossOrderId": "sl-9", "takeProfitOrderId": "tp-9"}
    assert _extract_bracket_order_ids(raw) == {"stop_loss": "sl-9", "take_profit": "tp-9"}


def test_extract_bracket_order_ids_empty_when_absent():
    assert _extract_bracket_order_ids({"id": "1"}) == {}
