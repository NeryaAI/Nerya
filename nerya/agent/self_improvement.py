"""Self-improvement hooks.

* `evolve(config)` — run reflection + create a `learning_update` proposal.
* `maybe_propose_from_turn(config, turn)` — lightweight per-turn hook that
  detects repeated patterns (e.g. N consecutive no-op turns, unusually many
  errors) and emits a proposal. Never writes to active limits or signer
  policy — only generates a proposal for operator review.
"""

from __future__ import annotations

from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..data import indicators as _indicators
from ..evolution.journal_analyzer import summarize_errors, summarize_risk
from ..evolution.patch_proposal import create_proposal
from ..evolution.reflection_engine import run_reflection
from ..llm.capability_matrix import summary as capability_summary
from ..trading import strategy_versions as _versions


def evolve(config: Config) -> dict[str, Any]:
    """Run reflection + auto-create a minimal `learning_update` proposal."""
    reflect = run_reflection(config.paths)
    err = summarize_errors(config.paths)
    ranked = rank_proposal_seeds(reflect)
    strategies = list((reflect or {}).get("strategies", {}).keys())
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
                f"[strategy={seed.get('strategy_id')}, session={seed.get('session_id')}]"
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
        "  to a higher-risk kind (strategy_config_patch / trigger_route_patch).",
        "",
        "See `memory/global.md` for the corresponding learning note.",
    ]
    extra = {"reflection.json": str(reflect)}
    if ranked:
        import json as _json
        extra["ranked_seeds.json"] = _json.dumps(ranked, indent=2, default=str)
    extra["provider_capabilities.json"] = _capability_snapshot()
    # Phase 5 — feed proposal with strategy version + indicator state so
    # reviewers see exactly which snapshot and which indicator backend
    # was active when the seeds were ranked.
    extra["strategy_versions.json"] = _strategy_version_snapshot(
        config, strategies)
    extra["indicator_state.json"] = _indicator_snapshot()
    prop = create_proposal(
        config.paths,
        kind="learning_update",
        summary=summary,
        rationale="\n".join(rationale_lines),
        extra_files=extra,
    )
    return {"reflection": reflect, "proposal": prop.asdict(), "ranked": ranked}


def rank_proposal_seeds(reflection: dict[str, Any]) -> list[dict[str, Any]]:
    """Rank proposal seeds surfaced by reflection by evidence strength.

    Uses Phase 8 attribution (top-cause weight + pnl magnitude +
    subagent volume) and Phase 13 provider quality (penalise strategies
    that depend on providers with unsupported capabilities) to order
    seeds deterministically.
    """
    strategies = (reflection or {}).get("strategies") or {}
    ranked: list[dict[str, Any]] = []
    for sid, findings in strategies.items():
        for row in findings.get("attribution") or []:
            top = row.get("top_cause") or {}
            weight = float(top.get("weight") or 0.0)
            pnl = abs(float(row.get("pnl_usd") or 0.0))
            pnl_boost = min(pnl / 500.0, 1.0)   # cap at 1.0
            subagents = row.get("subagents") or []
            sub_score = min(sum(s.get("calls", 0) for s in subagents) / 10.0, 0.5)
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
                        "subagent_calls": sum(s.get("calls", 0) for s in subagents),
                    },
                })
    ranked.sort(key=lambda s: s["score"], reverse=True)
    return ranked


def _strategy_version_snapshot(config: Config,
                               strategy_ids: list[str]) -> str:
    """Pin the active version + recent promotions for each strategy.

    Self-improvement proposals must be reviewable months after they
    were filed. Embedding the active ``version_id`` (plus the last
    three promotions) lets a reviewer reconstruct exactly which
    snapshot the recommendation targets, even after rollbacks.
    """
    import json as _json
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
    """Which indicator backend is currently serving the runtime."""
    import json as _json
    try:
        cap = _indicators.capability()
    except Exception as exc:
        cap = {"error": str(exc)}
    return _json.dumps({
        "talib_installed": _indicators.has_talib(),
        "capability": cap,
    }, indent=2, default=str)


def _capability_snapshot() -> str:
    """Return a compact JSON snapshot of provider capabilities, pinned into
    the proposal so reviewers can see what the runtime believed each
    provider supported at proposal time."""
    import json as _json
    try:
        data = capability_summary()
    except Exception:
        data = []
    return _json.dumps(data, indent=2, default=str)


def maybe_propose_from_turn(config: Config, *, turn: dict[str, Any]) -> dict[str, Any] | None:
    """Scan recent journals for patterns worth flagging as a proposal.

    Returns the created proposal dict if one was emitted, otherwise `None`.
    Intentionally conservative — only fires when the heuristic is unambiguous.
    """
    # Read last N agent turn records.
    p = config.paths.journal("agent")
    if not p.exists():
        return None
    tail: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines()[-50:]:
        try:
            import json as _json
            rec = _json.loads(line)
        except Exception:
            continue
        if rec.get("kind") == "agent.turn.end":
            tail.append(rec)
    if len(tail) < 10:
        return None
    noops = sum(1 for t in tail[-10:] if t.get("action") == "noop")
    if noops < 9:
        return None
    summary = f"{noops}/10 recent turns were noop — consider adjusting plan heuristics."
    prop = create_proposal(
        config.paths,
        kind="learning_update",
        summary=summary,
        rationale=(
            "# Many consecutive noop turns\n\n"
            f"- recent_turns: {len(tail)}\n"
            f"- noops: {noops}\n\n"
            "Operator should review planner heuristics or add more subagents."
        ),
    )
    jsonl.append(config.paths.journal("self_improvement"), {
        "kind": "self_improvement.auto_proposal",
        "trigger": "consecutive_noops",
        "proposal_id": prop.id,
    })
    return prop.asdict()
