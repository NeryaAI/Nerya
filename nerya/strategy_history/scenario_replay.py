"""Descriptive historical filtering, not a counterfactual execution simulator.

Changing risk limits or removing fills changes future exposure and available
liquidity. Ledger filtering cannot estimate that counterfactual profit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.paths import WorkspacePaths
from .attribution import _number, attribute_session


@dataclass
class ScenarioOverrides:
    confidence_threshold: float | None = None
    slippage_bps_cap: float | None = None
    latency_ms_cap: float | None = None
    daily_loss_cap_usd: float | None = None

    def __post_init__(self) -> None:
        for name, raw in asdict(self).items():
            if raw is None:
                continue
            value = _number(raw)
            if value is None:
                raise ValueError(f"{name} must be finite")
            if name == "confidence_threshold" and not 0 <= value <= 1:
                raise ValueError("confidence_threshold must be between zero and one")
            if name in {"latency_ms_cap", "daily_loss_cap_usd"} and value < 0:
                raise ValueError(f"{name} must be non-negative")
            setattr(self, name, value)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioReport:
    strategy_id: str
    session_id: str
    overrides: ScenarioOverrides
    baseline: dict[str, Any] = field(default_factory=dict)
    projection: dict[str, Any] = field(default_factory=dict)
    deltas: dict[str, Any] = field(default_factory=dict)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    note: str = (
        "Historical row filtering only. Missing measurements cannot pass a filter. "
        "No counterfactual trading, latency, risk-limit execution or market simulation was run; "
        "filtered counts do not establish achievable profit or an improved strategy."
    )

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def scenario_replay(paths: WorkspacePaths, strategy_id: str, session_id: str, *,
                    overrides: ScenarioOverrides | dict[str, Any] | None = None) -> ScenarioReport:
    if overrides is None:
        policy = ScenarioOverrides()
    elif isinstance(overrides, dict):
        policy = ScenarioOverrides(**overrides)  # Unknown/retired knobs are errors, not ignored.
    elif isinstance(overrides, ScenarioOverrides):
        policy = overrides
    else:
        raise TypeError("overrides must be a mapping or ScenarioOverrides")
    bundle = attribute_session(paths, strategy_id, session_id)
    intents, fills, risks = (bundle.evidence[name] for name in ("intents", "fills", "risk"))
    baseline = {
        "intents": len(intents), "fills": len(fills), "pnl_usd": bundle.pnl_usd,
        "risk_rejects": sum((row.get("risk_decision") or {}).get("decision") == "reject" for row in risks),
    }
    dropped = []

    def select(rows: list[dict[str, Any]], kind: str, key: str, limit: float | None,
               *, minimum: bool = False) -> list[dict[str, Any]]:
        if limit is None:
            return rows
        kept = []
        for row in rows:
            nested = row.get(kind) if isinstance(row.get(kind), dict) else {}
            value = _number(row.get(key, nested.get(key)))
            if value is not None and (value >= limit if minimum else value <= limit):
                kept.append(row)
            else:
                dropped.append({
                    "kind": kind, "reason": f"missing_{key}" if value is None else f"{key}_outside_override",
                    "override": limit, "observed": value, "ts": row.get("ts"),
                })
        return kept

    kept_intents = select(intents, "intent", "confidence", policy.confidence_threshold, minimum=True)
    kept_fills = select(fills, "fill", "slippage_bps", policy.slippage_bps_cap)
    kept_fills = select(kept_fills, "fill", "latency_ms", policy.latency_ms_cap)
    changed = any(value is not None for value in policy.asdict().values())
    projection = {
        "status": "counterfactual_not_simulated" if changed else "historical_baseline",
        "intents": len(kept_intents), "fills": len(kept_fills),
        "pnl_usd": None if changed else bundle.pnl_usd,
        "risk_replay_required": policy.daily_loss_cap_usd is not None,
    }
    return ScenarioReport(
        strategy_id, session_id, policy, baseline=baseline, projection=projection,
        deltas={"intents": len(kept_intents) - len(intents), "fills": len(kept_fills) - len(fills),
                "pnl_usd": None if changed or bundle.pnl_usd is None else 0.0},
        dropped=dropped,
    )
