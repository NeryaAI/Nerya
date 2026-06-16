"""Evidence-backed fitness breakdown for evolution proposals.

The vector is deliberately a breakdown, not a single magic score. It reduces
existing validation, backtest, human, and post-apply evidence into dimensions
that the UI and future GDI selector can explain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.paths import WorkspacePaths


_PASSED_VALIDATION = {"passed", "safe", "ready", "ok"}
_FAILED_VALIDATION = {"failed", "blocked"}
_NEGATIVE_PROPOSAL_STATES = {"rejected", "rolled_back"}
_POSITIVE_PROPOSAL_STATES = {"approved", "applied"}
_POST_APPLY_HEALTHY = {"healthy", "passed", "ok", "stable", "improved"}
_POST_APPLY_NEGATIVE = {"regressed", "failed", "degraded", "rollback_recommended"}
_MATERIALIZED_KINDS = {
    "strategy_tuning_proposal",
    "strategy_package_proposal",
    "strategy_config_patch",
    "script_proposal",
    "skill_proposal",
    "trigger_route_patch",
}
_CRITICAL_REGRESSION_METRICS = {
    "total_return_pct",
    "alpha_vs_benchmark_pct",
    "max_drawdown_pct",
    "total_slippage_usd",
    "total_fees_usd",
}


def proposal_fitness_vector(
    paths: WorkspacePaths,
    proposal: dict[str, Any],
    *,
    validation_plan: dict[str, Any] | None = None,
    backtest_comparison: dict[str, Any] | None = None,
    post_apply_monitor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact operator-facing fitness vector for a proposal."""

    validation_plan = validation_plan or _load_validation_plan(paths, proposal)
    dimensions = [
        _validation_dimension(validation_plan),
        _performance_dimension(backtest_comparison),
        _safety_dimension(proposal),
        _human_preference_dimension(proposal),
        _post_apply_dimension(proposal, post_apply_monitor),
    ]
    dimensions.append(_confidence_dimension(proposal, dimensions))
    blockers = _unique_strings([
        reason
        for dim in dimensions
        for reason in dim.get("blockers", [])
    ])
    warnings = _unique_strings([
        reason
        for dim in dimensions
        for reason in dim.get("warnings", [])
    ])
    evidence_refs = _unique_strings([
        ref
        for dim in dimensions
        for ref in dim.get("evidence_refs", [])
    ])
    status = _overall_status(dimensions, blockers)
    return {
        "version": "fitness_vector_v0",
        "status": status,
        "summary": _summary(status, blockers, warnings, dimensions),
        "dimensions": dimensions,
        "blockers": blockers,
        "warnings": warnings,
        "evidence_refs": evidence_refs,
        "ready_for_approval": (
            not blockers
            and _dimension_status(dimensions, "validation") == "passed"
            and _dimension_status(dimensions, "safety") == "passed"
            and str(proposal.get("state") or "").lower() not in _NEGATIVE_PROPOSAL_STATES
        ),
    }


def _validation_dimension(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not plan:
        return _dimension(
            "validation",
            "Validation",
            "missing",
            blockers=["missing_validation_plan"],
            summary="No validation plan is attached.",
        )
    status = str(plan.get("status") or "not_run").lower()
    refs = _validation_refs(plan)
    if status in _PASSED_VALIDATION:
        return _dimension(
            "validation",
            "Validation",
            "passed",
            score=1.0,
            evidence_refs=refs,
            summary="Required validation has passed.",
            details={"plan_id": plan.get("id"), "status": status},
        )
    if status in _FAILED_VALIDATION:
        return _dimension(
            "validation",
            "Validation",
            "failed",
            score=-1.0,
            blockers=[f"validation_status:{status}", *_str_list(plan.get("blocked_reasons"))],
            evidence_refs=refs,
            summary="Validation is blocked or failed.",
            details={"plan_id": plan.get("id"), "status": status},
        )
    return _dimension(
        "validation",
        "Validation",
        "pending",
        warnings=[f"validation_status:{status}"],
        evidence_refs=refs,
        summary="Validation exists but has not fully passed.",
        details={"plan_id": plan.get("id"), "status": status},
    )


def _performance_dimension(comparison: dict[str, Any] | None) -> dict[str, Any]:
    if not comparison:
        return _dimension(
            "performance_delta",
            "Performance Delta",
            "unknown",
            warnings=["backtest_comparison_missing"],
            summary="No before/after performance comparison is available.",
        )
    status = str(comparison.get("status") or "unknown")
    refs = _str_list(comparison.get("evidence_refs"))
    if status != "complete":
        return _dimension(
            "performance_delta",
            "Performance Delta",
            "pending",
            warnings=[f"backtest_comparison:{status}"],
            evidence_refs=refs,
            summary=str(comparison.get("summary") or "Backtest comparison is incomplete."),
            details={"comparison_status": status},
        )
    after_metrics = (comparison.get("after") or {}).get("metrics") or {}
    verdict = str(after_metrics.get("verdict") or "").upper()
    deltas = [
        row for row in comparison.get("metrics_delta") or []
        if isinstance(row, dict)
    ]
    regressed = [row for row in deltas if row.get("direction") == "regressed"]
    critical = [
        str(row.get("key"))
        for row in regressed
        if str(row.get("key")) in _CRITICAL_REGRESSION_METRICS
    ]
    if verdict == "FAIL":
        return _dimension(
            "performance_delta",
            "Performance Delta",
            "failed",
            score=-1.0,
            blockers=["backtest_verdict_fail", *[f"regressed:{key}" for key in critical]],
            evidence_refs=refs,
            summary=str(comparison.get("summary") or "Backtest verdict failed."),
            details={"verdict": verdict, "regressed_metrics": [r.get("key") for r in regressed]},
        )
    if critical:
        return _dimension(
            "performance_delta",
            "Performance Delta",
            "warning",
            score=0.0,
            warnings=[f"regressed:{key}" for key in critical],
            evidence_refs=refs,
            summary=str(comparison.get("summary") or "Critical performance metric regressed."),
            details={"verdict": verdict, "regressed_metrics": [r.get("key") for r in regressed]},
        )
    return _dimension(
        "performance_delta",
        "Performance Delta",
        "passed",
        score=1.0,
        evidence_refs=refs,
        summary=str(comparison.get("summary") or "Backtest comparison did not show critical regression."),
        details={"verdict": verdict, "regressed_metrics": [r.get("key") for r in regressed]},
    )


def _safety_dimension(proposal: dict[str, Any]) -> dict[str, Any]:
    metadata = proposal.get("metadata") if isinstance(proposal.get("metadata"), dict) else {}
    kind = str(proposal.get("kind") or "")
    if metadata.get("advisory_only"):
        return _dimension(
            "safety",
            "Safety",
            "warning",
            warnings=["advisory_only_no_mutation"],
            evidence_refs=_proposal_refs(proposal),
            summary="Proposal is advisory-only and should not be treated as an applied mutation.",
        )
    if kind in _MATERIALIZED_KINDS and metadata.get("materialized") is False:
        return _dimension(
            "safety",
            "Safety",
            "failed",
            score=-1.0,
            blockers=["mutation_not_materialized"],
            evidence_refs=_proposal_refs(proposal),
            summary="Mutation proposal has no materialized file changes.",
        )
    return _dimension(
        "safety",
        "Safety",
        "passed",
        score=1.0,
        evidence_refs=_proposal_refs(proposal),
        summary="No protected-scope or materialization blocker is recorded.",
    )


def _human_preference_dimension(proposal: dict[str, Any]) -> dict[str, Any]:
    state = str(proposal.get("state") or "draft").lower()
    refs = _proposal_refs(proposal)
    if state in _NEGATIVE_PROPOSAL_STATES:
        return _dimension(
            "human_preference",
            "Human Preference",
            "failed",
            score=-1.0,
            blockers=[f"proposal_state:{state}"],
            evidence_refs=refs,
            summary="Operator outcome is negative.",
            details={"state": state},
        )
    if state in _POSITIVE_PROPOSAL_STATES:
        return _dimension(
            "human_preference",
            "Human Preference",
            "passed",
            score=1.0,
            evidence_refs=refs,
            summary="Operator has approved or applied this proposal.",
            details={"state": state},
        )
    return _dimension(
        "human_preference",
        "Human Preference",
        "pending",
        warnings=[f"proposal_state:{state}"],
        evidence_refs=refs,
        summary="Proposal is still waiting for operator decision.",
        details={"state": state},
    )


def _post_apply_dimension(
    proposal: dict[str, Any],
    monitor: dict[str, Any] | None,
) -> dict[str, Any]:
    state = str(proposal.get("state") or "").lower()
    if state != "applied":
        return _dimension(
            "post_apply",
            "Post-Apply",
            "not_applicable",
            summary="Proposal has not been applied yet.",
        )
    status = str((monitor or {}).get("status") or "pending").lower()
    refs = _str_list((monitor or {}).get("evidence_refs"))
    if status in _POST_APPLY_HEALTHY:
        return _dimension(
            "post_apply",
            "Post-Apply",
            "passed",
            score=1.0,
            evidence_refs=refs,
            summary=str((monitor or {}).get("summary") or "Post-apply observation is healthy."),
            details={"status": status},
        )
    if status in _POST_APPLY_NEGATIVE:
        return _dimension(
            "post_apply",
            "Post-Apply",
            "failed",
            score=-1.0,
            blockers=[f"post_apply:{status}"],
            evidence_refs=refs,
            summary=str((monitor or {}).get("summary") or "Post-apply observation is negative."),
            details={"status": status},
        )
    return _dimension(
        "post_apply",
        "Post-Apply",
        "pending",
        warnings=["post_apply_observation_pending"],
        evidence_refs=refs,
        summary="Applied proposal is still waiting for post-apply evidence.",
        details={"status": status},
    )


def _confidence_dimension(
    proposal: dict[str, Any],
    dimensions: list[dict[str, Any]],
) -> dict[str, Any]:
    refs = _unique_strings([
        *_str_list(proposal.get("evidence_refs")),
        *[
            ref
            for dim in dimensions
            for ref in _str_list(dim.get("evidence_refs"))
        ],
    ])
    passed = sum(1 for dim in dimensions if dim.get("status") == "passed")
    failed = sum(1 for dim in dimensions if dim.get("status") == "failed")
    if failed:
        status = "failed"
        score = -1.0
        summary = "Confidence is low because at least one fitness dimension failed."
        blockers: list[str] = ["failed_dimension_present"]
        warnings: list[str] = []
    elif len(refs) >= 3 and passed >= 2:
        status = "passed"
        score = 1.0
        summary = "Confidence is supported by multiple evidence refs and passed dimensions."
        blockers = []
        warnings = []
    else:
        status = "warning"
        score = 0.0
        summary = "Confidence is limited by sparse evidence or pending dimensions."
        blockers = []
        warnings = ["low_evidence_or_pending_dimensions"]
    return _dimension(
        "confidence",
        "Confidence",
        status,
        score=score,
        blockers=blockers,
        warnings=warnings,
        evidence_refs=refs,
        summary=summary,
        details={"evidence_ref_count": len(refs), "passed_dimensions": passed, "failed_dimensions": failed},
    )


def _overall_status(dimensions: list[dict[str, Any]], blockers: list[str]) -> str:
    statuses = {str(dim.get("status") or "") for dim in dimensions}
    if blockers or "failed" in statuses:
        return "failed"
    if "pending" in statuses or "warning" in statuses or "missing" in statuses:
        return "warning"
    if "passed" in statuses:
        return "passed"
    return "unknown"


def _summary(
    status: str,
    blockers: list[str],
    warnings: list[str],
    dimensions: list[dict[str, Any]],
) -> str:
    passed = sum(1 for dim in dimensions if dim.get("status") == "passed")
    failed = sum(1 for dim in dimensions if dim.get("status") == "failed")
    pending = sum(1 for dim in dimensions if dim.get("status") in {"pending", "warning", "missing"})
    if status == "failed":
        head = f"Fitness blocked by {len(blockers)} blocker(s)."
    elif status == "passed":
        head = "Fitness evidence is currently clean."
    else:
        head = f"Fitness needs more evidence; {len(warnings)} warning(s) remain."
    return f"{head} Dimensions: {passed} passed, {failed} failed, {pending} pending/warning."


def _dimension(
    did: str,
    label: str,
    status: str,
    *,
    score: float | None = None,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    summary: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "id": did,
        "label": label,
        "status": status,
        "summary": summary,
        "blockers": _unique_strings(blockers or []),
        "warnings": _unique_strings(warnings or []),
        "evidence_refs": _unique_strings(evidence_refs or []),
        "details": details or {},
    }
    if score is not None:
        out["score"] = score
    return out


def _load_validation_plan(paths: WorkspacePaths, proposal: dict[str, Any]) -> dict[str, Any] | None:
    plan_id = str(proposal.get("validation_plan_id") or "").strip()
    if not plan_id:
        return None
    path = paths.evolution_validation_plans / f"{plan_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _validation_refs(plan: dict[str, Any]) -> list[str]:
    refs = [f"validation:{plan.get('id')}"] if plan.get("id") else []
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and step.get("evidence_ref"):
            refs.append(str(step["evidence_ref"]))
    return _unique_strings(refs)


def _proposal_refs(proposal: dict[str, Any]) -> list[str]:
    refs = _str_list(proposal.get("evidence_refs"))
    if proposal.get("id"):
        refs.append(f"proposal:{proposal.get('id')}")
    return _unique_strings(refs)


def _dimension_status(dimensions: list[dict[str, Any]], did: str) -> str:
    for dim in dimensions:
        if dim.get("id") == did:
            return str(dim.get("status") or "")
    return ""


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value if x is not None and str(x)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


__all__ = ["proposal_fitness_vector"]
