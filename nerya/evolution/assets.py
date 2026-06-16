"""Nerya-native evolution asset store.

Assets are persistent learning units. Genes are reusable rules; capsules
are validated cases. Candidate and rejected streams remain append-only so
operator decisions can be audited later.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .asset_policy import validate_mutation_scope, validate_validation_commands
from .event_store import record_event


GeneCategory = Literal["repair", "optimize", "harden", "research", "strategy", "skill"]


@dataclass(frozen=True)
class EvolutionGene:
    id: str
    category: GeneCategory
    signals_match: list[str]
    preconditions: list[str]
    strategy: list[str]
    validation: list[str]
    forbidden_scopes: list[str] = field(default_factory=list)
    max_files: int = 5
    confidence: float = 0.5
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvolutionCapsule:
    id: str
    gene_id: str | None
    source_event_id: str | None
    summary: str
    evidence_refs: list[str]
    validation_results: list[dict[str, Any]]
    outcome_score: float
    promotion_ref: str | None = None
    strategy_id: str | None = None
    ts: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvolutionAssetCandidate:
    id: str
    kind: Literal["gene", "capsule"]
    summary: str
    payload: dict[str, Any]
    evidence_refs: list[str] = field(default_factory=list)
    source_event_id: str | None = None
    strategy_id: str | None = None
    state: Literal["candidate", "promoted", "rejected"] = "candidate"
    safe_to_promote: bool = True
    blocked_reasons: list[str] = field(default_factory=list)
    promotion_gates: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_GENES: tuple[EvolutionGene, ...] = (
    EvolutionGene(
        id="gene_nerya_repair_from_tool_failures",
        category="repair",
        signals_match=["tool_failure_cluster", "script_error"],
        preconditions=["two_or_more_failures_share_a_tool_or_script"],
        strategy=[
            "read failing tool/script evidence",
            "draft a minimal PatchProposal with a focused regression check",
        ],
        validation=["python -m pytest tests/ -q"],
        forbidden_scopes=["vault/*", "accounts/*", "approvals/*"],
        max_files=5,
        confidence=0.7,
        summary="Repair repeated tool or script failures through proposal-first patches.",
    ),
    EvolutionGene(
        id="gene_nerya_harden_repeated_noop",
        category="harden",
        signals_match=["repeated_noop"],
        preconditions=["recent_turns_have_no_message_and_no_tool_calls"],
        strategy=[
            "inspect routing, prompt context, and task-state evidence",
            "create a learning_update proposal before any runtime patch",
        ],
        validation=["python -m pytest tests/test_memory_memsearch_ops.py -q"],
        forbidden_scopes=["vault/*", "accounts/*", "approvals/*"],
        max_files=3,
        confidence=0.65,
        summary="Harden agent turns that repeatedly stop without useful work.",
    ),
    EvolutionGene(
        id="gene_nerya_strategy_drawdown_review",
        category="strategy",
        signals_match=["strategy_drawdown", "high_slippage", "validation_failed"],
        preconditions=["strategy_id_is_known", "performance_snapshot_exists"],
        strategy=[
            "build a snapshot-backed tuning proposal",
            "require a structured validation plan before pending_review",
        ],
        validation=["python -m pytest tests/test_strategy_evolution_validation.py -q"],
        forbidden_scopes=["accounts/*", "vault/*", "strategies/*/limits.yml"],
        max_files=5,
        confidence=0.72,
        summary="Review strategy degradation with explicit validation gates.",
    ),
    EvolutionGene(
        id="gene_nerya_market_regime_tuning_review",
        category="strategy",
        signals_match=[
            "market_regime_trending",
            "market_regime_rangebound",
            "market_regime_high_volatility",
            "market_news_context",
            "market_data_degraded",
        ],
        preconditions=[
            "strategy_id_is_known",
            "performance_snapshot_has_market_context",
        ],
        strategy=[
            "match prior tuning lessons to the current market regime",
            "treat degraded market/news data as a validation blocker or warning",
            "keep regime-specific changes proposal-first and backtest-gated",
        ],
        validation=["python -m pytest tests/test_strategy_evolution_validation.py -q"],
        forbidden_scopes=["accounts/*", "vault/*", "strategies/*/limits.yml"],
        max_files=5,
        confidence=0.7,
        summary="Tune strategies using current market regime, news, and data-quality context.",
    ),
    EvolutionGene(
        id="gene_nerya_skill_failure_patch",
        category="skill",
        signals_match=["skill_example_failed", "script_error"],
        preconditions=["skill_or_script_path_is_known"],
        strategy=[
            "keep SKILL.md as the capability definition",
            "patch scripts/ or examples through PatchProposal",
        ],
        validation=["python -m pytest tests/ -q"],
        forbidden_scopes=["vault/*", "accounts/*", "approvals/*"],
        max_files=6,
        confidence=0.68,
        summary="Fix skill/script failures without adding YAML action surfaces.",
    ),
    EvolutionGene(
        id="gene_nerya_memory_quality_filter",
        category="harden",
        signals_match=["memory_low_value_write", "user_correction"],
        preconditions=["candidate_has_evidence_refs"],
        strategy=[
            "score memory writes before global persistence",
            "prefer event evidence over always-on prompt bloat",
        ],
        validation=["python -m pytest tests/test_memory_memsearch_ops.py -q"],
        forbidden_scopes=["vault/*"],
        max_files=3,
        confidence=0.62,
        summary="Prevent low-value or unsafe memory pollution.",
    ),
    EvolutionGene(
        id="gene_nerya_proposal_outcome_learning",
        category="research",
        signals_match=["proposal_rejected", "proposal_rolled_back"],
        preconditions=["proposal_outcome_is_terminal"],
        strategy=[
            "write negative event evidence",
            "lower confidence for matching future candidates until new evidence appears",
        ],
        validation=["python -m pytest tests/test_evolution_assets.py -q"],
        forbidden_scopes=["vault/*", "accounts/*", "approvals/*"],
        max_files=2,
        confidence=0.6,
        summary="Learn from rejected or rolled-back proposals.",
    ),
)


def list_genes(paths: WorkspacePaths) -> list[dict[str, Any]]:
    genes = [g.asdict() for g in DEFAULT_GENES]
    custom = _read_json(paths.evolution_genes, default=[])
    if isinstance(custom, list):
        by_id = {g["id"]: g for g in genes}
        for row in custom:
            if isinstance(row, dict) and row.get("id"):
                by_id[str(row["id"])] = row
        genes = list(by_id.values())
    return genes


def list_capsules(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = jsonl.read_all(paths.evolution_capsules)
    if strategy_id:
        rows = [r for r in rows if str(r.get("strategy_id") or "") == strategy_id]
    return rows[-max(1, int(limit)) :]


def list_candidates(paths: WorkspacePaths, *, limit: int = 100) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in jsonl.read_all(paths.evolution_candidates):
        cid = str(row.get("id") or "")
        if cid:
            latest[cid] = row
    rows = [
        r for r in latest.values()
        if str(r.get("state") or "candidate") == "candidate"
    ]
    return rows[-max(1, int(limit)) :]


def search_assets(
    paths: WorkspacePaths,
    *,
    kind: str | None = None,
    query: str | None = None,
    strategy_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    rows: list[dict[str, Any]] = []
    if kind in (None, "", "gene"):
        rows.extend({"kind": "gene", **g} for g in list_genes(paths))
    if kind in (None, "", "capsule"):
        rows.extend(
            {"kind": "capsule", **c}
            for c in list_capsules(paths, strategy_id=strategy_id, limit=max(limit, 500))
        )
    if q:
        rows = [
            row for row in rows
            if q in json.dumps(row, ensure_ascii=False, default=str).lower()
        ]
    if strategy_id:
        rows = [
            row for row in rows
            if row.get("kind") == "gene"
            or str(row.get("strategy_id") or "") == strategy_id
        ]
    return rows[-max(1, int(limit)) :]


def create_candidate(
    paths: WorkspacePaths,
    *,
    kind: str,
    summary: str,
    payload: dict[str, Any],
    evidence_refs: list[str] | None = None,
    source_event_id: str | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    evidence = list(evidence_refs or [])
    targets = [str(x) for x in payload.get("mutation_scope") or payload.get("targets") or []]
    commands = [str(x) for x in payload.get("validation") or payload.get("validation_commands") or []]
    policy = validate_mutation_scope(targets, max_files=int(payload.get("max_files") or 10))
    cmd_policy = validate_validation_commands(commands) if commands else None
    reasons = list(policy.reasons)
    if cmd_policy:
        reasons.extend(cmd_policy.reasons)
    gates = _candidate_promotion_gates(
        kind=kind,
        payload=payload,
        evidence_refs=evidence,
        policy_reasons=reasons,
    )
    reasons = list(gates.get("blockers") or [])
    candidate = EvolutionAssetCandidate(
        id=new_id("eac"),
        kind=kind,  # type: ignore[arg-type]
        summary=summary,
        payload=payload,
        evidence_refs=evidence,
        source_event_id=source_event_id,
        strategy_id=strategy_id,
        safe_to_promote=not reasons,
        blocked_reasons=reasons,
        promotion_gates=gates,
    )
    jsonl.append(paths.evolution_candidates, candidate.asdict(), stamp=False)
    record_event(
        paths,
        parent_id=source_event_id,
        outcome="candidate",
        strategy_id=strategy_id,
        summary=f"Evolution asset candidate: {summary}",
        evidence_refs=evidence,
        metadata={
            "candidate_id": candidate.id,
            "asset_kind": kind,
            "promotion_gates": gates,
        },
    )
    return candidate.asdict()


def promote_candidate(
    paths: WorkspacePaths,
    candidate_id: str,
    *,
    operator: str | None = None,
) -> dict[str, Any]:
    candidate = _find_candidate(paths, candidate_id)
    if candidate is None:
        return {"ok": False, "reason": "not_found", "candidate_id": candidate_id}
    gates = (
        candidate.get("promotion_gates")
        if isinstance(candidate.get("promotion_gates"), dict)
        else _candidate_promotion_gates(
            kind=str(candidate.get("kind") or ""),
            payload=dict(candidate.get("payload") or {}),
            evidence_refs=list(candidate.get("evidence_refs") or []),
            policy_reasons=list(candidate.get("blocked_reasons") or []),
        )
    )
    blocked_reasons = list(gates.get("blockers") or candidate.get("blocked_reasons") or [])
    if not candidate.get("safe_to_promote", False) or gates.get("can_promote") is False:
        return {
            "ok": False,
            "reason": "blocked",
            "candidate_id": candidate_id,
            "blocked_reasons": blocked_reasons,
            "promotion_gates": gates,
        }
    kind = str(candidate.get("kind") or "")
    payload = dict(candidate.get("payload") or {})
    if kind == "gene":
        genes = [g for g in list_genes(paths) if g.get("id") != payload.get("id")]
        genes.append(payload)
        _write_json(paths.evolution_genes, genes)
        promoted_ref = payload.get("id")
    elif kind == "capsule":
        capsule = {
            **payload,
            "id": payload.get("id") or new_id("cap"),
            "source_event_id": payload.get("source_event_id")
            or candidate.get("source_event_id"),
            "evidence_refs": payload.get("evidence_refs")
            or candidate.get("evidence_refs")
            or [],
            "strategy_id": payload.get("strategy_id")
            or candidate.get("strategy_id"),
            "ts": now_iso(),
        }
        jsonl.append(paths.evolution_capsules, capsule, stamp=False)
        promoted_ref = capsule["id"]
    else:
        return {"ok": False, "reason": f"unknown_kind:{kind}", "candidate_id": candidate_id}
    jsonl.append(
        paths.evolution_candidates,
        {
            **candidate,
            "state": "promoted",
            "decision": "promoted",
            "decided_at": now_iso(),
            "promoted_ref": promoted_ref,
            "operator": operator,
        },
        stamp=False,
    )
    record_event(
        paths,
        parent_id=candidate.get("source_event_id"),
        outcome="approved",
        outcome_score=1.0,
        strategy_id=candidate.get("strategy_id"),
        summary=f"Promoted evolution {kind} candidate {candidate_id}.",
        evidence_refs=list(candidate.get("evidence_refs") or []),
        metadata={
            "candidate_id": candidate_id,
            "promoted_ref": promoted_ref,
            "operator": operator,
        },
    )
    return {"ok": True, "candidate_id": candidate_id, "promoted_ref": promoted_ref}


def reject_candidate(
    paths: WorkspacePaths,
    candidate_id: str,
    *,
    reason: str = "",
    operator: str | None = None,
) -> dict[str, Any]:
    candidate = _find_candidate(paths, candidate_id)
    if candidate is None:
        return {"ok": False, "reason": "not_found", "candidate_id": candidate_id}
    rejected = {
        **candidate,
        "state": "rejected",
        "decision": "rejected",
        "decided_at": now_iso(),
        "rejected_reason": reason,
        "operator": operator,
    }
    jsonl.append(paths.evolution_candidates, rejected, stamp=False)
    jsonl.append(paths.evolution_rejected, rejected, stamp=False)
    record_event(
        paths,
        parent_id=candidate.get("source_event_id"),
        outcome="rejected",
        outcome_score=-0.5,
        strategy_id=candidate.get("strategy_id"),
        summary=f"Rejected evolution asset candidate {candidate_id}: {reason}",
        evidence_refs=list(candidate.get("evidence_refs") or []),
        metadata={"candidate_id": candidate_id, "operator": operator},
    )
    return {"ok": True, "candidate_id": candidate_id, "state": "rejected"}


def record_capsule_from_proposal(
    paths: WorkspacePaths,
    proposal_id: str,
    *,
    outcome_score: float = 1.0,
) -> dict[str, Any] | None:
    pdir = paths.proposals / proposal_id
    meta_path = pdir / "proposal.yml"
    if not meta_path.exists():
        return None
    meta = yaml_io.load(meta_path, default={}) or {}
    proposal_metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    capsule_metadata = _capsule_metadata_from_proposal(meta, proposal_metadata, proposal_id)
    selected_gene_ids = _str_list(capsule_metadata.get("selected_gene_ids"))
    capsule = EvolutionCapsule(
        id=new_id("cap"),
        gene_id=meta.get("gene_id") or (selected_gene_ids[0] if selected_gene_ids else None),
        source_event_id=meta.get("source_event_id"),
        summary=str(meta.get("summary") or proposal_id),
        evidence_refs=[str(x) for x in (meta.get("evidence_refs") or [])],
        validation_results=[
            {
                "validation_plan_id": meta.get("validation_plan_id"),
                "status": meta.get("validation_status") or "not_run",
            }
        ],
        outcome_score=outcome_score,
        promotion_ref=f"proposal:{proposal_id}",
        strategy_id=meta.get("strategy_id") or proposal_metadata.get("strategy_id"),
        metadata=capsule_metadata,
    )
    jsonl.append(paths.evolution_capsules, capsule.asdict(), stamp=False)
    return capsule.asdict()


def _capsule_metadata_from_proposal(
    meta: dict[str, Any],
    proposal_metadata: dict[str, Any],
    proposal_id: str,
) -> dict[str, Any]:
    trigger = (
        proposal_metadata.get("evolution_trigger_context")
        if isinstance(proposal_metadata.get("evolution_trigger_context"), dict)
        else {}
    )
    signal_kinds = _str_list(trigger.get("signal_kinds"))
    out: dict[str, Any] = {
        "proposal_kind": meta.get("kind"),
        "proposal_id": proposal_id,
    }
    if signal_kinds:
        out["trigger_signal_kinds"] = signal_kinds
    for source_key, target_key in (
        ("market_regimes", "trigger_market_regimes"),
        ("markets", "trigger_markets"),
        ("timeframes", "trigger_timeframes"),
        ("data_quality", "trigger_data_quality"),
        ("selected_gene_ids", "selected_gene_ids"),
        ("selected_capsule_ids", "selected_capsule_ids"),
        ("evidence_refs", "trigger_evidence_refs"),
    ):
        values = _str_list(trigger.get(source_key))
        if values:
            out[target_key] = values
    if proposal_metadata.get("package_hash"):
        out["package_hash"] = proposal_metadata.get("package_hash")
    return out


def _candidate_promotion_gates(
    *,
    kind: str,
    payload: dict[str, Any],
    evidence_refs: list[str],
    policy_reasons: list[str],
) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    origin = str(metadata.get("origin") or "").strip()
    preview_type = str(metadata.get("preview_type") or "").strip()
    preview_status = str(metadata.get("preview_status") or "").strip()
    blockers = _unique_strings([str(reason) for reason in policy_reasons if str(reason).strip()])
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    has_evidence = bool([ref for ref in evidence_refs if str(ref).strip()])
    if not has_evidence:
        blockers.append("missing_evidence_refs")
    checks.append({
        "id": "evidence_refs",
        "status": "passed" if has_evidence else "blocked",
        "summary": (
            f"{len(evidence_refs)} evidence ref(s) attached."
            if has_evidence else
            "Asset candidates need inspectable evidence refs before promotion."
        ),
    })

    checks.append({
        "id": "policy_scope",
        "status": "passed" if not policy_reasons else "blocked",
        "summary": (
            "Mutation scope and validation commands pass asset policy."
            if not policy_reasons else
            "Mutation scope or validation command policy blocked promotion."
        ),
        "reasons": list(policy_reasons),
    })

    if origin == "strategy_optimizer_preview":
        valid_preview = preview_type in {"static", "backtest"} and preview_status in {"passed", "failed"}
        if not valid_preview:
            blockers.append("invalid_preview_candidate")
        checks.append({
            "id": "preview_outcome",
            "status": "passed" if valid_preview else "blocked",
            "summary": (
                f"{preview_type or 'unknown'} preview outcome is {preview_status or 'unknown'}."
                if valid_preview else
                "Preview-generated candidates require a passed or failed static/backtest outcome."
            ),
        })
        if preview_status == "failed":
            warnings.append("promotes_as_negative_cautionary_capsule")
    else:
        checks.append({
            "id": "asset_payload",
            "status": "passed" if kind in {"gene", "capsule"} else "blocked",
            "summary": f"Candidate kind is {kind or 'unknown'}.",
        })
        if kind not in {"gene", "capsule"}:
            blockers.append(f"unknown_kind:{kind}")

    warnings.append("review_only_until_promoted")
    checks.append({
        "id": "runtime_selector",
        "status": "review_only",
        "summary": "Pending candidates are not used by Selector/GDI until explicit promotion.",
    })
    blockers = _unique_strings(blockers)
    warnings = _unique_strings(warnings)
    return {
        "version": "asset_candidate_promotion_gates_v1",
        "can_promote": not blockers,
        "review_only_until_promoted": True,
        "selector_eligible": False,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _find_candidate(paths: WorkspacePaths, candidate_id: str) -> dict[str, Any] | None:
    for row in reversed(jsonl.read_all(paths.evolution_candidates)):
        if str(row.get("id") or "") == candidate_id:
            return row
    return None


def _read_json(path, *, default):
    p = pathsafe(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path, data: Any) -> None:
    atomic_write_text(pathsafe(path), json.dumps(data, indent=2, ensure_ascii=False, default=str))


def pathsafe(path):
    from pathlib import Path

    return Path(path)


__all__ = [
    "DEFAULT_GENES",
    "EvolutionAssetCandidate",
    "EvolutionCapsule",
    "EvolutionGene",
    "create_candidate",
    "list_candidates",
    "list_capsules",
    "list_genes",
    "promote_candidate",
    "record_capsule_from_proposal",
    "reject_candidate",
    "search_assets",
]
