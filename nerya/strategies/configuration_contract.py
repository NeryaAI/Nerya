"""Manifest consistency checks shared by save validation and replay preflight."""
from __future__ import annotations
import math


def configuration_issues(manifest):
    issues = []
    extras = manifest.extras
    params = extras.get("params") or {}
    if not isinstance(params, dict):
        return [("invalid_strategy_params", "params must be a mapping")]
    if "price_simulation" in params:
        simulation = params["price_simulation"]
        if not isinstance(simulation, dict) or set(simulation) != {"dataset_id"}:
            issues.append(("invalid_price_simulation",
                "params.price_simulation requires only dataset_id"))
    indicator = params.get("indicator")
    if isinstance(indicator, dict) and {"fast_period", "slow_period"} <= indicator.keys():
        periods = [indicator[name] for name in ("fast_period", "slow_period")]
        if any(type(period) is not int or period < 1 for period in periods):
            issues.append(("invalid_ema_periods",
                "params.indicator.fast_period and slow_period must be positive integers"))
        else:
            lookback = params.get("lookback")
            required = max(periods) + 1
            if type(lookback) is not int or lookback < required:
                issues.append(("insufficient_closed_history_lookback",
                    f"EMA crossover needs lookback >= {required}: one prior and one current closed point "
                    f"are required after the slow-period SMA seed; forming candles do not count"))
    sizing = params.get("sizing", {})
    if "sizing" in params:
        from ..core.errors import IntentValidationError
        from ..trading.order_intents import SizingPolicy
        try:
            if not isinstance(sizing, dict):
                raise IntentValidationError("params.sizing must be a mapping")
            SizingPolicy(**sizing)
        except (IntentValidationError, TypeError) as exc:
            issues.append(("invalid_strategy_sizing", str(exc)))
            sizing = {}
    if isinstance(sizing, dict) and sizing.get("method") == "fixed_usd":
        amount = sizing.get("fixed_usd")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            issues.append(("invalid_strategy_sizing", "params.sizing.fixed_usd must be finite and positive"))
        elif manifest.policy.max_single_order_usd > 0 and amount > manifest.policy.max_single_order_usd:
            issues.append(("strategy_sizing_policy_conflict",
                f"params.sizing.fixed_usd={amount} exceeds policy.max_single_order_usd={manifest.policy.max_single_order_usd}. "
                "Preserve the operator's requested size and risk limits; correct conflicting template defaults before replay, never silently downsize."))
    if "schedule_enabled" in extras:
        issues.append(("invalid_schedule_field", "schedule_enabled is not a runtime switch; use schedule.enabled: false to disable the schedule."))
    return issues
