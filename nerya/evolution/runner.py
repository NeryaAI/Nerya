"""Evolution runner — workspace-wide self-improvement tick.

Renders journal evidence + capability snapshots into a fresh
``learning_update`` proposal that operators review. Never mutates
protected scopes; the proposal is the only artifact.

Historically this lived in ``nerya/agent/self_improvement.py`` together
with the legacy per-turn proposal hook (``maybe_propose_from_turn``).
The new workspace-native agent loop has no notion of "post-turn
self-improvement" — proposals are now produced explicitly by an
operator running ``POST /evolution/reflect`` (``cli.commands.evolution``)
or by a scheduled trigger. Keeping the runner here untangles it from
the agent kernel and makes the dependency graph match the ownership
boundary.
"""

from __future__ import annotations

import json as _json
from typing import Any

from ..core.config import Config
from ..data import indicators as _indicators
from ..evolution.journal_analyzer import summarize_errors, summarize_risk
from ..evolution.patch_proposal import create_proposal
from ..evolution.reflection_engine import run_reflection
from ..evolution.event_store import record_event
from ..evolution.selector import select_assets_for_signals
from ..evolution.signals import collect_signals
from ..evolution.validation_plan import build_validation_plan, write_validation_plan
from ..llm.capability_matrix import summary as capability_summary
from ..trading import strategy_versions as _versions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evolve(config: Config) -> dict[str, Any]:
    """Run reflection + auto-create a minimal ``learning_update`` proposal."""

    reflect = run_reflection(config.paths)
    err = summarize_errors(config.paths)
    ranked = rank_proposal_seeds(reflect)
    signals = collect_signals(config.paths, persist=True)
    strategies = list((reflect or {}).get("strategies", {}).keys())
    selected_assets = select_assets_for_signals(
        config.paths,
        signals,
        strategy_id=(strategies[0] if len(strategies) == 1 else None),
    )
    event = record_event(
        config.paths,
        signals=[str(s.get("id")) for s in signals],
        genes_used=[str(g.get("id")) for g in selected_assets.get("genes", [])],
        validation_status="not_run",
        outcome="candidate",
        summary="Workspace reflection tick collected evolution signals.",
        evidence_refs=_signal_evidence_refs(signals),
        metadata={"strategy_count": len(strategies), "seed_count": len(ranked)},
    )
    risk: dict[str, Any] = {"per_strategy": {}}
    for sid in strategies:
        try:
            risk["per_strategy"][sid] = summarize_risk(config.paths, sid)
        except Exception:
            continue
    summary = (
        f"Reflection tick. errors={err.get('count', 0)}, "
        f"strategies={len(strategies)}, seeds={len(ranked)}."
    )
    rationale_lines = [
        "# Learning update",
        "",
        "## Evidence",
        f"- errors summary: {err}",
        f"- risk summary: {risk}",
        "",
        "## Top proposal seeds (ranked by evidence)",
    ]
    if ranked:
        for i, seed in enumerate(ranked[:5], 1):
            rationale_lines.append(
                f"{i}. **{seed['kind']}** (score={seed['score']:.2f}) — "
                f"{seed.get('reason', '')} "
                f"[strategy={seed.get('strategy_id')}, "
                f"session={seed.get('session_id')}]"
            )
    else:
        rationale_lines.append("- no seeds surfaced this tick")
    rationale_lines += [
        "",
        "## Rollback plan",
        "- learning_update proposals never mutate live config. Discard the",
        "  proposal to roll back; protected scopes remain untouched.",
        "",
        "## Test plan",
        "- review attribution evidence for each seed before promoting it",
        "  to a higher-risk kind (strategy_config_patch / "
        "trigger_route_patch).",
        "",
        "See `memory/global.md` for the corresponding learning note.",
    ]
    extra = {"reflection.json": str(reflect)}
    if ranked:
        extra["ranked_seeds.json"] = _json.dumps(
            ranked, indent=2, default=str
        )
    extra["signals.json"] = _json.dumps(signals, indent=2, default=str)
    extra["selected_assets.json"] = _json.dumps(
        selected_assets, indent=2, default=str
    )
    extra["provider_capabilities.json"] = _capability_snapshot()
    extra["strategy_versions.json"] = _strategy_version_snapshot(
        config, strategies
    )
    extra["indicator_state.json"] = _indicator_snapshot()
    plan = build_validation_plan(
        [
            {"type": "manual_review", "notes": "Review ranked seeds and evidence refs."},
            {"type": "unit_test", "command": "python -m pytest tests/test_memory_memsearch_ops.py -q"},
        ],
        source="evolution.runner",
    )
    validation_plan_id = write_validation_plan(config.paths, plan)
    prop = create_proposal(
        config.paths,
        kind="learning_update",
        summary=summary,
        rationale="\n".join(rationale_lines),
        extra_files=extra,
        evidence_refs=_signal_evidence_refs(signals),
        source_event_id=str(event.get("id") or ""),
        validation_plan_id=validation_plan_id,
        metadata={
            "signals": [str(s.get("id")) for s in signals],
            "genes_used": [str(g.get("id")) for g in selected_assets.get("genes", [])],
        },
    )
    record_event(
        config.paths,
        parent_id=str(event.get("id") or ""),
        signals=[str(s.get("id")) for s in signals],
        genes_used=[str(g.get("id")) for g in selected_assets.get("genes", [])],
        proposal_id=prop.id,
        validation_status="not_run",
        outcome="proposed",
        summary=summary,
        evidence_refs=_signal_evidence_refs(signals),
        metadata={"validation_plan_id": validation_plan_id},
    )
    return {
        "reflection": reflect,
        "proposal": prop.asdict(),
        "ranked": ranked,
        "signals": signals,
        "selected_assets": selected_assets,
        "event": event,
    }


def rank_proposal_seeds(reflection: dict[str, Any]) -> list[dict[str, Any]]:
    """Rank proposal seeds surfaced by reflection by evidence strength."""

    strategies = (reflection or {}).get("strategies") or {}
    ranked: list[dict[str, Any]] = []
    for sid, findings in strategies.items():
        for row in findings.get("attribution") or []:
            top = row.get("top_cause") or {}
            weight = float(top.get("weight") or 0.0)
            pnl = abs(float(row.get("pnl_usd") or 0.0))
            pnl_boost = min(pnl / 500.0, 1.0)
            subagents = row.get("subagents") or []
            sub_score = min(
                sum(s.get("calls", 0) for s in subagents) / 10.0, 0.5
            )
            for seed in row.get("proposal_seeds") or []:
                ranked.append({
                    **seed,
                    "strategy_id": sid,
                    "session_id": row.get("session_id"),
                    "score": round(weight + pnl_boost + sub_score, 4),
                    "evidence": {
                        "cause": top.get("cause"),
                        "cause_weight": weight,
                        "pnl_usd": row.get("pnl_usd"),
                        "subagent_calls": sum(
                            s.get("calls", 0) for s in subagents
                        ),
                    },
                })
    ranked.sort(key=lambda s: s["score"], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _strategy_version_snapshot(
    config: Config, strategy_ids: list[str]
) -> str:
    snap: dict[str, Any] = {}
    for sid in strategy_ids:
        try:
            active = _versions.active_version_id(config.paths, sid)
        except Exception:
            active = None
        try:
            promos = _versions.list_promotions(config.paths, sid)
        except Exception:
            promos = []
        snap[sid] = {
            "active_version_id": active,
            "recent_promotions": [p.asdict() for p in (promos or [])[-3:]],
        }
    return _json.dumps(snap, indent=2, default=str)


def _indicator_snapshot() -> str:
    try:
        cap = _indicators.capability()
    except Exception as exc:
        cap = {"error": str(exc)}
    return _json.dumps({
        "talib_installed": _indicators.has_talib(),
        "capability": cap,
    }, indent=2, default=str)


def _capability_snapshot() -> str:
    try:
        data = capability_summary()
    except Exception:
        data = []
    return _json.dumps(data, indent=2, default=str)


def _signal_evidence_refs(signals: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for sig in signals:
        for ref in sig.get("evidence_refs") or []:
            s = str(ref)
            if s not in refs:
                refs.append(s)
    return refs[:50]


__all__ = ["evolve", "rank_proposal_seeds"]
