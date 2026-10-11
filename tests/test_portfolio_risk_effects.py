from dataclasses import replace
from decimal import Decimal

import pytest

from nerya.trading.portfolio_risk import RiskMetrics, drawdown, evaluate, reduces_risk

pytestmark = pytest.mark.smoke


def test_closing_a_hedge_is_not_a_risk_reducing_exit():
    before = RiskMetrics(1000, gross_exposure_usd=1000, stress_loss_usd=100, delta_usd=0)
    after = replace(before, gross_exposure_usd=500, stress_loss_usd=400, delta_usd=500)
    assert not reduces_risk(before, after)
    assert evaluate(before, after, {}, mode="reduce_only") == ["trading_risk_mode_blocks_action"]


def test_removing_collateral_cannot_use_reduction_freshness_exemption():
    before = RiskMetrics(1000, gross_exposure_usd=1000, debt_usd=500, health_factor=Decimal("2"))
    after = replace(before, gross_exposure_usd=800, health_factor=Decimal("1.5"))
    assert not reduces_risk(before, after)
    assert "lending_health_factor_below_limit" in evaluate(before, after, {"min_health_factor": "1.8"})


def test_repayment_is_allowed_to_improve_an_already_unsafe_health_factor():
    before = RiskMetrics(1000, debt_usd=600, health_factor=Decimal("1.1"))
    after = replace(before, debt_usd=550, health_factor=Decimal("1.2"))
    assert reduces_risk(before, after)
    assert evaluate(before, after, {"min_health_factor": "1.8"}, mode="halt_new_risk") == []


def test_naked_option_sale_needs_all_risk_limits_and_actual_margin():
    before = RiskMetrics(1000)
    after = RiskMetrics(1000, initial_margin_usd=600, maintenance_margin_usd=400, stress_loss_usd=200)
    assert "options_risk_policy_required" in evaluate(before, after, {}, requires_options_policy=True)
    limits = {"max_stress_loss_usd": 100, "max_delta_usd": 100, "max_gamma_usd": 100,
              "max_vega_usd": 100, "max_margin_utilization": "0.5", "min_margin_buffer_usd": 700}
    assert set(evaluate(before, after, limits, requires_options_policy=True)) == {
        "max_stress_loss_usd_exceeded", "margin_utilization_exceeded", "margin_buffer_insufficient"}


def test_freeze_all_never_reenables_exits():
    before = RiskMetrics(1000, gross_exposure_usd=1000)
    after = RiskMetrics(1000)
    assert evaluate(before, after, {}, mode="freeze_all") == ["trading_risk_mode_blocks_action"]


def test_actual_drawdown_and_loss_caps_block_adding_risk():
    before = RiskMetrics(1000, gross_exposure_usd=500)
    after = RiskMetrics(800, gross_exposure_usd=600, drawdown_pct=drawdown(800, 1000), daily_loss_usd=200)
    assert evaluate(before, after, {"max_drawdown_pct": 10, "max_daily_loss_usd": 100}) == [
        "max_daily_loss_usd_exceeded", "max_drawdown_pct_exceeded"]
