"""Deterministic risk checks over observed and projected portfolio effects."""
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .instruments import number
from ..core.errors import TradingError


@dataclass(frozen=True)
class RiskMetrics:
    equity_usd: Decimal
    gross_exposure_usd: Decimal = Decimal(0)
    initial_margin_usd: Decimal = Decimal(0)
    maintenance_margin_usd: Decimal = Decimal(0)
    stress_loss_usd: Decimal = Decimal(0)
    delta_usd: Decimal = Decimal(0)
    gamma_usd: Decimal = Decimal(0)
    vega_usd: Decimal = Decimal(0)
    debt_usd: Decimal = Decimal(0)
    health_factor: Decimal | None = None
    drawdown_pct: Decimal = Decimal(0)
    daily_loss_usd: Decimal = Decimal(0)
    theta_usd: Decimal = Decimal(0)
    protocol_exposure_usd: Decimal = Decimal(0)

    def __post_init__(self):
        for key, value in asdict(self).items():
            if value is None:
                continue
            value = number(value)
            if key not in {"equity_usd", "delta_usd", "gamma_usd", "vega_usd","theta_usd"} and value < 0:
                raise TradingError(f"invalid_risk_metric:{key}")
            object.__setattr__(self, key, value)

    def asdict(self):
        return {key: str(value) if value is not None else None for key, value in asdict(self).items()}


def reduces_risk(before: RiskMetrics, after: RiskMetrics) -> bool:
    """A close label is not evidence: hedges and collateral can protect other legs."""
    names = ("gross_exposure_usd", "initial_margin_usd", "maintenance_margin_usd",
             "stress_loss_usd", "delta_usd", "gamma_usd", "vega_usd", "debt_usd","protocol_exposure_usd")
    pairs = [(abs(getattr(before, key)), abs(getattr(after, key))) for key in names]
    if after.debt_usd and (after.health_factor is None or before.health_factor is None
                          or after.health_factor < before.health_factor):
        return False
    return all(new <= old for old, new in pairs) and any(new < old for old, new in pairs)


def evaluate(before: RiskMetrics, after: RiskMetrics, limits: dict[str, Any], *, mode="normal",
             requires_options_policy=False) -> list[str]:
    reasons = []
    reducing = reduces_risk(before, after)
    if mode not in {"normal", "halt_new_risk", "reduce_only", "freeze_all"}:
        return ["invalid_trading_risk_mode"]
    if mode == "freeze_all" or (mode in {"halt_new_risk", "reduce_only"} and not reducing):
        reasons.append("trading_risk_mode_blocks_action")
    required = {"max_stress_loss_usd", "max_delta_usd", "max_gamma_usd", "max_vega_usd",
                "max_margin_utilization", "min_margin_buffer_usd"}
    if requires_options_policy and any(key not in limits or number(limits[key], positive=True) <= 0 for key in required):
        reasons.append("options_risk_policy_required")
    metrics = {
        "max_gross_exposure_usd": after.gross_exposure_usd,
        "max_debt_usd": after.debt_usd,
        "max_protocol_exposure_usd":after.protocol_exposure_usd,
        "max_stress_loss_usd": after.stress_loss_usd,
        "max_delta_usd": abs(after.delta_usd), "max_gamma_usd": abs(after.gamma_usd),
        "max_vega_usd": abs(after.vega_usd), "max_daily_loss_usd": after.daily_loss_usd,
        "max_theta_usd":abs(after.theta_usd),
        "max_drawdown_pct": after.drawdown_pct,
    }
    for key, metric in metrics.items():
        cap = number(limits.get(key, 0))
        if cap < 0:
            raise TradingError(f"invalid_risk_limit:{key}")
        if cap and metric > cap and not reducing:
            reasons.append(key + "_exceeded")
    utilization = limits.get("max_margin_utilization")
    if utilization is not None:
        cap = number(utilization, positive=True)
        if cap > 1:
            raise TradingError("invalid_margin_utilization")
        if after.equity_usd <= 0 or after.initial_margin_usd > cap * after.equity_usd:
            if not reducing:
                reasons.append("margin_utilization_exceeded")
    buffer = number(limits.get("min_margin_buffer_usd", 0))
    if after.equity_usd - after.maintenance_margin_usd < buffer and not reducing:
        reasons.append("margin_buffer_insufficient")
    if after.debt_usd:
        minimum = limits.get("min_health_factor")
        if minimum is None or after.health_factor is None:
            reasons.append("lending_health_policy_required")
        elif after.health_factor < number(minimum, positive=True) and not reducing:
            reasons.append("lending_health_factor_below_limit")
    return reasons


def drawdown(current: Any, high_water: Any) -> Decimal:
    now, high = number(current), number(high_water)
    return max(Decimal(0), (high - now) / high * 100) if high > 0 else Decimal(0)
