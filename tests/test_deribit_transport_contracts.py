import json
import time
from types import SimpleNamespace

import pytest

from nerya.connectors.ccxt_adapter import CcxtConnector
from nerya.connectors.cex_base import CEXCredentials
from nerya.connectors.provider_spec import get_registry
from nerya.core.errors import TradingError
from nerya.trading.instruments import Instrument

pytestmark=pytest.mark.smoke
SYMBOL="BTC/USD:BTC-261030-100000-C"
SECOND="BTC/USD:BTC-261030-110000-C"


def market(symbol=SYMBOL,*,size=1):
    return {"id":"BTC-30OCT26-100000-C" if symbol==SYMBOL else "BTC-30OCT26-110000-C","symbol":symbol,
        "base":"BTC","quote":"USD","settle":"BTC","option":True,"contract":True,"inverse":True,"active":True,
        "expiry":int(time.time()*1000)+86400000,"strike":100000,"optionType":"call","contractSize":size,
        "info":{"kind":"option","contract_size":size,"min_trade_amount":.1,"settlement_currency":"BTC","quote_currency":"BTC",
                "expiration_timestamp":int(time.time()*1000)+86400000,"strike":100000,"option_type":"call"}}


class Client:
    enableRateLimit=True
    rateLimit=100
    calls=None
    def __init__(self):
        self.calls=[];self.markets={SYMBOL:market(),SECOND:market(SECOND)}
    def load_markets(self):return self.markets
    def market(self,symbol):return self.markets[symbol]
    def request(self,method,api,http,params,config):
        self.calls.append((method,api,http,params,config))
        if method=="simulate_portfolio":return {"result":{"currency":"BTC","equity":2,"margin_balance":2,
            "initial_margin":.2,"maintenance_margin":.1,"projected_initial_margin":.3,"projected_maintenance_margin":.15,
            "cross_collateral_enabled":False,"portfolio_margining_enabled":True,"margin_model":"segregated_pm"}}
        if method=="create_combo":return {"result":{"id":"BTC-COMBO","state":"active","legs":[
            {"instrument_name":self.markets[SYMBOL]["id"],"amount":1},{"instrument_name":self.markets[SECOND]["id"],"amount":-1}]}}
        if method=="get_instrument":return {"result":{"instrument_name":"BTC-COMBO","kind":"option_combo","is_active":True,
            "settlement_currency":"BTC","quote_currency":"BTC","min_trade_amount":.1,"tick_size":.0001}}
        if method in {"buy","sell"}:return {"result":{"order":{"order_id":"option-1"}}}
        raise AssertionError(method)


def connector():
    result=CcxtConnector(exchange_id="deribit",live=True,credentials=CEXCredentials(api_key="offline-key",api_secret="offline-secret"))
    result._client=Client()
    return result


def test_deribit_registered_as_ccxt_with_explicit_unverified_execution():
    spec=get_registry().find("deribit")
    assert spec.runtime=="python_ccxt" and "options" in spec.instrument_types
    assert spec.factory({"venue":"deribit"}).exchange_id=="deribit"
    assert spec.supports["place_order"] is False


def test_premium_currency_is_not_strike_currency():
    instrument=Instrument.from_ccxt("deribit",market())
    assert instrument.quote=="USD" and instrument.price_currency=="BTC"
    assert instrument.base_quantity(2,100000)==2


def test_deribit_amount_is_wire_base_quantity_not_generic_contract_multiplier():
    conn=connector();conn._client.markets[SYMBOL]=market(size=.1)
    terms=conn.deribit_option_terms(SYMBOL,2)
    assert terms["quantity_base"]=="0.2"
    assert conn._base_amount(SYMBOL,.2,price=.01)==.2
    conn._client.markets["BTC-PERP"]={"contract":True,"inverse":True,"option":False,"contractSize":10}
    assert conn._base_amount("BTC-PERP",1000,price=100000)==.01


def test_simulation_uses_current_endpoint_and_native_amount_map():
    conn=connector()
    result=conn.deribit_simulate_portfolio("BTC",{SYMBOL:-.2})
    call=conn._client.calls[-1]
    assert call[0]=="simulate_portfolio" and call[1]=="private"
    assert json.loads(call[3]["simulated_positions"])=={"BTC-30OCT26-100000-C":-.2}
    assert call[3]["add_positions"]=="true" and call[4]["cost"]==10
    assert result["portfolio_margining_enabled"]


def test_simulation_without_rate_limit_is_rejected():
    conn=connector();conn._client.enableRateLimit=False
    with pytest.raises(TradingError,match="rate_limit_required"):conn.deribit_simulate_portfolio("BTC",{SYMBOL:1})
    assert not conn._client.calls


def test_native_combo_is_authorized_before_creation_and_keeps_credit_bound():
    conn=connector();events=[]
    side,wire=conn.deribit_combo_order({SYMBOL:.2,SECOND:-.2},max_debit_quote=-.00355,label="controlled",valid_until=1,
        authorize=lambda:events.append("authorized"))
    assert events==["authorized"] and conn._client.calls[0][0]=="create_combo"
    assert side=="buy" and float(wire["price"])*float(wire["amount"])<=-.00355
    assert wire["time_in_force"]=="fill_or_kill" and wire["valid_until"]==1


def test_combo_creation_is_never_sent_when_producer_fence_fails():
    conn=connector()
    def blocked():raise TradingError("producer_expired")
    with pytest.raises(TradingError,match="producer_expired"):
        conn.deribit_combo_order({SYMBOL:1,SECOND:-1},max_debit_quote=.1,label="controlled",valid_until=1,authorize=blocked)
    assert not conn._client.calls


def test_order_label_persisted_before_send():
    conn=connector();events=[]
    wire={"label":"unique","instrument_name":"BTC-30OCT26-100000-C"}
    result=conn.deribit_send("sell",wire,authorize=lambda:events.append("authorized"),
        persist_before_send=lambda ref:events.append(("persisted",ref,len(conn._client.calls))))
    assert events[0]=="authorized" and events[1][0]=="persisted" and events[1][2]==0
    assert result["order"]["order_id"]=="option-1"


def test_generic_order_path_cannot_apply_linear_margin_to_options():
    conn=connector()
    with pytest.raises(TradingError,match="typed financial execution"):
        conn.place_order(market=SYMBOL,side="sell",order_type="limit",size=.2,price=.01)
    assert not conn._client.calls
