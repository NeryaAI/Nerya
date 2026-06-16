"""Append-only post-apply observation helpers for evolution proposals."""

from __future__ import annotations

from typing import Any

from ..core import jsonl
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .observation_summary import (
    POST_APPLY_HEALTHY_STATUSES,
    POST_APPLY_NEGATIVE_STATUSES,
    summarize_observation_weights,
)
from .patch_proposal import Proposal, list_proposals


_POST_APPLY_HEALTHY = set(POST_APPLY_HEALTHY_STATUSES)
_POST_APPLY_NEGATIVE = set(POST_APPLY_NEGATIVE_STATUSES)
_POST_APPLY_PENDING = {"pending", "observing"}
_ALLOWED_STATUSES = _POST_APPLY_HEALTHY | _POST_APPLY_NEGATIVE | _POST_APPLY_PENDING
_MAX_SUMMARY_CHARS = 2_000


def record_post_apply_observation(
    paths: WorkspacePaths,
    *,
    proposal_id: str,
    status: str | None = None,
    summary: str = "",
    source: str = "manual",
    observed_at: str | None = None,
    evidence_refs: Any = None,
    metrics: dict[str, Any] | None = None,
    backtest_result: dict[str, Any] | None = None,
    run_id: str | None = None,
    operator: str | None = None,
    metadata: dict[str, Any] | None = None,
    allow_unapplied: bool = False,
) -> dict[str, Any]:
    """Record a post-apply observation as append-only evidence.

    The helper is intentionally narrow: it does not apply, rollback, or mutate
    proposal files. It only appends a normalized
    ``proposal.post_apply_observation`` row to the evolution journal.
    """

    pid = str(proposal_id or "").strip()
    if not pid:
        return {"ok": False, "reason": "proposal_id_required"}
    proposal = _find_proposal(paths, pid)
    if proposal is None:
        return {"ok": False, "reason": "proposal_not_found", "proposal_id": pid}
    if str(proposal.state or "").lower() != "applied" and not allow_unapplied:
        return {
            "ok": False,
            "reason": "proposal_not_applied",
            "proposal_id": pid,
            "state": proposal.state,
        }
    if metrics is not None and not isinstance(metrics, dict):
        return {"ok": False, "reason": "metrics_must_be_object", "proposal_id": pid}
    if backtest_result is not None and not isinstance(backtest_result, dict):
        return {"ok": False, "reason": "backtest_result_must_be_object", "proposal_id": pid}

    refs = _str_list(evidence_refs)
    if not refs and not metrics and not backtest_result:
        return {"ok": False, "reason": "evidence_required", "proposal_id": pid}

    normalized_status = _normalize_status(status) or _derive_status(
        metrics=metrics or {},
        backtest_result=backtest_result or {},
    )
    if normalized_status not in _ALLOWED_STATUSES:
        return {
            "ok": False,
            "reason": "invalid_status",
            "proposal_id": pid,
            "status": normalized_status,
            "allowed_statuses": sorted(_ALLOWED_STATUSES),
        }

    observation_id = new_id("obs")
    row: dict[str, Any] = {
        "id": observation_id,
        "kind": "proposal.post_apply_observation",
        "proposal_id": pid,
        "proposal_kind": proposal.kind,
        "strategy_id": _proposal_strategy(proposal),
        "status": normalized_status,
        "summary": _bounded_summary(
            summary,
            status=normalized_status,
            source=source,
        ),
        "source": _clean_source(source),
        "observed_at": observed_at or now_iso(),
        "evidence_refs": refs,
    }
    if metrics:
        row["metrics"] = metrics
    if backtest_result:
        row["backtest_result"] = backtest_result
    if run_id:
        row["run_id"] = str(run_id)
    if operator:
        row["operator"] = str(operator)
    if metadata:
        row["metadata"] = dict(metadata)

    journal_path = paths.journal("evolution")
    line_index = len(jsonl.read_all(journal_path))
    jsonl.append(journal_path, row)
    journal_ref = f"journal:evolution:{line_index}"
    evidence = _unique_strings([*refs, journal_ref])
    return {
        "ok": True,
        "observation": {**row, "evidence_refs": evidence, "journal_ref": journal_ref},
        "proposal_id": pid,
        "status": normalized_status,
        "journal_ref": journal_ref,
        "evidence_refs": evidence,
        "next_step": _next_step(normalized_status),
    }


def post_apply_observations_by_proposal(
    paths: WorkspacePaths,
    *,
    proposal_id: str | None = None,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    target = str(proposal_id or "").strip()
    for index, row in enumerate(jsonl.read_all(paths.journal("evolution"))):
        if row.get("kind") != "proposal.post_apply_observation":
            continue
        pid = str(row.get("proposal_id") or "")
        if not pid or (target and pid != target):
            continue
        evidence_refs = _unique_strings([
            *_str_list(row.get("evidence_refs")),
            f"journal:evolution:{index}",
        ])
        rows.setdefault(pid, []).append({
            **row,
            "evidence_refs": evidence_refs,
            "journal_ref": f"journal:evolution:{index}",
        })
    for pid, observations in rows.items():
        observations.sort(key=lambda item: str(item.get("observed_at") or item.get("ts") or ""))
        rows[pid] = observations[-max(1, int(limit)) :]
    return rows


def post_apply_monitor(
    proposal: dict[str, Any] | Proposal,
    observations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    state = _proposal_field(proposal, "state")
    if str(state or "").lower() != "applied":
        return None
    if not observations:
        return {
            "status": "pending",
            "summary": "Applied change needs post-apply observation.",
            "evidence_refs": [],
            "observations": [],
        }
    latest = observations[-1]
    status = str(latest.get("status") or latest.get("outcome") or "observing").lower()
    weighted_summary = {
        "status": status,
        "observed_at": latest.get("observed_at") or latest.get("ts"),
        "count": len(observations),
        **summarize_observation_weights(observations),
    }
    return {
        "status": status,
        "summary": str(latest.get("summary") or latest.get("note") or status),
        "observed_at": latest.get("observed_at") or latest.get("ts"),
        "evidence_refs": _unique_strings([
            *_str_list(latest.get("evidence_refs")),
            *[
                ref
                for obs in observations[-5:]
                for ref in _str_list(obs.get("evidence_refs"))
            ],
        ]),
        "latest": latest,
        "observations": observations[-5:],
        "weighted_summary": weighted_summary,
    }


def _find_proposal(paths: WorkspacePaths, proposal_id: str) -> Proposal | None:
    for proposal in list_proposals(paths):
        if proposal.id == proposal_id:
            return proposal
    return None


def _proposal_strategy(proposal: Proposal) -> str | None:
    meta = proposal.metadata or {}
    direct = meta.get("strategy_id")
    if direct:
        return str(direct)
    target = str(proposal.target or "")
    parts = target.replace("\\", "/").split("/")
    if "strategies" in parts:
        idx = parts.index("strategies")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _proposal_field(proposal: dict[str, Any] | Proposal, key: str) -> Any:
    if isinstance(proposal, dict):
        return proposal.get(key)
    return getattr(proposal, key, None)


def _normalize_status(status: str | None) -> str:
    return str(status or "").strip().lower().replace(" ", "_").replace("-", "_")


def _derive_status(
    *,
    metrics: dict[str, Any],
    backtest_result: dict[str, Any],
) -> str:
    backtest_metrics = backtest_result.get("metrics")
    if not isinstance(backtest_metrics, dict):
        backtest_metrics = {}
    verdict = str(
        backtest_result.get("verdict")
        or backtest_metrics.get("verdict")
        or metrics.get("verdict")
        or ""
    ).upper()
    ok_value = backtest_result.get("ok")
    coverage_ok = backtest_result.get("coverage_ok")
    if ok_value is False or coverage_ok is False or verdict == "FAIL":
        return "failed"
    if ok_value is True or verdict in {"PASS", "PASSED", "OK", "WARN"}:
        return "healthy"
    return "observing"


def _bounded_summary(summary: str, *, status: str, source: str) -> str:
    text = str(summary or "").strip()
    if not text:
        text = f"Post-apply {source or 'manual'} observation recorded as {status}."
    if len(text) > _MAX_SUMMARY_CHARS:
        return text[:_MAX_SUMMARY_CHARS] + "...[truncated]"
    return text


def _clean_source(source: str) -> str:
    text = str(source or "manual").strip().lower().replace(" ", "_")
    return text[:80] or "manual"


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


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


def _next_step(status: str) -> str:
    if status in _POST_APPLY_NEGATIVE:
        return "review_rollback_or_negative_capsule"
    if status in _POST_APPLY_HEALTHY:
        return "promote_or_reuse_learning"
    return "continue_post_apply_monitoring"


__all__ = [
    "post_apply_monitor",
    "post_apply_observations_by_proposal",
    "record_post_apply_observation",
]
