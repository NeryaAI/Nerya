"""Deribit instrument and portfolio operations through the owned CCXT client."""
import json
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING

from ..core.errors import TradingError
from ..trading.instruments import number


def rpc(connector,method,api,params):
    if connector.exchange_id!="deribit":raise TradingError("deribit_connector_required")
    if api=="private":connector._check_live_and_keys()
    config={}
    if method=="simulate_portfolio":
        if not connector.client.enableRateLimit:raise TradingError("deribit_simulation_rate_limit_required")
        config["cost"]=max(1.0,1000.0/connector.client.rateLimit)
    reply=connector.client.request(method,api,"GET",params,config=config)
    if not isinstance(reply,dict) or reply.get("error") or not isinstance(reply.get("result"),(dict,list)):
        raise TradingError("deribit_invalid_response")
    return reply["result"]


def option_terms(connector,symbol,contracts,premium=None):
    market=connector.client.market(connector._normalise_symbol(symbol))
    info=market.get("info") or {}
    if not market.get("option") or info.get("kind")!="option" or market.get("active") is not True:
        raise TradingError("active_deribit_option_required")
    count=number(contracts,positive=True)
    unit=number(info.get("contract_size"),positive=True)
    quantity=count*unit
    step=number(info.get("min_trade_amount"),positive=True)
    if quantity<step or quantity%step:raise TradingError("deribit_amount_increment")
    required=("settlement_currency","quote_currency","expiration_timestamp","strike","option_type")
    if any(info.get(key) is None for key in required):raise TradingError("deribit_option_contract_metadata_missing")
    result={"instrument_name":market["id"],"symbol":market["symbol"],"contracts":str(count),"quantity_base":str(quantity),
            "settlement_currency":info["settlement_currency"],"premium_currency":info["quote_currency"],
            "underlying":market["base"],"strike":str(info["strike"]),"expiry_ms":int(info["expiration_timestamp"]),"option_type":info["option_type"]}
    if premium is not None:result["premium_quote"]=str(number(premium,positive=True))
    return result


def simulate_portfolio(connector,currency,quantities,*,add_positions=True):
    if type(add_positions) is not bool:raise TradingError("invalid_add_positions")
    positions={}
    for symbol,quantity in quantities.items():
        quantity=number(quantity)
        if not quantity:continue
        market=connector.client.market(connector._normalise_symbol(symbol))
        terms=option_terms(connector,symbol,abs(quantity)/number(market["info"]["contract_size"],positive=True))
        if terms["settlement_currency"]!=currency:raise TradingError("simulation_settlement_mismatch")
        instrument=terms["instrument_name"]
        positions[instrument]=positions.get(instrument,Decimal(0))+quantity
    result=rpc(connector,"simulate_portfolio","private",{
        "currency":currency,"add_positions":"true" if add_positions else "false",
        "simulated_positions":json.dumps({key:float(value) for key,value in positions.items()},allow_nan=False,separators=(",",":"))})
    if result.get("currency")!=currency:raise TradingError("simulation_currency_mismatch")
    for key in ("equity","margin_balance","initial_margin","maintenance_margin","projected_initial_margin","projected_maintenance_margin"):
        number(result.get(key))
    if type(result.get("cross_collateral_enabled")) is not bool or type(result.get("portfolio_margining_enabled")) is not bool or not result.get("margin_model"):
        raise TradingError("simulation_margin_model_missing")
    return result


def combo_order(connector,quantities,*,max_debit_quote,label,valid_until,authorize):
    if not 2<=len(quantities)<=4:raise TradingError("invalid_combo_leg_count")
    if not isinstance(label,str) or not 1<=len(label)<=64 or type(valid_until) is not int:
        raise TradingError("invalid_combo_order_identity")
    wanted,trades,currencies={},[],set()
    for symbol,quantity in quantities.items():
        quantity=number(quantity)
        if not quantity:raise TradingError("zero_combo_leg")
        market=connector.client.market(connector._normalise_symbol(symbol))
        terms=option_terms(connector,symbol,abs(quantity)/number(market["info"]["contract_size"],positive=True))
        instrument=terms["instrument_name"]
        if instrument in wanted:raise TradingError("duplicate_combo_leg")
        wanted[instrument]=quantity
        currencies.add((terms["settlement_currency"],terms["premium_currency"],market["base"]))
        trades.append({"instrument_name":instrument,"amount":float(abs(quantity)),"direction":"buy" if quantity>0 else "sell"})
    if len(currencies)!=1:raise TradingError("mixed_combo_currencies")
    authorize()
    combo=rpc(connector,"create_combo","private",{"trades":json.dumps(trades,allow_nan=False,separators=(",",":"))})
    ratios={}
    for leg in combo.get("legs",[]):
        instrument=leg["instrument_name"];ratio=number(leg["amount"])
        if instrument in ratios or not ratio or ratio!=ratio.to_integral_value():raise TradingError("invalid_combo_ratio")
        ratios[instrument]=ratio
    if set(ratios)!=set(wanted):raise TradingError("combo_leg_identity_mismatch")
    factors={wanted[key]/ratios[key] for key in wanted}
    if len(factors)!=1 or combo.get("state")!="active":raise TradingError("combo_ratio_mismatch")
    scale=factors.pop();side="buy" if scale>0 else "sell"
    info=rpc(connector,"get_instrument","public",{"instrument_name":combo["id"]})
    settlement,premium,_=next(iter(currencies))
    if info.get("instrument_name")!=combo["id"] or info.get("kind")!="option_combo" or info.get("is_active") is not True or info.get("settlement_currency")!=settlement or info.get("quote_currency")!=premium:
        raise TradingError("combo_instrument_mismatch")
    step=number(info.get("min_trade_amount"),positive=True)
    if abs(scale)<step or abs(scale)%step:raise TradingError("combo_amount_increment")
    price=number(max_debit_quote)/scale
    tick=number(info.get("tick_size"),positive=True)
    for row in sorted(info.get("tick_size_steps") or [],key=lambda item:number(item["above_price"])):
        if price>number(row["above_price"]):tick=number(row["tick_size"],positive=True)
    rounding=ROUND_FLOOR if side=="buy" else ROUND_CEILING
    price=(price/tick).to_integral_value(rounding=rounding)*tick
    return side,{"instrument_name":combo["id"],"amount":str(abs(scale)),"price":str(price),"type":"limit",
                 "time_in_force":"fill_or_kill","post_only":"false","label":label,"valid_until":valid_until}


def send(connector,side,wire,*,authorize,persist_before_send):
    if side not in {"buy","sell"}:raise TradingError("invalid_deribit_side")
    authorize()
    persist_before_send({"venue":"deribit","label":wire["label"],"instrument_name":wire["instrument_name"]})
    try:return rpc(connector,side,"private",wire)
    except Exception as exc:
        raise TradingError("deribit_order_submission_failed",ambiguous=connector._is_ambiguous_exc(exc)) from exc
