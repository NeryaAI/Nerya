from dataclasses import replace
from decimal import Decimal
import time

import pytest

from nerya.core.errors import TradingError
from nerya.trading.instruments import Instrument,Greeks
from nerya.trading.options_risk import OptionExposure,european_price,portfolio_metrics,margin_usd,admit_margin
from nerya.trading.portfolio_risk import RiskMetrics

pytestmark=pytest.mark.smoke


def exposure(contracts=1,strike=100):
    instrument=Instrument("call", "deribit","BTC/USD:BTC-C","option","BTC","USD","BTC",Decimal("0.1"),
        int(time.time()*1000)+30*86400000,Decimal(str(strike)),"call",price_currency="BTC")
    price=european_price(spot_usd=100,strike_usd=strike,volatility=.2,years=Decimal(30)/365,option_type="call")
    return OptionExposure(instrument,Decimal(str(contracts)),Decimal(100),Decimal(str(strike)),Decimal(".2"),price,
        Greeks(".5",".02","2","-1"),Decimal(1),"deribit:public/ticker",time.time())


def metrics(positions,cash=0):
    return portfolio_metrics(positions,equity_usd=1000,initial_margin_usd=10,maintenance_margin_usd=5,venue_net_delta_usd=20,
        cash_and_hedge_delta_usd={"BTC":cash},stress_shocks=[-.3,0,.3],volatility_shifts=[-.1,0,.1],days_forward=[0,7,31])


def test_option_units_greek_conventions_and_premium_are_explicit():
    row=exposure(2)
    assert row.quantity_base==Decimal(".2")
    effect=row.greek_effect()
    assert effect["delta_bs_usd"]==10
    assert effect["gamma_usd"]==Decimal(".002")
    assert effect["vega_usd"]==Decimal(".4")
    assert effect["theta_usd"]==Decimal("-.2")
    result=metrics([row])
    assert result.gross_exposure_usd==20
    assert result.delta_usd==20  # Venue NTD supplied independently of B-S delta.


def test_expiry_price_and_put_call_parity():
    assert european_price(spot_usd=120,strike_usd=100,volatility=.2,years=0,option_type="call")==20
    assert european_price(spot_usd=80,strike_usd=100,volatility=.2,years=0,option_type="put")==20
    call=european_price(spot_usd=120,strike_usd=100,volatility=.2,years=1,option_type="call")
    put=european_price(spot_usd=120,strike_usd=100,volatility=.2,years=1,option_type="put")
    assert abs(call-put-20)<Decimal("1e-10")


def test_closing_protective_leg_can_increase_stress_loss():
    short=exposure(-10)
    protected=metrics([short,exposure(10,110)])
    naked=metrics([short])
    assert naked.stress_loss_usd>protected.stress_loss_usd


def test_coin_collateral_exposure_is_in_stress_not_assumed_stable_usd():
    assert metrics([],cash=1000).stress_loss_usd==300
    assert metrics([],cash=0).stress_loss_usd==0


def test_missing_account_evidence_and_stale_greeks_fail_closed():
    with pytest.raises(TradingError,match="evidence_required"):replace(exposure(),as_of=0)
    with pytest.raises(TradingError,match="complete_option_account_delta"):
        portfolio_metrics([],equity_usd=1000,initial_margin_usd=0,maintenance_margin_usd=0,venue_net_delta_usd=None,
            cash_and_hedge_delta_usd={},stress_shocks=[.3],volatility_shifts=[0],days_forward=[0])


def reply(**changes):
    return {"currency":"BTC","equity":"2","margin_balance":"2","initial_margin":".2","maintenance_margin":".1",
            "projected_initial_margin":".2","projected_maintenance_margin":".1","cross_collateral_enabled":False,
            "portfolio_margining_enabled":True,"margin_model":"segregated_pm",**changes}


def limits():
    return {"max_stress_loss_usd":50000,"max_delta_usd":100000,"max_gamma_usd":1000,"max_vega_usd":1000,
            "max_margin_utilization":".8","min_margin_buffer_usd":10000}


def test_margin_admission_counts_incremental_margin_premium_and_fee():
    before=RiskMetrics(200000,stress_loss_usd=0)
    after=RiskMetrics(200000,100000,stress_loss_usd=20000,delta_usd=60000)
    quote=admit_margin(reply(),reply(projected_initial_margin=".35",projected_maintenance_margin=".2"),{"BTC":100000},
        before,after,limits(),order_gross_usd=100000,max_debit_usd=5000,fee_usd=100)
    assert Decimal(quote["asset_amounts"]["DERIBIT_MARGIN_USD:BTC"])==20100
    assert Decimal(quote["collateral_usd"])==20000
    assert Decimal(quote["available_asset_amounts"]["DERIBIT_MARGIN_USD:BTC"])==180000


def test_cross_margin_report_uses_usd_totals_and_native_projected_units():
    row=reply(cross_collateral_enabled=True,total_equity_usd=200000,total_margin_balance_usd=200000,
        total_initial_margin_usd=20000,total_maintenance_margin_usd=10000)
    result=margin_usd(row,{"BTC":100000})
    assert result["equity"]==200000 and result["projected_initial_margin"]==20000


def test_naked_option_policy_cannot_default_to_zero_limits():
    with pytest.raises(TradingError,match="options_risk_policy_required"):
        admit_margin(reply(),reply(),{"BTC":100000},RiskMetrics(200000),RiskMetrics(200000,100000),{},
            order_gross_usd=100000,max_debit_usd=0,fee_usd=100)


def test_margin_model_changes_are_not_silent():
    with pytest.raises(TradingError,match="margin_model_changed"):
        admit_margin(reply(),reply(margin_model="segregated_sm"),{"BTC":100000},RiskMetrics(200000),RiskMetrics(200000),limits(),
            order_gross_usd=100000,max_debit_usd=0,fee_usd=100)
