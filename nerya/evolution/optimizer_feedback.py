"""Summaries for optimizer outcome-feedback learning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from .patch_proposal import list_proposals


def optimizer_feedback_summary(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    """Aggregate strategy optimizer feedback from prior tuning reports."""

    proposals = {proposal.id: proposal for proposal in list_proposals(paths)}
    rows = [
        row for row in jsonl.read_all(paths.journal("strategy_evolution"))
        if row.get("kind") == "strategy.tuning"
        and row.get("proposal_id")
        and (not strategy_id or str(row.get("strategy_id") or "") == strategy_id)
    ][-max(1, int(limit)) :]
    feature_scores: dict[str, dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    sample_count = positive_samples = negative_samples = neutral_samples = 0
    proposal_samples = 0
    candidate_decision_samples = 0
    candidate_decision_positive_samples = 0
    candidate_decision_negative_samples = 0
    candidate_decision_neutral_samples = 0
    evidence_refs: list[str] = []
    for row in rows:
        proposal_id = str(row.get("proposal_id") or "")
        proposal = proposals.get(proposal_id)
        if proposal is None:
            continue
        report = _read_optimizer_report(proposal.path)
        if not report:
            continue
        feedback = report.get("outcome_feedback")
        if not isinstance(feedback, dict) or not feedback:
            continue
        sample_count += int(feedback.get("sample_count") or 0)
        positive_samples += int(feedback.get("positive_samples") or 0)
        negative_samples += int(feedback.get("negative_samples") or 0)
        neutral_samples += int(feedback.get("neutral_samples") or 0)
        proposal_samples += int(feedback.get("proposal_samples") or 0)
        candidate_decision_samples += int(feedback.get("candidate_decision_samples") or 0)
        candidate_decision_positive_samples += int(feedback.get("candidate_decision_positive_samples") or 0)
        candidate_decision_negative_samples += int(feedback.get("candidate_decision_negative_samples") or 0)
        candidate_decision_neutral_samples += int(feedback.get("candidate_decision_neutral_samples") or 0)
        for feature in _feature_rows(feedback):
            name = str(feature.get("feature") or "").strip()
            if not name:
                continue
            target = feature_scores.setdefault(
                name,
                {
                    "feature": name,
                    "positive": 0.0,
                    "negative": 0.0,
                    "net": 0.0,
                    "samples": 0,
                },
            )
            target["positive"] = round(
                float(target.get("positive") or 0.0)
                + _float(feature.get("positive")),
                4,
            )
            target["negative"] = round(
                float(target.get("negative") or 0.0)
                + _float(feature.get("negative")),
                4,
            )
            target["net"] = round(
                float(target.get("positive") or 0.0)
                - float(target.get("negative") or 0.0),
                4,
            )
            target["samples"] = int(target.get("samples") or 0) + int(feature.get("samples") or 0)
            _merge_source_counts(target, feature, key="sources")
            _merge_source_weights(target, feature, key="positive_by_source")
            _merge_source_weights(target, feature, key="negative_by_source")
        selected = _selected_candidate(report)
        run_id = str(row.get("run_id") or "")
        evidence_refs.extend([
            f"proposal:{proposal_id}",
            f"strategy_tuning:{run_id}" if run_id else "",
        ])
        examples.append({
            "proposal_id": proposal_id,
            "run_id": run_id,
            "strategy_id": row.get("strategy_id"),
            "state": proposal.state,
            "selected_candidate_id": report.get("selected_candidate_id"),
            "selected_score": report.get("selected_score"),
            "candidate_status": selected.get("status") if selected else None,
            "feedback_sample_count": feedback.get("sample_count"),
        })
    features = list(feature_scores.values())
    positive = sorted(
        [row for row in features if _float(row.get("net")) > 0],
        key=lambda row: _float(row.get("net")),
        reverse=True,
    )
    negative = sorted(
        [row for row in features if _float(row.get("net")) < 0],
        key=lambda row: _float(row.get("net")),
    )
    candidate_decisions = candidate_decision_summary(
        paths,
        strategy_id=strategy_id,
        limit=limit,
    )
    calibration = _optimizer_feedback_calibration(
        run_count=len(examples),
        sample_count=sample_count,
        positive_samples=positive_samples,
        negative_samples=negative_samples,
        neutral_samples=neutral_samples,
        proposal_samples=proposal_samples,
        candidate_decision_samples=candidate_decision_samples,
        candidate_decision_positive_samples=candidate_decision_positive_samples,
        candidate_decision_negative_samples=candidate_decision_negative_samples,
        candidate_decision_neutral_samples=candidate_decision_neutral_samples,
        features=features,
        candidate_decisions=candidate_decisions,
    )
    return {
        "version": "optimizer_feedback_summary_v1",
        "strategy_id": strategy_id,
        "run_count": len(examples),
        "sample_count": sample_count,
        "positive_samples": positive_samples,
        "negative_samples": negative_samples,
        "neutral_samples": neutral_samples,
        "proposal_samples": proposal_samples,
        "candidate_decision_samples": candidate_decision_samples,
        "candidate_decision_positive_samples": candidate_decision_positive_samples,
        "candidate_decision_negative_samples": candidate_decision_negative_samples,
        "candidate_decision_neutral_samples": candidate_decision_neutral_samples,
        "top_positive_features": positive[:8],
        "top_negative_features": negative[:8],
        "recent_examples": examples[-8:],
        "candidate_decisions": candidate_decisions,
        "calibration": calibration,
        "evidence_refs": _unique_strings(evidence_refs)[-16:],
    }


def _merge_source_counts(
    target: dict[str, Any],
    feature: dict[str, Any],
    *,
    key: str,
) -> None:
    source_counts = feature.get(key) if isinstance(feature.get(key), dict) else {}
    if not source_counts:
        return
    merged = target.get(key) if isinstance(target.get(key), dict) else {}
    for source, count in source_counts.items():
        name = str(source or "").strip()
        if not name:
            continue
        merged[name] = int(merged.get(name) or 0) + int(count or 0)
    target[key] = merged


def _merge_source_weights(
    target: dict[str, Any],
    feature: dict[str, Any],
    *,
    key: str,
) -> None:
    source_weights = feature.get(key) if isinstance(feature.get(key), dict) else {}
    if not source_weights:
        return
    merged = target.get(key) if isinstance(target.get(key), dict) else {}
    for source, weight in source_weights.items():
        name = str(source or "").strip()
        if not name:
            continue
        merged[name] = round(float(merged.get(name) or 0.0) + _float(weight), 4)
    target[key] = merged


def _optimizer_feedback_calibration(
    *,
    run_count: int,
    sample_count: int,
    positive_samples: int,
    negative_samples: int,
    neutral_samples: int,
    proposal_samples: int,
    candidate_decision_samples: int,
    candidate_decision_positive_samples: int,
    candidate_decision_negative_samples: int,
    candidate_decision_neutral_samples: int,
    features: list[dict[str, Any]],
    candidate_decisions: dict[str, Any],
) -> dict[str, Any]:
    warnings: list[str] = []
    total = max(0, int(sample_count))
    decision_ratio = _ratio(candidate_decision_samples, total)
    proposal_ratio = _ratio(proposal_samples, total)
    positive_ratio = _ratio(positive_samples, total)
    negative_ratio = _ratio(negative_samples, total)
    neutral_ratio = _ratio(neutral_samples, total)
    total_abs_net = sum(abs(_float(row.get("net"))) for row in features)
    top_abs_net = max([abs(_float(row.get("net"))) for row in features] or [0.0])
    concentration = round((top_abs_net / total_abs_net), 4) if total_abs_net > 0 else 0.0
    promoted = int(candidate_decisions.get("promoted") or 0)
    rejected = int(candidate_decisions.get("rejected") or 0)
    decision_total = int(candidate_decisions.get("total") or 0)
    promoted_ratio = _ratio(promoted, decision_total)
    rejected_ratio = _ratio(rejected, decision_total)

    if run_count < 2:
        warnings.append("single_run_feedback")
    if total < 4:
        warnings.append("low_sample_count")
    if candidate_decision_samples and decision_ratio >= 0.7:
        warnings.append("operator_decision_dominant")
    if concentration >= 0.75 and len(features) >= 2:
        warnings.append("feature_concentration_high")
    if negative_ratio >= 0.7 and total >= 4:
        warnings.append("negative_feedback_dominant")
    if positive_ratio >= 0.9 and total >= 4:
        warnings.append("positive_feedback_unbalanced")
    if candidate_decision_samples and proposal_samples == 0:
        warnings.append("no_proposal_outcome_samples")

    if total == 0:
        confidence = "none"
        status = "no_feedback"
    elif "low_sample_count" in warnings or "single_run_feedback" in warnings:
        confidence = "low"
        status = "needs_more_evidence"
    elif "operator_decision_dominant" in warnings or "feature_concentration_high" in warnings:
        confidence = "medium"
        status = "watch"
    else:
        confidence = "high"
        status = "calibrated"

    return {
        "version": "optimizer_feedback_calibration_v1",
        "status": status,
        "confidence": confidence,
        "warnings": warnings,
        "run_count": run_count,
        "sample_count": total,
        "source_mix": {
            "proposal_samples": proposal_samples,
            "candidate_decision_samples": candidate_decision_samples,
            "proposal_ratio": proposal_ratio,
            "candidate_decision_ratio": decision_ratio,
        },
        "polarity_mix": {
            "positive_samples": positive_samples,
            "negative_samples": negative_samples,
            "neutral_samples": neutral_samples,
            "positive_ratio": positive_ratio,
            "negative_ratio": negative_ratio,
            "neutral_ratio": neutral_ratio,
        },
        "candidate_decision_mix": {
            "total": decision_total,
            "promoted": promoted,
            "rejected": rejected,
            "promoted_ratio": promoted_ratio,
            "rejected_ratio": rejected_ratio,
            "positive_samples": candidate_decision_positive_samples,
            "negative_samples": candidate_decision_negative_samples,
            "neutral_samples": candidate_decision_neutral_samples,
        },
        "feature_concentration": {
            "top_abs_net": round(top_abs_net, 4),
            "total_abs_net": round(total_abs_net, 4),
            "top_feature_ratio": concentration,
            "feature_count": len(features),
        },
    }


def _ratio(numerator: int | float, denominator: int | float) -> float:
    denom = float(denominator or 0)
    if denom <= 0:
        return 0.0
    return round(float(numerator or 0) / denom, 4)


def candidate_decision_summary(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    """Summarize terminal asset-candidate decisions created by optimizer previews."""

    latest = _latest_candidates(paths, limit=limit * 4)
    rows: list[dict[str, Any]] = []
    for row in latest.values():
        state = str(row.get("state") or "candidate")
        if state not in {"promoted", "rejected"}:
            continue
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if metadata.get("origin") != "strategy_optimizer_preview":
            continue
        outcome_score = _float(payload.get("outcome_score"))
        evidence_refs = _unique_strings([
            *[str(ref) for ref in row.get("evidence_refs") or []],
            f"strategy_tuning:{metadata.get('optimizer_run_id')}" if metadata.get("optimizer_run_id") else "",
        ])
        rows.append({
            "candidate_id": row.get("id"),
            "asset_kind": row.get("kind"),
            "state": state,
            "decision": row.get("decision") or state,
            "operator": row.get("operator"),
            "decided_at": row.get("decided_at") or row.get("ts"),
            "strategy_id": row.get("strategy_id"),
            "summary": row.get("summary"),
            "promoted_ref": row.get("promoted_ref"),
            "rejected_reason": row.get("rejected_reason"),
            "optimizer_run_id": metadata.get("optimizer_run_id"),
            "optimizer_candidate_id": metadata.get("optimizer_candidate_id"),
            "preview_type": metadata.get("preview_type"),
            "preview_status": metadata.get("preview_status"),
            "selected_by_optimizer": bool(metadata.get("selected_by_optimizer")),
            "outcome_score": outcome_score,
            "evidence_refs": evidence_refs[:8],
        })
    rows.sort(key=lambda row: str(row.get("decided_at") or ""), reverse=True)
    promoted = sum(1 for row in rows if row.get("state") == "promoted")
    rejected = sum(1 for row in rows if row.get("state") == "rejected")
    return {
        "version": "optimizer_candidate_decisions_v1",
        "total": len(rows),
        "promoted": promoted,
        "rejected": rejected,
        "recent": rows[:8],
        "evidence_refs": _unique_strings([
            ref
            for row in rows[:8]
            for ref in (row.get("evidence_refs") or [])
        ])[:16],
    }


def enrich_optimizer_report_with_candidate_decisions(
    paths: WorkspacePaths,
    report: dict[str, Any],
    *,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    """Attach latest asset-candidate decision state to optimizer report rows."""

    if not isinstance(report, dict) or not report:
        return report
    by_asset_id, by_optimizer_key = _candidate_decision_indexes(paths, strategy_id=strategy_id)
    candidates: list[dict[str, Any]] = []
    for row in report.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        candidate = dict(row)
        asset_candidate = (
            dict(candidate.get("asset_candidate"))
            if isinstance(candidate.get("asset_candidate"), dict)
            else {}
        )
        decision = None
        asset_id = str(asset_candidate.get("id") or "")
        if asset_id:
            decision = by_asset_id.get(asset_id)
        if decision is None:
            key = (
                str(report.get("run_id") or asset_candidate.get("optimizer_run_id") or ""),
                str(candidate.get("candidate_id") or ""),
            )
            decision = by_optimizer_key.get(key)
        if decision:
            asset_candidate.update({
                "id": decision.get("candidate_id") or asset_candidate.get("id"),
                "state": decision.get("state"),
                "decision": decision.get("decision"),
                "decided_at": decision.get("decided_at"),
                "operator": decision.get("operator"),
                "promoted_ref": decision.get("promoted_ref"),
                "rejected_reason": decision.get("rejected_reason"),
                "evidence_refs": _unique_strings([
                    *[str(ref) for ref in asset_candidate.get("evidence_refs") or []],
                    *[str(ref) for ref in decision.get("evidence_refs") or []],
                ])[:8],
            })
            candidate["asset_candidate"] = asset_candidate
            candidate["asset_candidate_decision"] = decision
        candidates.append(candidate)
    return {**report, "candidates": candidates}


def _read_optimizer_report(proposal_path: Path) -> dict[str, Any] | None:
    path = proposal_path / "tuning_run.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    report = data.get("optimizer_report") if isinstance(data, dict) else None
    return report if isinstance(report, dict) and report else None


def _latest_candidates(paths: WorkspacePaths, *, limit: int = 320) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    rows = jsonl.read_all(paths.evolution_candidates)
    for row in rows[-max(1, int(limit)) :]:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "")
        if cid:
            latest[cid] = row
    return latest


def _candidate_decision_indexes(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    summary = candidate_decision_summary(paths, strategy_id=strategy_id, limit=160)
    by_asset_id: dict[str, dict[str, Any]] = {}
    by_optimizer_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in summary.get("recent") or []:
        if not isinstance(row, dict):
            continue
        asset_id = str(row.get("candidate_id") or "")
        run_id = str(row.get("optimizer_run_id") or "")
        optimizer_candidate_id = str(row.get("optimizer_candidate_id") or "")
        if asset_id:
            by_asset_id[asset_id] = row
        if run_id and optimizer_candidate_id:
            by_optimizer_key[(run_id, optimizer_candidate_id)] = row
    return by_asset_id, by_optimizer_key


def _feature_rows(feedback: dict[str, Any]) -> list[dict[str, Any]]:
    top = feedback.get("top_features")
    if isinstance(top, list):
        return [row for row in top if isinstance(row, dict)]
    return []


def _selected_candidate(report: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [row for row in report.get("candidates", []) if isinstance(row, dict)]
    selected_id = str(report.get("selected_candidate_id") or "")
    selected_index = _maybe_int(report.get("selected_index"))
    for index, candidate in enumerate(candidates):
        if selected_id and str(candidate.get("candidate_id") or "") == selected_id:
            return candidate
        if selected_index is not None and index == selected_index:
            return candidate
    return candidates[0] if candidates else None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


__all__ = [
    "candidate_decision_summary",
    "enrich_optimizer_report_with_candidate_decisions",
    "optimizer_feedback_summary",
]
