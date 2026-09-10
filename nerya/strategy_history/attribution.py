"""Scoped ledger observations; causality belongs to an evidence-backed review.

Amounts, counts and latency are measurements, not interchangeable quality scores.
The same in-memory ledger snapshot can feed a review and its diagnostics.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..core.paths import WorkspacePaths
from . import store


REVIEW_LEDGERS = ("triggers", "intents", "risk", "decisions", "orders", "fills", "pnl", "messages", "subagents")


def read_strategy_evidence(paths: WorkspacePaths, strategy_id: str) -> dict[str, list[dict[str, Any]]]:
    root = paths.strategies.resolve()
    if not strategy_id or (root / strategy_id).resolve().parent != root:
        raise ValueError("strategy_id must identify one strategy in this workspace")
    return {name: store.read_ledger(paths, strategy_id, name) for name in REVIEW_LEDGERS}


@dataclass
class AttributionBundle:
    strategy_id: str
    session_id: str
    counts: dict[str, int] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    evidence_sha256: str = ""
    pnl_usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scoped(paths: WorkspacePaths, strategy_id: str, session_id: str,
            ledger: str, ledgers: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    if not session_id:
        raise ValueError("session_id is required for attributable evidence")
    rows = ledgers.get(ledger, []) if ledgers is not None else store.read_ledger(paths, strategy_id, ledger)
    return [row for row in rows if row.get("session_id") == session_id]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _pnl_value(row: dict[str, Any]) -> float | None:
    pnl = row.get("pnl") or {}
    return _number(pnl.get("realized_usd", pnl.get("realized_pnl_usd")))


def _session_pnl(rows: Iterable[dict[str, Any]]) -> float | None:
    values = [_pnl_value(row) for row in rows]
    # An incomplete ledger must not masquerade as a complete realized total.
    return sum(values) if values and all(value is not None for value in values) else None


def attribute_session(paths: WorkspacePaths, strategy_id: str, session_id: str, *,
                      ledgers: dict[str, list[dict[str, Any]]] | None = None) -> AttributionBundle:
    snapshot = read_strategy_evidence(paths, strategy_id) if ledgers is None else ledgers
    evidence = {name: _scoped(paths, strategy_id, session_id, name, snapshot) for name in REVIEW_LEDGERS}
    root = paths.root.resolve()
    refs = {name: f"file:{(paths.strategy_history(strategy_id) / (name + '.jsonl')).resolve().relative_to(root).as_posix()}"
            for name, rows in evidence.items() if rows}
    counts = {name: len(rows) for name, rows in evidence.items()}
    observations = [{"kind": "ledger_observed", "ledger": name, "rows": counts[name], "evidence_refs": [ref]}
                    for name, ref in refs.items()]
    if evidence["triggers"] and not evidence["intents"]:
        observations.append({
            "kind": "no_intent_recorded", "evidence_refs": [refs["triggers"]],
            "interpretation": "Abstention, missing evidence and execution failure are alternatives to investigate, not proven defects.",
        })
    rejected = [row for row in evidence["risk"] if (row.get("risk_decision") or {}).get("decision") == "reject"]
    if rejected:
        observations.append({
            "kind": "risk_rejected", "count": len(rejected), "evidence_refs": [refs["risk"]],
            "interpretation": "A configured risk control acted; rejection alone does not justify relaxing that control.",
        })
    return AttributionBundle(
        strategy_id=strategy_id, session_id=session_id, counts=counts,
        observations=observations, evidence=evidence, evidence_refs=list(refs.values()),
        evidence_sha256=hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest(),
        pnl_usd=_session_pnl(evidence["pnl"]),
    )



def subagent_contribution(paths: WorkspacePaths, strategy_id: str, session_id: str, *,
                          ledgers: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Summarise subagent behaviour over a single session.

    Reports observed calls, signals, rejections and reported uncertainty.
    These describe activity, not causal contribution or investment quality.
    """
    rows = _scoped(paths, strategy_id, session_id, "subagents", ledgers)
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
        u = _number(metrics.get("uncertainty"))
        if u is not None and 0 <= u <= 1:
            bucket["uncertainty_sum"] += float(u)
            bucket["uncertainty_samples"] += 1
        bucket["evidence_count"] += len(metrics.get("evidence") or [])

    out: list[dict[str, Any]] = []
    for bucket in per_subagent.values():
        n = bucket["uncertainty_samples"]
        total_uncertainty = bucket.pop("uncertainty_sum")
        bucket["avg_uncertainty"] = round(total_uncertainty / n, 4) if n else None
        bucket["signals_used"] = sorted(bucket["signals_used"])
        out.append(bucket)

    out.sort(key=lambda b: b["name"])  # Activity volume is not evidence of contribution quality.
    return {
        "strategy_id": strategy_id,
        "session_id": session_id,
        "subagents": out,
    }


def execution_quality(paths: WorkspacePaths, strategy_id: str, session_id: str, *,
                      ledgers: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Report measured distributions, never substitute zero for missing data."""
    fills = _scoped(paths, strategy_id, session_id, "fills", ledgers)
    per_fill = []
    for row in fills:
        latency = _fill_num(row, "latency_ms")
        per_fill.append({
            "slippage_bps": _fill_num(row, "slippage_bps"),
            "latency_ms": latency if latency is not None and latency >= 0 else None,
            "market": row.get("market") or (row.get("fill") or {}).get("market"),
            "order_id": row.get("order_id") or (row.get("fill") or {}).get("order_id"),
        })

    def distribution(key: str) -> dict[str, Any]:
        values = sorted(row[key] for row in per_fill if row[key] is not None)
        result = {"samples": len(values), "missing": len(fills) - len(values),
                  "max": max(values) if values else None}
        for name, quantile in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99)):
            result[name] = values[round(quantile * (len(values) - 1))] if values else None
        return result

    return {"strategy_id": strategy_id, "session_id": session_id,
            "fills_total": len(fills), "slippage_bps": distribution("slippage_bps"),
            "latency_ms": distribution("latency_ms"), "per_fill": per_fill}


def paper_vs_live_divergence(paths: WorkspacePaths, strategy_id: str, *,
                             window_sessions: int = 25,
                             ledgers: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Descriptive comparison of explicitly classified sessions, not causal lift."""
    if window_sessions < 0:
        raise ValueError("window_sessions must be non-negative")
    snapshot = ledgers if ledgers is not None else read_strategy_evidence(paths, strategy_id)
    modes: dict[str, set[str]] = {}
    for name in ("fills", "pnl"):
        for row in snapshot.get(name, []):
            sid = row.get("session_id")
            if not sid:
                continue
            nested = row.get("fill" if name == "fills" else "pnl") or {}
            for raw in (row.get("mode"), nested.get("mode")):
                if raw is not None:
                    modes.setdefault(sid, set()).add(str(raw).strip().lower())
    per_session: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot.get("pnl", []):
        sid = row.get("session_id")
        if sid:
            history = per_session.pop(sid, [])
            history.append(row)
            per_session[sid] = history  # Window follows latest ledger activity.
    recent = list(per_session)[-window_sessions:] if window_sessions else []
    samples: dict[str, list[float]] = {"paper": [], "live": []}
    unknown = incomplete = 0
    for sid in recent:
        mode = modes.get(sid, set())
        if mode not in ({"paper"}, {"live"}):
            unknown += 1
            continue
        pnl = _session_pnl(per_session[sid])
        if pnl is None:
            incomplete += 1
            continue
        samples[next(iter(mode))].append(pnl)
    means = {mode: sum(values) / len(values) if values else None for mode, values in samples.items()}
    difference = means["live"] - means["paper"] if all(value is not None for value in means.values()) else None
    return {
        "strategy_id": strategy_id, "sessions_window": window_sessions,
        "paper_sessions": len(samples["paper"]), "live_sessions": len(samples["live"]),
        "unclassified_sessions": unknown, "incomplete_pnl_sessions": incomplete,
        "paper_mean_pnl_usd": means["paper"], "live_mean_pnl_usd": means["live"],
        "divergence_usd": difference,
        "comparison": "descriptive_unmatched_sessions; not a causal or risk-adjusted performance estimate",
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
    pnl_by_order: dict[str, float | None] = {}
    for row in pnls:
        oid = row.get("order_id") or (row.get("pnl") or {}).get("order_id")
        if not oid:
            continue
        value = _pnl_value(row)
        key = str(oid)
        if value is None:
            pnl_by_order[key] = None
        elif key not in pnl_by_order or pnl_by_order[key] is not None:
            pnl_by_order[key] = pnl_by_order.get(key, 0.0) + value
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
            if not isinstance(v, (int, float)) or _number(v) is None:
                continue
            bucket = per_indicator.setdefault(k, {"win": [], "loss": []})
            if outcome > 0:
                bucket["win"].append(float(v))
            elif outcome < 0:
                bucket["loss"].append(float(v))
    out: list[dict[str, Any]] = []
    for name, bucket in per_indicator.items():
        wins, losses = bucket["win"], bucket["loss"]
        out.append({
            "indicator": name, "win_samples": len(wins), "loss_samples": len(losses),
            "win_mean": sum(wins) / len(wins) if wins else None,
            "loss_mean": sum(losses) / len(losses) if losses else None,
            "delta": sum(wins) / len(wins) - sum(losses) / len(losses) if wins and losses else None,
        })
    out.sort(key=lambda e: e["indicator"])  # Different indicator units cannot rank causal importance.
    return {
        "strategy_id": strategy_id,
        "session_id": session_id,
        "indicators": out,
    }


def _fill_num(row: dict[str, Any], key: str) -> float | None:
    v = row.get(key)
    if v is None:
        v = (row.get("fill") or {}).get(key)
    return _number(v)
