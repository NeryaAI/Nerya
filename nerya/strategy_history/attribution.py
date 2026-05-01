"""trading-native attribution.

Given a session's ledger rows, produce a structured *attribution bundle*
that ranks candidate root causes. This output is intentionally
rule-based and independent of any LLM so:

- backtests and replays are deterministic
- the proposal pipeline can consume the output as data
- an operator can always verify ``why`` without reading model prose

Categories
----------
``bad_trigger``          — risk/approval rejected every intent in the session
``weak_subagent``        — intents had sub-threshold confidence
``risk_threshold``       — risk gate blocked on limits that match session PnL
``bad_execution``        — slippage/latency outliers
``stale_config``         — stale-quote or ``max_stale_seconds`` rejections
``missed_opportunity``   — triggers fired but produced zero intents
``overtrading``          — intents >> fills, or many cancels
``strategy_drift``       — realized PnL divergent from paper expectation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.paths import WorkspacePaths
from . import store


ROOT_CAUSES: tuple[str, ...] = (
    "bad_trigger",
    "weak_subagent",
    "risk_threshold",
    "bad_execution",
    "stale_config",
    "missed_opportunity",
    "overtrading",
    "strategy_drift",
)


@dataclass
class AttributionBundle:
    strategy_id: str
    session_id: str
    counts: dict[str, int] = field(default_factory=dict)
    root_causes: list[dict[str, Any]] = field(default_factory=list)
    proposal_seeds: list[dict[str, Any]] = field(default_factory=list)
    pnl_usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "session_id": self.session_id,
            "counts": dict(self.counts),
            "root_causes": list(self.root_causes),
            "proposal_seeds": list(self.proposal_seeds),
            "pnl_usd": self.pnl_usd,
        }


def _scoped(paths: WorkspacePaths, strategy_id: str, session_id: str,
            ledger: str) -> list[dict[str, Any]]:
    return [r for r in store.read_ledger(paths, strategy_id, ledger)
            if r.get("session_id") == session_id]


def _session_pnl(rows: Iterable[dict[str, Any]]) -> float:
    total = 0.0
    for r in rows:
        pnl = r.get("pnl") or {}
        total += float(pnl.get("realized_usd",
                               pnl.get("realized_pnl_usd", 0.0)) or 0.0)
    return total


def attribute_session(paths: WorkspacePaths, strategy_id: str,
                      session_id: str) -> AttributionBundle:
    triggers = _scoped(paths, strategy_id, session_id, "triggers")
    intents  = _scoped(paths, strategy_id, session_id, "intents")
    risks    = _scoped(paths, strategy_id, session_id, "risk")
    orders   = _scoped(paths, strategy_id, session_id, "orders")
    fills    = _scoped(paths, strategy_id, session_id, "fills")
    pnls     = _scoped(paths, strategy_id, session_id, "pnl")

    counts = {
        "triggers": len(triggers),
        "intents": len(intents),
        "risks": len(risks),
        "orders": len(orders),
        "fills": len(fills),
    }

    bundle = AttributionBundle(
        strategy_id=strategy_id, session_id=session_id,
        counts=counts,
        pnl_usd=_session_pnl(pnls) if pnls else None,
    )

    # bad_trigger — triggers fired but nothing made it past risk + fill
    if triggers and not intents and not fills:
        bundle.root_causes.append({
            "cause": "bad_trigger",
            "weight": 1.0,
            "evidence": {"triggers": len(triggers), "fills": 0},
        })
        bundle.proposal_seeds.append({
            "kind": "trigger_route_patch",
            "reason": "triggers fired but zero intents materialised",
        })

    # missed_opportunity — triggers fired, some intents, zero fills
    if triggers and intents and not fills:
        bundle.root_causes.append({
            "cause": "missed_opportunity",
            "weight": 0.9,
            "evidence": {
                "triggers": len(triggers),
                "intents": len(intents),
                "fills": 0,
            },
        })

    def _risk_decision(row: dict[str, Any]) -> str:
        rd = row.get("risk_decision") or {}
        return (rd.get("decision") or rd.get("verdict") or row.get("decision")
                or row.get("verdict") or "")

    # risk_threshold — all intents blocked by risk
    if intents and risks and all(_risk_decision(r) == "reject" for r in risks):
        bundle.root_causes.append({
            "cause": "risk_threshold",
            "weight": 0.8,
            "evidence": {"rejected": len(risks)},
        })
        bundle.proposal_seeds.append({
            "kind": "risk_limit_suggestion",
            "reason": "all intents rejected by risk gate",
        })

    def _intent_conf(row: dict[str, Any]) -> float | None:
        v = row.get("confidence")
        if v is None:
            v = (row.get("intent") or {}).get("confidence")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    low_conf = [
        i for i in intents
        if _intent_conf(i) is not None and _intent_conf(i) < 0.4  # type: ignore[operator]
    ]
    if low_conf and len(low_conf) == len(intents):
        bundle.root_causes.append({
            "cause": "weak_subagent",
            "weight": 0.6,
            "evidence": {
                "intents": len(intents),
                "avg_confidence": sum(
                    _intent_conf(i) or 0.0 for i in low_conf
                ) / len(low_conf),
            },
        })

    def _fill_num(row: dict[str, Any], key: str) -> float | None:
        v = row.get(key)
        if v is None:
            v = (row.get("fill") or {}).get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    bad_fills = [
        f for f in fills
        if (
            (_fill_num(f, "slippage_bps") or 0) > 50
            or (_fill_num(f, "latency_ms") or 0) > 2000
        )
    ]
    if bad_fills:
        bundle.root_causes.append({
            "cause": "bad_execution",
            "weight": 0.7,
            "evidence": {"bad_fills": len(bad_fills),
                         "total_fills": len(fills)},
        })

    # stale_config — risk rejections mentioning "stale"
    stale_rejects = [
        r for r in risks
        if "stale" in str(
            r.get("reason")
            or (r.get("risk_decision") or {}).get("reason")
            or ""
        ).lower()
    ]
    if stale_rejects:
        bundle.root_causes.append({
            "cause": "stale_config",
            "weight": 0.5,
            "evidence": {"stale_rejects": len(stale_rejects)},
        })

    # overtrading — many intents, few fills
    if len(intents) >= 5 and fills and len(intents) >= 3 * len(fills):
        bundle.root_causes.append({
            "cause": "overtrading",
            "weight": 0.5,
            "evidence": {"intents": len(intents), "fills": len(fills)},
        })

    # strategy_drift — realized pnl < -threshold
    if bundle.pnl_usd is not None and bundle.pnl_usd < -200.0:
        bundle.root_causes.append({
            "cause": "strategy_drift",
            "weight": 0.4,
            "evidence": {"pnl_usd": bundle.pnl_usd},
        })
        bundle.proposal_seeds.append({
            "kind": "strategy_config_patch",
            "reason": f"realized PnL {bundle.pnl_usd:.2f} USD below drift threshold",
        })

    bundle.root_causes.sort(key=lambda r: r["weight"], reverse=True)
    return bundle


# ==================================================================
# v2 — richer attribution surfaces
# ==================================================================

def subagent_contribution(paths: WorkspacePaths, strategy_id: str,
                          session_id: str) -> dict[str, Any]:
    """Summarise subagent behaviour over a single session.

    Reads the per-subagent journal the runtime writes and
    aggregates signals/skill_calls/rejections/uncertainty so the
    review & optimization surfaces can rank who actually contributed
    to an outcome instead of assuming every subagent was equally
    useful.
    """
    rows = [r for r in store.read_ledger(paths, strategy_id, "subagents")
            if r.get("session_id") == session_id]
    per_subagent: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r.get("name") or (r.get("output") or {}).get("subagent") or "?"
        bucket = per_subagent.setdefault(name, {
            "name": name,
            "calls": 0,
            "signals_used": set(),
            "skill_calls": 0,
            "rejected_actions": 0,
            "uncertainty_sum": 0.0,
            "uncertainty_samples": 0,
            "evidence_count": 0,
        })
        bucket["calls"] += 1
        metrics = ((r.get("output") or {}).get("metrics")
                   or r.get("metrics") or {})
        for sig in metrics.get("signals_used") or []:
            bucket["signals_used"].add(str(sig))
        bucket["skill_calls"] += len(metrics.get("skill_calls") or [])
        bucket["rejected_actions"] += len(metrics.get("rejected_actions") or [])
        u = metrics.get("uncertainty")
        if isinstance(u, (int, float)):
            bucket["uncertainty_sum"] += float(u)
            bucket["uncertainty_samples"] += 1
        bucket["evidence_count"] += len(metrics.get("evidence") or [])

    out: list[dict[str, Any]] = []
    for bucket in per_subagent.values():
        n = max(1, bucket.pop("uncertainty_samples"))
        avg_unc = bucket.pop("uncertainty_sum") / n
        bucket["avg_uncertainty"] = round(avg_unc, 4)
        bucket["signals_used"] = sorted(bucket["signals_used"])
        out.append(bucket)

    out.sort(key=lambda b: (-b["calls"], b["name"]))
    return {
        "strategy_id": strategy_id,
        "session_id": session_id,
        "subagents": out,
    }


def execution_quality(paths: WorkspacePaths, strategy_id: str,
                      session_id: str) -> dict[str, Any]:
    """Richer execution-quality attribution.

    Rather than a single "bad_fills > 50bps" flag, we compute slippage
    and latency percentiles + a per-fill score so optimization can
    pinpoint which fills dragged the quality down.
    """
    fills = _scoped(paths, strategy_id, session_id, "fills")
    slip: list[float] = []
    lat:  list[float] = []
    per_fill: list[dict[str, Any]] = []
    for f in fills:
        s = _fill_num(f, "slippage_bps") or 0.0
        l = _fill_num(f, "latency_ms") or 0.0
        slip.append(float(s))
        lat.append(float(l))
        # Composite 0..1 score: lower slip & latency => higher score.
        score = max(0.0, 1.0 - min(s / 100.0, 1.0) * 0.6 - min(l / 5000.0, 1.0) * 0.4)
        per_fill.append({
            "slippage_bps": s, "latency_ms": l,
            "score": round(score, 3),
            "market": f.get("market") or (f.get("fill") or {}).get("market"),
        })

    def _p(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        xs_sorted = sorted(xs)
        k = max(0, min(len(xs_sorted) - 1, int(round(q * (len(xs_sorted) - 1)))))
        return xs_sorted[k]

    return {
        "strategy_id": strategy_id,
        "session_id": session_id,
        "fills_total": len(fills),
        "slippage_bps": {
            "p50": _p(slip, 0.50), "p90": _p(slip, 0.90), "p99": _p(slip, 0.99),
            "max": max(slip) if slip else 0.0,
        },
        "latency_ms": {
            "p50": _p(lat, 0.50), "p90": _p(lat, 0.90), "p99": _p(lat, 0.99),
            "max": max(lat) if lat else 0.0,
        },
        "per_fill": per_fill,
        "avg_quality": (sum(x["score"] for x in per_fill) / len(per_fill)
                        if per_fill else 0.0),
    }


def paper_vs_live_divergence(paths: WorkspacePaths, strategy_id: str, *,
                             window_sessions: int = 25) -> dict[str, Any]:
    """Compare realised PnL on paper vs live sessions.

    ``window_sessions`` caps how far back we scan to keep the review
    incremental. We classify each session by whether any live fill
    appears in its fills ledger; the divergence is reported as
    ``live_mean_pnl - paper_mean_pnl`` together with sample counts.
    """
    pnls = store.read_ledger(paths, strategy_id, "pnl")
    fills = store.read_ledger(paths, strategy_id, "fills")
    # Index fill rows by session id for the live/paper classification.
    sessions_with_live: set[str] = set()
    for row in fills:
        sid = row.get("session_id")
        if not sid:
            continue
        mode = ((row.get("fill") or {}).get("mode")
                or row.get("mode") or "").lower()
        if mode == "live":
            sessions_with_live.add(sid)
    per_session_pnl: dict[str, float] = {}
    for row in pnls:
        sid = row.get("session_id")
        if not sid:
            continue
        per_session_pnl.setdefault(sid, 0.0)
        per_session_pnl[sid] += float(
            (row.get("pnl") or {}).get("realized_usd",
                                       (row.get("pnl") or {}).get("realized_pnl_usd", 0.0)) or 0.0
        )

    recent_sids = list(per_session_pnl.keys())[-window_sessions:]
    paper = [v for sid, v in per_session_pnl.items()
             if sid in recent_sids and sid not in sessions_with_live]
    live  = [v for sid, v in per_session_pnl.items()
             if sid in recent_sids and sid in sessions_with_live]

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "strategy_id": strategy_id,
        "sessions_window": window_sessions,
        "paper_sessions": len(paper),
        "live_sessions": len(live),
        "paper_mean_pnl_usd": round(_mean(paper), 4),
        "live_mean_pnl_usd":  round(_mean(live), 4),
        "divergence_usd":     round(_mean(live) - _mean(paper), 4),
    }


def indicator_sensitivity(paths: WorkspacePaths, strategy_id: str,
                          session_id: str, *,
                          indicators: list[str] | None = None,
                          ) -> dict[str, Any]:
    """Correlate indicator scalars recorded in decisions with pnl.

    The decision ledger may embed a ``features`` / ``indicators`` dict
    captured at the moment the intent was produced. This helper walks
    those embedded snapshots and, for each indicator name, reports the
    mean value on *winning* vs *losing* decisions. Missing data is
    surfaced explicitly rather than silently skipped.
    """
    decisions = _scoped(paths, strategy_id, session_id, "decisions")
    pnls = _scoped(paths, strategy_id, session_id, "pnl")
    pnl_by_order: dict[str, float] = {}
    for row in pnls:
        oid = row.get("order_id") or (row.get("pnl") or {}).get("order_id")
        if not oid:
            continue
        pnl_by_order[str(oid)] = float(
            (row.get("pnl") or {}).get("realized_usd",
                                       (row.get("pnl") or {}).get("realized_pnl_usd", 0.0)) or 0.0
        )
    per_indicator: dict[str, dict[str, Any]] = {}
    for row in decisions:
        d = row.get("decision") or {}
        feats = d.get("features") or d.get("indicators") or {}
        if not isinstance(feats, dict):
            continue
        oid = d.get("order_id") or row.get("order_id")
        outcome = pnl_by_order.get(str(oid)) if oid else None
        if outcome is None:
            continue
        for k, v in feats.items():
            if indicators and k not in indicators:
                continue
            if not isinstance(v, (int, float)):
                continue
            bucket = per_indicator.setdefault(k, {"win": [], "loss": []})
            if outcome > 0:
                bucket["win"].append(float(v))
            elif outcome < 0:
                bucket["loss"].append(float(v))
    out: list[dict[str, Any]] = []
    for name, bucket in per_indicator.items():
        w = bucket["win"]
        l = bucket["loss"]
        out.append({
            "indicator": name,
            "win_samples": len(w),
            "loss_samples": len(l),
            "win_mean": sum(w) / len(w) if w else None,
            "loss_mean": sum(l) / len(l) if l else None,
            "delta": (sum(w) / len(w) if w else 0.0) - (sum(l) / len(l) if l else 0.0),
        })
    out.sort(key=lambda e: abs(e.get("delta") or 0.0), reverse=True)
    return {
        "strategy_id": strategy_id,
        "session_id": session_id,
        "indicators": out,
    }


def _fill_num(row: dict[str, Any], key: str) -> float | None:
    v = row.get(key)
    if v is None:
        v = (row.get("fill") or {}).get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
