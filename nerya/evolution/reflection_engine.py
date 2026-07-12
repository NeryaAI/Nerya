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
from ..core.config import Config
from ..core.paths import WorkspacePaths
from ..strategy_history import store
from ..strategy_history.attribution import (
    attribute_session,
    subagent_contribution,
    paper_vs_live_divergence,
)


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


def _validated_strategy_ids(
    paths: WorkspacePaths,
    strategy_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    root = paths.strategies.resolve()
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in strategy_ids:
        strategy_id = str(raw or "").strip()
        if not strategy_id:
            invalid.append(strategy_id)
            continue
        if strategy_id in seen:
            continue
        seen.add(strategy_id)
        try:
            strategy_path = (root / strategy_id).resolve()
            strategy_path.relative_to(root)
        except (ValueError, OSError):
            invalid.append(strategy_id)
            continue
        if not strategy_path.is_dir():
            invalid.append(strategy_id)
            continue
        valid.append(strategy_id)
    return valid, invalid


def run_reflection(paths: WorkspacePaths,
                   strategy_ids: Iterable[str] | None = None,
                   *,
                   config: Config | None = None) -> dict[str, Any]:
    """Scan the journals, write a global learning note and per-strategy notes
    for any finding. Returns a summary dict with all findings."""
    errors = jsonl.read_all(paths.journal("errors"))
    trading = jsonl.read_all(paths.journal("trading"))
    skills = jsonl.read_all(paths.journal("skills"))
    evolution = jsonl.read_all(paths.journal("evolution"))
    runtime_config = config or Config(paths=paths, data={})

    requested = (
        list(strategy_ids)
        if strategy_ids is not None
        else _list_strategy_ids(paths)
    )
    strategies, invalid_strategy_ids = _validated_strategy_ids(paths, requested)
    memory_write_errors: list[dict[str, str]] = []

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
            note = (
                f"Reflection for {sid}: "
                + ", ".join(
                    f"{key}={len(value)}"
                    for key, value in findings.items()
                    if value
                )
            )
            from ..memory.runtime import MemoryRuntime

            try:
                remembered = MemoryRuntime(runtime_config, strategy_id=sid).remember(
                    category="learning",
                    content=note,
                    scope="strategy",
                    key="reflection.latest",
                    source="reflection:strategy",
                    evidence_refs=[f"strategy:{sid}"],
                    writer_id="reflection_engine",
                )
                if not remembered.ok:
                    memory_write_errors.append({
                        "scope": "strategy",
                        "strategy_id": sid,
                        "skip_reason": remembered.skip_reason or "write_failed",
                    })
            except Exception as exc:  # noqa: BLE001 - report the failed tick
                memory_write_errors.append({
                    "scope": "strategy",
                    "strategy_id": sid,
                    "skip_reason": f"{type(exc).__name__}: {exc}",
                })

    summary_note = (
        f"Reflection scan: errors={len(errors)}, trading_events={len(trading)}, "
        f"skill_events={len(skills)}, evolution_events={len(evolution)}, "
        f"strategies_scanned={len(strategies)}.\n"
        f"Recent error samples: {errors[-3:]}"
    )
    from ..memory.runtime import MemoryRuntime

    try:
        global_write = MemoryRuntime(runtime_config).remember(
            category="learning",
            content=summary_note,
            scope="global",
            key="reflection.workspace.latest",
            source="reflection:global",
            evidence_refs=[
                "journal:errors",
                "journal:trading",
                "journal:skills",
                "journal:evolution",
            ],
            writer_id="reflection_engine",
        )
        if not global_write.ok:
            memory_write_errors.append({
                "scope": "global",
                "strategy_id": "",
                "skip_reason": global_write.skip_reason or "write_failed",
            })
    except Exception as exc:  # noqa: BLE001 - report the failed tick
        memory_write_errors.append({
            "scope": "global",
            "strategy_id": "",
            "skip_reason": f"{type(exc).__name__}: {exc}",
        })
    path = paths.memory / "global.md"
    write_error = (
        memory_write_errors[0]["skip_reason"]
        if memory_write_errors
        else ""
    )
    return {
        "ok": not invalid_strategy_ids and not memory_write_errors,
        "file": str(path),
        "errors": len(errors),
        "strategies": per_strategy,
        "invalid_strategy_ids": invalid_strategy_ids,
        "memory_write_errors": memory_write_errors,
        "write_error": write_error,
    }
