from types import SimpleNamespace

import pytest

from nerya.connectors.ccxt_adapter import CcxtConnector
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke


def connector():
    calls = []
    market = {"symbol": "SOL/USDT", "base": "SOL", "quote": "USDT", "spot": True,
              "limits": {"amount": {"min": 0.001}, "cost": {"min": 1}}}
    client = SimpleNamespace(id="bybit", load_markets=lambda: {"SOL/USDT": market},
                             amount_to_precision=lambda s, v: str(v), price_to_precision=lambda s, v: str(v),
                             create_order=lambda *a: calls.append(a) or {"id": "venue", "status": "open", "amount": a[3], "price": a[4]})
    conn = CcxtConnector(exchange_id="bybit", live=True, _client=client)
    conn._check_live_and_keys = lambda: None
    return conn, calls


@pytest.mark.parametrize("side,bound", [("buy", 100.25), ("sell", 99.75)])
def test_market_order_is_ioc_with_a_venue_enforced_price_bound(side, bound):
    conn, calls = connector()
    conn.place_order(market="BYBIT:SOL/USDT", side=side, order_type="market", size=1,
                     reference_price=100, max_slippage_bps=25)
    assert calls[0][1] == "limit"
    assert calls[0][4] == bound
    assert calls[0][5]["timeInForce"] == "IOC"


@pytest.mark.parametrize("params", [{"qty": "10000"}, {"orderType": "Market"}, {"vaultAddress": "elsewhere"}])
def test_extra_parameters_cannot_change_the_authorized_spend_or_account(params):
    conn, calls = connector()
    with pytest.raises(TradingError, match="override"):
        conn.place_order(market="BYBIT:SOL/USDT", side="buy", order_type="market", size=1,
                         reference_price=100, max_slippage_bps=25, extra_params=params)
    assert calls == []
