"""Convert strategy tick records into post-apply evolution observations."""

from __future__ import annotations

from typing import Any

from ..core.paths import WorkspacePaths
from ..strategies.state import StrategyRunRecord, StrategyVersionRegistry
from .post_apply_observation import record_post_apply_observation


def record_strategy_run_post_apply_observation(
    paths: WorkspacePaths,
    record: StrategyRunRecord,
) -> dict[str, Any]:
    """Append a post-apply observation for a strategy tick when possible.

    Strategy ticks do not carry a proposal id directly. The stable link is the
    strategy package hash: apply-time version records pin
    ``package_hash -> proposal_id`` after a strategy proposal or tuning proposal
    lands in the workspace.
    """

    version = StrategyVersionRegistry(paths, record.strategy_id).get(record.package_hash)
    if version is None or not version.proposal_id:
        return {
            "ok": False,
            "reason": "no_proposal_for_package_hash",
            "strategy_id": record.strategy_id,
            "package_hash": record.package_hash,
            "run_id": record.run_id,
        }

    result = record.outputs.get("result") if isinstance(record.outputs, dict) else {}
    if not isinstance(result, dict):
        result = {}
    status = _status_for_run(record)
    evidence_ref = f"file:strategies/{record.strategy_id}/runs/{record.run_id}.json"
    metrics = {
        "strategy_id": record.strategy_id,
        "package_hash": record.package_hash,
        "mode": record.mode,
        "run_status": record.status,
        "duration_ms": record.duration_ms,
        "llm_calls": int((record.outputs or {}).get("llm_calls") or 0),
        "subagent_calls": int((record.outputs or {}).get("subagent_calls") or 0),
        "result_status": result.get("status"),
        "has_intent": bool(result.get("intent")),
        "has_order": bool(result.get("order")),
    }
    summary = (
        f"Post-apply {record.mode} strategy tick {record.run_id} finished "
        f"as {record.status}: {record.reason or result.get('reason') or 'no reason'}"
    )
    return record_post_apply_observation(
        paths,
        proposal_id=version.proposal_id,
        status=status,
        summary=summary,
        source=f"strategy_run_{record.mode}",
        observed_at=record.finished_at,
        evidence_refs=[evidence_ref],
        metrics=metrics,
        run_id=record.run_id,
        metadata={
            "strategy_id": record.strategy_id,
            "package_hash": record.package_hash,
            "strategy_version": version.asdict(),
            "session_id": record.session_id,
            "trigger_event_id": record.trigger_event_id,
        },
    )


def _status_for_run(record: StrategyRunRecord) -> str:
    if str(record.status or "").lower() == "error" or record.error:
        return "failed"
    return "observing"


__all__ = ["record_strategy_run_post_apply_observation"]
