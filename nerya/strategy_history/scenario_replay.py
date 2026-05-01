"""scenario replay for historical sessions.

Session replay (``nerya.workspace.replay``) answers:

    "what actually happened?"

Scenario replay answers:

    "what would have happened if the knobs had been different?"

This module is intentionally a *counterfactual projection* — it does
not re-execute connectors or re-run the LLM. It walks the ledger rows
already captured by the session writer and applies pure-Python
re-evaluation rules to show how many intents would have passed under
alternative thresholds, limits, or execution parameters. The output
is a structured ``ScenarioReport`` so the operator-facing surface can
render diffs without reading raw journals.

Scope guardrails:

* Inputs are pure data: no network, no filesystem mutation.
* Overrides must be explicit (``risk_limits``, ``confidence_threshold``,
  ``slippage_bps_cap`` …); there is no silent defaulting.
* Truth envelope fields (``source``, ``mode``, ``note``) are carried
  forward so the operator knows this is counterfactual analysis, not
  a new real run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.paths import WorkspacePaths
from . import store


@dataclass
class ScenarioOverrides:
    confidence_threshold: float | None = None
    slippage_bps_cap: float | None = None
    latency_ms_cap: float | None = None
    daily_loss_cap_usd: float | None = None
    min_fill_score: float | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "confidence_threshold": self.confidence_threshold,
            "slippage_bps_cap": self.slippage_bps_cap,
            "latency_ms_cap": self.latency_ms_cap,
            "daily_loss_cap_usd": self.daily_loss_cap_usd,
            "min_fill_score": self.min_fill_score,
        }


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
        "Counterfactual projection only; connector/LLM calls were NOT replayed."
    )

    def asdict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "session_id": self.session_id,
            "overrides": self.overrides.asdict(),
            "baseline": dict(self.baseline),
            "projection": dict(self.projection),
            "deltas": dict(self.deltas),
            "dropped": list(self.dropped),
            "note": self.note,
        }


def _scoped(paths: WorkspacePaths, sid: str, session_id: str,
            ledger: str) -> list[dict[str, Any]]:
    return [r for r in store.read_ledger(paths, sid, ledger)
            if r.get("session_id") == session_id]


def _fnum(row: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                pass
        nested = row.get("fill") or row.get("intent") or row.get("pnl") or {}
        if isinstance(nested, dict) and k in nested and nested[k] is not None:
            try:
                return float(nested[k])
            except (TypeError, ValueError):
                pass
    return None


def scenario_replay(paths: WorkspacePaths, strategy_id: str, session_id: str,
                    *, overrides: ScenarioOverrides | dict[str, Any] | None = None,
                    ) -> ScenarioReport:
    """Project alternative outcomes for one historical session.

    The function works in three steps:

    1. Load the session's intents, risk decisions, fills and pnl rows.
    2. Compute the *baseline* counts and PnL from those rows.
    3. Apply each non-``None`` override in turn. Rows that no longer
       satisfy the override are moved to :attr:`ScenarioReport.dropped`
       with a human-readable reason.
    """
    if overrides is None:
        ov = ScenarioOverrides()
    elif isinstance(overrides, dict):
        ov = ScenarioOverrides(**{k: overrides.get(k)
                                  for k in ScenarioOverrides.__dataclass_fields__})
    else:
        ov = overrides

    intents = _scoped(paths, strategy_id, session_id, "intents")
    risks   = _scoped(paths, strategy_id, session_id, "risk")
    fills   = _scoped(paths, strategy_id, session_id, "fills")
    pnls    = _scoped(paths, strategy_id, session_id, "pnl")

    def _pnl(rows):
        return sum(float((r.get("pnl") or {}).get(
            "realized_usd", (r.get("pnl") or {}).get("realized_pnl_usd", 0.0)) or 0.0)
                   for r in rows)

    baseline = {
        "intents": len(intents),
        "risk_rejects": sum(1 for r in risks
                            if (r.get("risk_decision") or {}).get("decision") == "reject"
                            or r.get("decision") == "reject"),
        "fills": len(fills),
        "pnl_usd": round(_pnl(pnls), 4),
    }

    dropped: list[dict[str, Any]] = []

    kept_intents = intents
    if ov.confidence_threshold is not None:
        next_kept = []
        for i in kept_intents:
            conf = (i.get("confidence")
                    or (i.get("intent") or {}).get("confidence"))
            try:
                c = float(conf) if conf is not None else 0.0
            except (TypeError, ValueError):
                c = 0.0
            if c >= ov.confidence_threshold:
                next_kept.append(i)
            else:
                dropped.append({
                    "kind": "intent",
                    "reason": "confidence_below_override",
                    "override": ov.confidence_threshold,
                    "observed": c,
                    "ts": i.get("ts"),
                })
        kept_intents = next_kept

    kept_fills = fills
    if ov.slippage_bps_cap is not None:
        nxt = []
        for f in kept_fills:
            s = _fnum(f, "slippage_bps") or 0.0
            if s <= ov.slippage_bps_cap:
                nxt.append(f)
            else:
                dropped.append({
                    "kind": "fill",
                    "reason": "slippage_above_cap",
                    "override": ov.slippage_bps_cap,
                    "observed": s,
                    "ts": f.get("ts"),
                })
        kept_fills = nxt
    if ov.latency_ms_cap is not None:
        nxt = []
        for f in kept_fills:
            l = _fnum(f, "latency_ms") or 0.0
            if l <= ov.latency_ms_cap:
                nxt.append(f)
            else:
                dropped.append({
                    "kind": "fill",
                    "reason": "latency_above_cap",
                    "override": ov.latency_ms_cap,
                    "observed": l,
                    "ts": f.get("ts"),
                })
        kept_fills = nxt
    if ov.min_fill_score is not None:
        nxt = []
        for f in kept_fills:
            s = _fnum(f, "slippage_bps") or 0.0
            l = _fnum(f, "latency_ms") or 0.0
            score = max(0.0,
                        1.0 - min(s / 100.0, 1.0) * 0.6
                        - min(l / 5000.0, 1.0) * 0.4)
            if score >= ov.min_fill_score:
                nxt.append(f)
            else:
                dropped.append({
                    "kind": "fill",
                    "reason": "fill_score_below_override",
                    "override": ov.min_fill_score,
                    "observed": round(score, 3),
                    "ts": f.get("ts"),
                })
        kept_fills = nxt

    kept_fill_oids = {
        (f.get("order_id") or (f.get("fill") or {}).get("order_id"))
        for f in kept_fills
    }
    projected_pnls = [
        p for p in pnls
        if (p.get("order_id") or (p.get("pnl") or {}).get("order_id")) in kept_fill_oids
    ] if kept_fill_oids else pnls

    projected_pnl = round(_pnl(projected_pnls), 4)

    if ov.daily_loss_cap_usd is not None and projected_pnl < -abs(ov.daily_loss_cap_usd):
        dropped.append({
            "kind": "session",
            "reason": "daily_loss_cap_hit",
            "override": ov.daily_loss_cap_usd,
            "observed": projected_pnl,
        })
        projected_pnl = -abs(ov.daily_loss_cap_usd)

    projection = {
        "intents": len(kept_intents),
        "fills": len(kept_fills),
        "pnl_usd": projected_pnl,
    }
    deltas = {
        "intents": projection["intents"] - baseline["intents"],
        "fills": projection["fills"] - baseline["fills"],
        "pnl_usd": round(projection["pnl_usd"] - baseline["pnl_usd"], 4),
    }
    return ScenarioReport(
        strategy_id=strategy_id,
        session_id=session_id,
        overrides=ov,
        baseline=baseline,
        projection=projection,
        deltas=deltas,
        dropped=dropped,
    )


__all__ = ["ScenarioOverrides", "ScenarioReport", "scenario_replay"]
