"""Proposal ranking from attribution evidence.

Purpose
-------
's governing requirement is that every agent-authored change is
proposal-only and evidence-backed. We had proposal *creation* covered
in :mod:`nerya.evolution.patch_proposal`, but nothing consumed the
richer surfaces (attribution bundles, reflection findings,
paper/live divergence) to rank or prioritize proposals for an
operator.

This module closes that loop. Given the workspace state, it:

1. Mines recent evidence from journals and attribution helpers.
2. Scores every open proposal on three axes:
   ``severity`` — how big is the problem the proposal addresses
   ``freshness`` — how recently did the evidence appear
   ``scope``    — how focused is the fix (narrow > broad)
3. Emits a ranked list with a machine-readable ``score`` and a short
   ``rationale`` string that cites the evidence that drove the score.

Design intent
-------------
* **No network calls.** Everything is derived from on-disk journals
  and the existing strategy_history helpers so the ranker is cheap
  and deterministic.
* **Proposal-only.** Nothing here mutates a proposal's state, and the
  ranker never touches protected scopes.
* **Transparent scoring.** Scores are computed with simple, visible
  arithmetic. No hidden weights, no ML model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..strategy_history import attribution as _attribution
from ..strategy_history import store as _history_store
from . import reflection_engine
from .patch_proposal import Proposal, list_proposals


# -------------------------------------------------------- scoring constants
_KIND_SCOPE_WEIGHT: dict[str, float] = {
    # narrower fixes are preferred when the evidence is equivalent
    "learning_update":        1.0,
    "prompt_patch":           0.9,
    "trigger_route_patch":    0.85,
    "risk_limit_suggestion":  0.8,
    "strategy_config_patch":  0.75,
    "skill_proposal":         0.6,
    "script_proposal":        0.55,
}

_STATE_WEIGHT: dict[str, float] = {
    "draft":    1.00,
    "proposed": 0.95,
    "approved": 0.60,  # already decided, less urgent to surface again
    "applied":  0.25,
    "rolledback": 0.40,
    "rejected": 0.10,
}


# -------------------------------------------------------- evidence bundle
@dataclass
class EvidenceBundle:
    """Aggregated, read-only evidence snapshot for a single strategy.

    This is what the ranker consumes. Callers can also render it for
    UI / operator review without running the ranker.
    """

    strategy_id: str
    losses: list[dict[str, Any]] = field(default_factory=list)
    bad_triggers: list[dict[str, Any]] = field(default_factory=list)
    high_slippage: list[dict[str, Any]] = field(default_factory=list)
    stale_data: list[dict[str, Any]] = field(default_factory=list)
    subagent_disagreement: list[dict[str, Any]] = field(default_factory=list)
    overtrading: list[dict[str, Any]] = field(default_factory=list)
    missed_opportunity: list[dict[str, Any]] = field(default_factory=list)
    divergence: dict[str, Any] = field(default_factory=dict)

    def severity(self) -> float:
        """Roll up the evidence into a 0..1 severity score.

        The score is the fraction of reflection buckets that contain
        at least one finding, clamped to ``[0, 1]``. Divergence adds a
        bonus if ``paper vs live`` differ by more than $10/session.
        """
        buckets = (
            self.losses, self.bad_triggers, self.high_slippage,
            self.stale_data, self.subagent_disagreement,
            self.overtrading, self.missed_opportunity,
        )
        non_empty = sum(1 for b in buckets if b)
        base = non_empty / max(1, len(buckets))
        div = abs(float(self.divergence.get("divergence_usd") or 0.0))
        bonus = 0.1 if div >= 10.0 else 0.0
        return min(1.0, base + bonus)

    def signals(self) -> list[str]:
        """Return the human-readable names of the non-empty buckets."""
        out: list[str] = []
        for name, bucket in (
            ("losses", self.losses),
            ("bad_triggers", self.bad_triggers),
            ("high_slippage", self.high_slippage),
            ("stale_data", self.stale_data),
            ("subagent_disagreement", self.subagent_disagreement),
            ("overtrading", self.overtrading),
            ("missed_opportunity", self.missed_opportunity),
        ):
            if bucket:
                out.append(f"{name}={len(bucket)}")
        div = self.divergence.get("divergence_usd")
        if div is not None and abs(float(div)) >= 10.0:
            out.append(f"paper_vs_live={div}")
        return out


def build_evidence(paths: WorkspacePaths, strategy_id: str) -> EvidenceBundle:
    """Read every reflection + attribution signal for a strategy.

    Best-effort: each signal is independently try/excepted so an empty
    or malformed ledger never blocks the bundle. The caller can inspect
    ``signals()`` to see which buckets fired.
    """

    def _safe(fn, *args):
        try:
            return fn(*args) or []
        except Exception:
            return []

    try:
        divergence = _attribution.paper_vs_live_divergence(paths, strategy_id)
    except Exception:
        divergence = {}

    return EvidenceBundle(
        strategy_id=strategy_id,
        losses=_safe(reflection_engine.find_losses, paths, strategy_id),
        bad_triggers=_safe(reflection_engine.find_bad_triggers, paths, strategy_id),
        high_slippage=_safe(reflection_engine.find_high_slippage, paths, strategy_id),
        stale_data=_safe(reflection_engine.find_stale_data, paths, strategy_id),
        subagent_disagreement=_safe(
            reflection_engine.find_subagent_disagreement, paths, strategy_id,
        ),
        overtrading=_safe(reflection_engine.find_overtrading, paths, strategy_id),
        missed_opportunity=_safe(
            reflection_engine.find_missed_opportunities, paths, strategy_id,
        ),
        divergence=divergence,
    )


# -------------------------------------------------------- ranking
@dataclass
class RankedProposal:
    proposal: Proposal
    score: float
    severity: float
    freshness: float
    scope: float
    state_weight: float
    signals: list[str]
    rationale: str

    def asdict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal.id,
            "kind": self.proposal.kind,
            "state": self.proposal.state,
            "summary": self.proposal.summary,
            "score": round(self.score, 4),
            "severity": round(self.severity, 4),
            "freshness": round(self.freshness, 4),
            "scope": round(self.scope, 4),
            "state_weight": round(self.state_weight, 4),
            "signals": self.signals,
            "rationale": self.rationale,
        }


def _freshness(evidence: EvidenceBundle) -> float:
    """Rough freshness proxy — 1.0 if any bucket fired at all, else 0.3.

    We deliberately do NOT try to derive a real timestamp here because
    the reflection buckets already apply their own recency windows.
    The signal is simply "did recent evidence fire?" which is enough
    to separate stale proposals from live ones in the ranking.
    """
    return 1.0 if evidence.signals() else 0.3


def rank_proposal(
    proposal: Proposal, evidence: EvidenceBundle,
) -> RankedProposal:
    sev = evidence.severity()
    fresh = _freshness(evidence)
    scope = _KIND_SCOPE_WEIGHT.get(proposal.kind, 0.5)
    state_w = _STATE_WEIGHT.get(proposal.state, 0.5)

    score = sev * 0.5 + fresh * 0.2 + scope * 0.15 + state_w * 0.15
    signals = evidence.signals()
    if signals:
        rationale = (
            f"Evidence supports this proposal: {', '.join(signals)}. "
            f"Severity {sev:.2f}, scope {scope:.2f}, state {proposal.state!r}."
        )
    else:
        rationale = (
            f"No recent evidence found for strategy "
            f"{evidence.strategy_id!r}. Score reflects scope+state only."
        )
    return RankedProposal(
        proposal=proposal,
        score=score,
        severity=sev,
        freshness=fresh,
        scope=scope,
        state_weight=state_w,
        signals=signals,
        rationale=rationale,
    )


def rank_proposals(
    paths: WorkspacePaths, *,
    strategy_id: str | None = None,
    states: tuple[str, ...] | None = ("draft", "proposed"),
) -> list[RankedProposal]:
    """Return every proposal ranked high-to-low.

    * ``strategy_id`` — if given, rank only proposals whose target
      references that strategy (best-effort substring match) and
      evaluate evidence from that strategy. If ``None``, each proposal
      is scored against its own strategy when derivable; else against
      an empty bundle.
    * ``states`` — restrict to proposal lifecycle states. By default
      only ``draft`` and ``proposed`` proposals are ranked because
      those are the ones an operator can still act on.
    """
    all_proposals = list_proposals(paths)
    if states is not None:
        state_set = set(states)
        all_proposals = [p for p in all_proposals if p.state in state_set]

    evidence_cache: dict[str, EvidenceBundle] = {}

    def _evidence_for(sid: str | None) -> EvidenceBundle:
        sid = sid or ""
        if sid in evidence_cache:
            return evidence_cache[sid]
        bundle = (
            build_evidence(paths, sid) if sid
            else EvidenceBundle(strategy_id="")
        )
        evidence_cache[sid] = bundle
        return bundle

    ranked: list[RankedProposal] = []
    for proposal in all_proposals:
        sid = strategy_id or _derive_strategy_id(proposal)
        evidence = _evidence_for(sid)
        if strategy_id and sid and not _proposal_touches_strategy(
            proposal, strategy_id,
        ):
            continue
        ranked.append(rank_proposal(proposal, evidence))
    ranked.sort(key=lambda rp: rp.score, reverse=True)
    return ranked


def _derive_strategy_id(proposal: Proposal) -> str | None:
    """Best-effort: find a strategy_id the proposal references.

    We look at ``target``, ``summary``, and ``rationale`` for
    ``strategies/<id>/`` or ``strategy_id: <id>`` hints. The result is
    used only to pick an evidence bundle; a miss just returns an empty
    bundle and ranks on scope/state.
    """
    haystacks: list[str] = []
    for field_name in ("target", "summary"):
        value = getattr(proposal, field_name, None)
        if value:
            haystacks.append(str(value))
    for hay in haystacks:
        for needle in ("strategies/", "strategy_id:"):
            if needle in hay:
                token = hay.split(needle, 1)[1]
                sid = token.split("/", 1)[0].split()[0].strip().strip(",;'\"")
                if sid:
                    return sid
    return None


def _proposal_touches_strategy(proposal: Proposal, strategy_id: str) -> bool:
    derived = _derive_strategy_id(proposal)
    if derived is None:
        # Unattributed proposals (e.g. global prompt patches) do not
        # belong to a single strategy. They surface in unscoped ranks
        # but not in strategy-scoped ones.
        return False
    return derived == strategy_id


# -------------------------------------------------------- journal / snapshot
def write_ranking_snapshot(
    paths: WorkspacePaths, ranked: list[RankedProposal],
) -> dict[str, Any]:
    """Persist the latest ranking under `workspace/evolution/ranking.json`.

    Operator UIs can load this file to render the current priority list
    without re-running the ranker.
    """
    snapshot = {
        "generated_at": now_iso(),
        "count": len(ranked),
        "ranked": [rp.asdict() for rp in ranked],
    }
    out_path = paths.evolution / "ranking.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    out_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"path": str(out_path), "count": len(ranked)}


__all__ = [
    "EvidenceBundle", "RankedProposal",
    "build_evidence", "rank_proposal", "rank_proposals",
    "write_ranking_snapshot",
]
