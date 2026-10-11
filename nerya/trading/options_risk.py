"""Explicit option units, local stress scenarios and venue margin admission.

European repricing is an independent stress model, not a venue margin model
or a guarantee of attainable prices. Cash/hedge deltas and venue NTD must be
supplied from the complete account; omitted observations are never zeroed.
"""
from dataclasses import dataclass,replace
from decimal import Decimal
from math import log,sqrt
from statistics import NormalDist
import time

from ..core.errors import TradingError
from .instruments import Instrument,Greeks,number
from .portfolio_risk import RiskMetrics,evaluate,reduces_risk


def european_price(*,spot_usd,strike_usd,volatility,years,option_type):
    spot,strike=number(spot_usd,positive=True),number(strike_usd,positive=True)
    sigma=number(volatility,positive=True)
    years=number(years)
    if option_type not in {"call","put"}:raise TradingError("invalid_option_type")
    if years<=0:return max(Decimal(0),spot-strike if option_type=="call" else strike-spot)
    s,k,v,t=map(float,(spot,strike,sigma,years))
    root=sqrt(t);d1=(log(s/k)+v*v*t/2)/(v*root);d2=d1-v*root
    normal=NormalDist()
    value=s*normal.cdf(d1)-k*normal.cdf(d2) if option_type=="call" else k*normal.cdf(-d2)-s*normal.cdf(-d1)
    return Decimal(str(max(0.0,value)))


@dataclass(frozen=True)
class OptionExposure:
    instrument: Instrument
    contracts: Decimal
    spot_usd: Decimal
    strike_usd: Decimal
    volatility: Decimal
    mark_usd: Decimal
    native_greeks: Greeks
    greek_counter_currency_fx_usd: Decimal
    source: str
    as_of: float

    def __post_init__(self):
        if self.instrument.kind!="option" or not self.source or not 0<=time.time()-self.as_of<=30:
            raise TradingError("option_risk_evidence_required")
        for name in ("contracts","spot_usd","strike_usd","volatility","mark_usd","greek_counter_currency_fx_usd"):
            object.__setattr__(self,name,number(getattr(self,name),positive=name not in {"contracts","mark_usd"}))
        if self.mark_usd<0:raise TradingError("invalid_option_mark")

    @property
    def quantity_base(self):return self.contracts*self.instrument.contract_size

    def value(self,*,shock=0,volatility_shift=0,days_forward=0,now=None):
        instant=time.time() if now is None else now
        years=max(Decimal(0),(Decimal(self.instrument.expiry_ms)/1000-Decimal(str(instant))-Decimal(str(days_forward))*86400)/Decimal(365*86400))
        return european_price(spot_usd=self.spot_usd*(1+number(shock)),strike_usd=self.strike_usd,
                              volatility=max(Decimal("0.01"),self.volatility+number(volatility_shift)),years=years,option_type=self.instrument.option_type)*self.quantity_base

    def greek_effect(self):
        quantity=self.quantity_base;fx=self.greek_counter_currency_fx_usd
        return {"delta_bs_usd":quantity*self.native_greeks.delta*self.spot_usd,
                "gamma_usd":quantity*self.native_greeks.gamma*(self.spot_usd*Decimal("0.01"))**2*fx/2,
                "vega_usd":quantity*self.native_greeks.vega*fx,"theta_usd":quantity*self.native_greeks.theta*fx}


def portfolio_metrics(positions,*,equity_usd,initial_margin_usd,maintenance_margin_usd,
                      venue_net_delta_usd,cash_and_hedge_delta_usd,stress_shocks,volatility_shifts,days_forward):
    if not isinstance(cash_and_hedge_delta_usd,dict) or venue_net_delta_usd is None:
        raise TradingError("complete_option_account_delta_required")
    if not stress_shocks or not volatility_shifts or not days_forward:
        raise TradingError("options_stress_scenarios_required")
    shocks=[number(item) for item in stress_shocks]
    if any(item<=-1 for item in shocks):raise TradingError("invalid_underlying_stress_shock")
    shifts=[number(item) for item in volatility_shifts]
    days=[number(item) for item in days_forward]
    if any(item<0 for item in days):raise TradingError("invalid_time_stress")
    underlyings={item.instrument.base for item in positions}|set(cash_and_hedge_delta_usd)
    if any(key not in cash_and_hedge_delta_usd for key in underlyings):raise TradingError("complete_option_cash_delta_required")
    cash={key:number(value) for key,value in cash_and_hedge_delta_usd.items()}
    now=time.time();baseline=sum((item.value(now=now) for item in positions),Decimal(0))
    model_error=sum((abs(item.value(now=now)-item.mark_usd*item.quantity_base) for item in positions),Decimal(0))
    losses=[Decimal(0)]
    for shock in shocks:
        # Correlated shock and one underlying at a time. A caller may supply
        # additional joint scenarios via its own reviewed component model.
        for selected in [None,*sorted(underlyings)]:
            for shift in shifts:
                for forward in days:
                    pnl=sum((item.value(shock=shock if selected is None or item.instrument.base==selected else 0,
                                        volatility_shift=shift,days_forward=forward,now=now) for item in positions),Decimal(0))-baseline
                    pnl+=sum((delta*shock for base,delta in cash.items() if selected is None or selected==base),Decimal(0))
                    losses.append(max(Decimal(0),-pnl))
    effects=[item.greek_effect() for item in positions]
    return RiskMetrics(number(equity_usd),sum((abs(item.quantity_base)*item.spot_usd for item in positions),Decimal(0)),
        number(initial_margin_usd),number(maintenance_margin_usd),max(losses)+model_error,
        number(venue_net_delta_usd),sum((item["gamma_usd"] for item in effects),Decimal(0)),
        sum((item["vega_usd"] for item in effects),Decimal(0)),theta_usd=sum((item["theta_usd"] for item in effects),Decimal(0)))


def margin_usd(reply,fx_usd):
    if type(reply.get("cross_collateral_enabled")) is not bool or type(reply.get("portfolio_margining_enabled")) is not bool:
        raise TradingError("deribit_margin_model_missing")
    fx=number(fx_usd[reply["currency"]],positive=True)
    cross=reply["cross_collateral_enabled"]
    prefix,suffix=("total_","_usd") if cross else ("","")
    factor=Decimal(1) if cross else fx
    result={key:number(reply[prefix+key+suffix])*factor for key in ("equity","margin_balance","initial_margin","maintenance_margin")}
    for key in ("projected_initial_margin","projected_maintenance_margin"):result[key]=number(reply[key])*fx
    if any(value<0 for key,value in result.items() if "margin" in key and key!="margin_balance"):
        raise TradingError("negative_deribit_margin")
    return result


def admit_margin(before_reply,after_reply,fx_usd,before_risk,after_risk,limits,*,order_gross_usd,max_debit_usd,fee_usd,mode="normal"):
    if any(before_reply.get(key)!=after_reply.get(key) for key in ("currency","margin_model","portfolio_margining_enabled","cross_collateral_enabled")):
        raise TradingError("deribit_margin_model_changed")
    before_margin,after_margin=margin_usd(before_reply,fx_usd),margin_usd(after_reply,fx_usd)
    debit=max(Decimal(0),number(max_debit_usd));fee=number(fee_usd)
    if fee<0:raise TradingError("negative_fee")
    before=replace(before_risk,equity_usd=before_margin["equity"],
        initial_margin_usd=max(before_margin["initial_margin"],before_margin["projected_initial_margin"]),
        maintenance_margin_usd=max(before_margin["maintenance_margin"],before_margin["projected_maintenance_margin"]))
    after=replace(after_risk,equity_usd=min(before_margin["equity"],after_margin["equity"])-debit-fee,
        initial_margin_usd=max(after_margin["initial_margin"],after_margin["projected_initial_margin"]),
        maintenance_margin_usd=max(after_margin["maintenance_margin"],after_margin["projected_maintenance_margin"]))
    reasons=evaluate(before,after,limits,mode=mode,requires_options_policy=True)
    if "options_risk_policy_required" in reasons:raise TradingError("options_risk_policy_required")
    collateral=min(before_margin["margin_balance"],after_margin["margin_balance"])-debit-fee
    if not reduces_risk(before,after):
        if collateral<=0 or after.initial_margin_usd>number(limits.get("max_margin_utilization"),positive=True)*collateral:
            reasons.append("deribit_collateral_utilization_exceeded")
        if collateral-after.maintenance_margin_usd<number(limits.get("min_margin_buffer_usd"),positive=True):
            reasons.append("deribit_collateral_buffer_insufficient")
    if reasons:raise TradingError("deribit_risk_rejected:"+",".join(reasons))
    reserve=max(Decimal(0),after.initial_margin_usd-before.initial_margin_usd)+debit+fee
    key="DERIBIT_MARGIN_USD:"+("CROSS" if before_reply["cross_collateral_enabled"] else before_reply["currency"])
    return {"asset_amounts":{key:str(reserve)},"available_asset_amounts":{key:str(max(Decimal(0),before_margin["margin_balance"]-before.initial_margin_usd))},
        "risk_usd":str(max(number(order_gross_usd,positive=True),after.stress_loss_usd-before.stress_loss_usd)),
        "spend_usd":str(debit),"fee_usd":str(fee),"collateral_usd":str(reserve-fee),
        "portfolio_effect":{"before":before.asdict(),"after":after.asdict(),"source":"deribit:simulate_portfolio","as_of":time.time()}}
