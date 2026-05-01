"""Reflection runner.

Scans both the global journals and per-strategy history ledgers to surface
patterns that the main agent or a human operator should look at. No change
is ever applied automatically — findings become ``learning_update`` notes
and (optionally) evolution proposals.

The individual ``find_*`` helpers are pure functions over already-written
ledger rows, so they are cheap, offline and fully testable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..strategy_history import store
from ..strategy_history.attribution import (
    attribute_session,
    subagent_contribution,
    paper_vs_live_divergence,
)
from .learning_writer import append_global_learning, append_strategy_learning


# ---------------------------------------------------------------------------
# Finders
# ---------------------------------------------------------------------------


def find_losses(paths: WorkspacePaths, strategy_id: str,
                *, min_loss_usd: float = 50.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows = store.read_ledger(paths, strategy_id, "pnl")
    for r in rows:
        pnl = (r.get("pnl") or {})
        realized = float(pnl.get("realized_usd", pnl.get("realized_pnl_usd", 0)) or 0)
        if realized <= -abs(min_loss_usd):
            out.append({
                "issue": "loss",
                "session_id": r.get("session_id"),
                "realized_usd": realized,
                "details": pnl,
            })
    return out


def find_bad_triggers(paths: WorkspacePaths, strategy_id: str,
                      *, top: int = 5) -> list[dict[str, Any]]:
    """Triggers that frequently lead to rejected risk or losing fills."""
    risks = store.read_ledger(paths, strategy_id, "risk")
    triggers = store.read_ledger(paths, strategy_id, "triggers")
    losses = {row["session_id"] for row in find_losses(paths, strategy_id)}

    # Map session -> trigger route
    session_to_route: dict[str, str] = {}
    for tr in triggers:
        sid = tr.get("session_id")
        route = (tr.get("event") or {}).get("name") or (tr.get("event") or {}).get("source")
        if sid and route:
            session_to_route[sid] = route

    bad_counter: Counter[str] = Counter()
    for r in risks:
        if (r.get("risk_decision") or {}).get("decision") == "reject":
            route = session_to_route.get(r.get("session_id"))
            if route:
                bad_counter[route] += 1
    for sid in losses:
        route = session_to_route.get(sid)
        if route:
            bad_counter[route] += 1

    return [
        {"issue": "bad_trigger", "route": route, "bad_events": count}
        for route, count in bad_counter.most_common(top)
    ]


def find_high_slippage(paths: WorkspacePaths, strategy_id: str,
                       *, threshold_bps: float = 50.0) -> list[dict[str, Any]]:
    """Compare fill price vs intent reference/limit price in bps."""
    fills = store.read_ledger(paths, strategy_id, "fills")
    intents = {
        (r.get("intent") or {}).get("intent_id"): (r.get("intent") or {})
        for r in store.read_ledger(paths, strategy_id, "intents")
    }
    out: list[dict[str, Any]] = []
    for fr in fills:
        f = fr.get("fill") or {}
        intent = intents.get(f.get("intent_id")) or {}
        ref = intent.get("limit_price") or intent.get("reference_price")
        price = f.get("price")
        if not ref or not price:
            continue
        try:
            slip_bps = abs(float(price) - float(ref)) / float(ref) * 10_000.0
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if slip_bps >= threshold_bps:
            out.append({
                "issue": "high_slippage",
                "session_id": fr.get("session_id"),
                "order_id": f.get("order_id"),
                "slippage_bps": round(slip_bps, 2),
                "reference_price": ref,
                "fill_price": price,
            })
    return out


def find_stale_data(paths: WorkspacePaths, strategy_id: str,
                    *, max_stale_s: float = 60.0) -> list[dict[str, Any]]:
    """Triggers whose payload carries a ``data_age_s`` above the threshold."""
    out: list[dict[str, Any]] = []
    for r in store.read_ledger(paths, strategy_id, "triggers"):
        ev = r.get("event") or {}
        payload = ev.get("payload") or {}
        age = payload.get("data_age_s")
        if age is None:
            continue
        try:
            age_f = float(age)
        except (TypeError, ValueError):
            continue
        if age_f > max_stale_s:
            out.append({
                "issue": "stale_data",
                "session_id": r.get("session_id"),
                "route": ev.get("name"),
                "data_age_s": age_f,
            })
    return out


def find_subagent_disagreement(paths: WorkspacePaths,
                               strategy_id: str) -> list[dict[str, Any]]:
    """Sessions where multiple subagents produced conflicting verdicts."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in store.read_ledger(paths, strategy_id, "subagents"):
        by_session[r.get("session_id")].append(r)
    out: list[dict[str, Any]] = []
    for sid, rows in by_session.items():
        verdicts = {((r.get("output") or {}).get("verdict")
                     or (r.get("output") or {}).get("action")) for r in rows}
        verdicts.discard(None)
        if len(verdicts) > 1:
            out.append({
                "issue": "subagent_disagreement",
                "session_id": sid,
                "verdicts": sorted(v for v in verdicts if v),
                "agents": [r.get("name") for r in rows],
            })
    return out


def find_overtrading(paths: WorkspacePaths, strategy_id: str,
                     *, window_s: float = 3600.0,
                     max_trades: int = 10) -> list[dict[str, Any]]:
    """Rolling windows with too many intents signal overtrading."""
    from datetime import datetime

    ts_list: list[float] = []
    for r in store.read_ledger(paths, strategy_id, "intents"):
        ts = (r.get("intent") or {}).get("ts")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        ts_list.append(dt.timestamp())
    ts_list.sort()
    if len(ts_list) <= max_trades:
        return []

    hits: list[dict[str, Any]] = []
    for i, end in enumerate(ts_list):
        start_idx = i - max_trades
        if start_idx < 0:
            continue
        window = end - ts_list[start_idx]
        if window <= window_s:
            hits.append({
                "issue": "overtrading",
                "window_s": window_s,
                "trades_in_window": max_trades + 1,
                "end_ts": end,
            })
            break  # one hit is enough
    return hits


def find_missed_opportunities(paths: WorkspacePaths,
                              strategy_id: str) -> list[dict[str, Any]]:
    """Sessions with an analysis verdict of buy/sell but no intent submitted."""
    verdicts_by_session: dict[str, str] = {}
    for r in store.read_ledger(paths, strategy_id, "subagents"):
        sid = r.get("session_id")
        out = r.get("output") or {}
        verdict = out.get("verdict") or out.get("action")
        if sid and verdict and verdict.lower() in {"buy", "sell", "enter", "open"}:
            verdicts_by_session[sid] = verdict

    intents_by_session = {r.get("session_id")
                          for r in store.read_ledger(paths, strategy_id, "intents")}

    return [
        {"issue": "missed_opportunity", "session_id": sid, "verdict": v}
        for sid, v in verdicts_by_session.items()
        if sid not in intents_by_session
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _session_attribution(paths: WorkspacePaths, sid: str,
                         *, max_sessions: int = 25) -> list[dict[str, Any]]:
    """For each of the most recent sessions, run the attribution
    pipeline and surface the top root cause plus subagent summary. This
    gives proposals explicit evidence to cite instead of vague
    "something seems off" language."""
    seen: list[str] = []
    for row in store.read_ledger(paths, sid, "triggers"):
        session = row.get("session_id")
        if not session or session in seen:
            continue
        seen.append(session)
    out: list[dict[str, Any]] = []
    for session in seen[-max_sessions:]:
        bundle = attribute_session(paths, sid, session).as_dict()
        if not bundle.get("root_causes") and not bundle.get("proposal_seeds"):
            continue
        subs = subagent_contribution(paths, sid, session)
        out.append({
            "session_id": session,
            "top_cause": (bundle["root_causes"][0]
                          if bundle["root_causes"] else None),
            "proposal_seeds": bundle.get("proposal_seeds", []),
            "pnl_usd": bundle.get("pnl_usd"),
            "subagents": subs.get("subagents", []),
        })
    return out


def _list_strategy_ids(paths: WorkspacePaths) -> list[str]:
    root = paths.root / "strategies"
    if not root.exists():
        return []
    return [p.name for p in root.iterdir() if p.is_dir() and (p / "history").exists()]


def run_reflection(paths: WorkspacePaths,
                   strategy_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Scan the journals, write a global learning note and per-strategy notes
    for any finding. Returns a summary dict with all findings."""
    errors = jsonl.read_all(paths.journal("errors"))
    trading = jsonl.read_all(paths.journal("trading"))
    skills = jsonl.read_all(paths.journal("skills"))
    evolution = jsonl.read_all(paths.journal("evolution"))

    strategies = list(strategy_ids) if strategy_ids is not None else _list_strategy_ids(paths)

    per_strategy: dict[str, dict[str, list]] = {}
    for sid in strategies:
        findings = {
            "losses": find_losses(paths, sid),
            "bad_triggers": find_bad_triggers(paths, sid),
            "high_slippage": find_high_slippage(paths, sid),
            "stale_data": find_stale_data(paths, sid),
            "subagent_disagreement": find_subagent_disagreement(paths, sid),
            "overtrading": find_overtrading(paths, sid),
            "missed_opportunity": find_missed_opportunities(paths, sid),
        }
        # v2 — feed reflection with attribution evidence.
        try:
            findings["attribution"] = _session_attribution(paths, sid)
        except Exception:
            findings["attribution"] = []
        try:
            findings["paper_live_divergence"] = [paper_vs_live_divergence(paths, sid)]
        except Exception:
            findings["paper_live_divergence"] = []
        per_strategy[sid] = findings
        # write a strategy learning note if anything fired
        if any(findings.values()):
            append_strategy_learning(
                paths, sid,
                note=(f"Reflection for {sid}: "
                      + ", ".join(f"{k}={len(v)}" for k, v in findings.items() if v)),
                kind="strategy",
            )

    summary_note = (
        f"Reflection scan: errors={len(errors)}, trading_events={len(trading)}, "
        f"skill_events={len(skills)}, evolution_events={len(evolution)}, "
        f"strategies_scanned={len(strategies)}.\n"
        f"Recent error samples: {errors[-3:]}"
    )
    path = append_global_learning(paths, note=summary_note, kind="global")
    return {
        "ok": True,
        "file": str(path),
        "errors": len(errors),
        "strategies": per_strategy,
    }
