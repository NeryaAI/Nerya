from __future__ import annotations

import difflib
import json
from pathlib import Path

from ..evolution import assets as evolution_assets
from ..evolution.backtest_comparison import proposal_backtest_comparison
from ..evolution.event_store import list_events, list_signals
from ..evolution.evidence_resolver import resolve_evidence_refs
from ..evolution.fitness import proposal_fitness_vector
from ..evolution.lineage_graph import build_lineage_graph
from ..evolution.optimizer_feedback import (
    enrich_optimizer_report_with_candidate_decisions,
    optimizer_feedback_summary,
)
from ..evolution.runner import evolve
from ..evolution.patch_proposal import delete_proposal, list_proposals, set_state
from ..evolution.post_apply_observation import (
    post_apply_monitor,
    post_apply_observations_by_proposal,
    record_post_apply_observation,
)
from ..evolution.periodic_reflection import (
    PERIODIC_REFLECTION_SCHEDULE_ID,
    configure_periodic_reflection,
    ensure_periodic_reflection,
    get_periodic_reflection,
)
from ..evolution.promotion import apply_proposal
from ..evolution.promotion import proposal_action_gates
from ..evolution.ranking import (
    build_evidence, rank_proposals, write_ranking_snapshot,
)
from ..evolution.rollback import rollback_proposal
from ..evolution.selector import annotate_assets_with_gdi
from ..evolution.signals import collect_signals
from ..evolution.timeline import build_timeline, proposal_process_trace, proposal_why_reused
from ..evolution.validation_plan import run_validation_plan
from ..evidence import autoingest as _evidence_autoingest
from ..core.paths import WorkspacePaths


_MAX_PROPOSAL_FILE_BYTES = 200_000
_MAX_PROPOSAL_FILES_TOTAL_BYTES = 1_000_000


def _proposal_strategy_files(proposal_path: Path) -> dict[str, str]:
    strategies_root = proposal_path / "after" / "strategies"
    if not strategies_root.exists():
        return {}
    strategy_dirs = [p for p in strategies_root.iterdir() if p.is_dir()]
    files: dict[str, str] = {}
    total = 0
    for strategy_dir in sorted(strategy_dirs):
        for path in sorted(p for p in strategy_dir.rglob("*") if p.is_file()):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_PROPOSAL_FILE_BYTES:
                continue
            if total + size > _MAX_PROPOSAL_FILES_TOTAL_BYTES:
                return files
            try:
                rel = path.relative_to(strategy_dir).as_posix()
                if len(strategy_dirs) > 1:
                    rel = f"{strategy_dir.name}/{rel}"
                files[rel] = path.read_text(encoding="utf-8")
                total += size
            except UnicodeDecodeError:
                continue
            except OSError:
                continue
    return files


def _proposal_workspace_root(proposal_path: Path) -> Path:
    try:
        return proposal_path.parents[2]
    except IndexError:
        return proposal_path


def _read_text_preview(path: Path, *, max_bytes: int = _MAX_PROPOSAL_FILE_BYTES) -> tuple[str, bool, bool]:
    try:
        size = path.stat().st_size
    except OSError:
        return "", False, False
    truncated = size > max_bytes
    try:
        with path.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError:
        return "", False, False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True
    try:
        return raw.decode("utf-8"), True, truncated
    except UnicodeDecodeError:
        return "", False, truncated


def _proposal_file_changes(proposal_path: Path, workspace_root: Path) -> list[dict]:
    after_root = proposal_path / "after"
    if not after_root.exists():
        return []
    changes: list[dict] = []
    total = 0
    for after_path in sorted(p for p in after_root.rglob("*") if p.is_file()):
        try:
            size = after_path.stat().st_size
        except OSError:
            continue
        if size > _MAX_PROPOSAL_FILE_BYTES:
            continue
        if total + size > _MAX_PROPOSAL_FILES_TOTAL_BYTES:
            break
        rel = after_path.relative_to(after_root).as_posix()
        before_path = workspace_root / rel
        after_text, after_text_ok, after_truncated = _read_text_preview(after_path)
        if not after_text_ok:
            continue
        before_exists = before_path.exists()
        before_text = ""
        before_text_ok = False
        before_truncated = False
        if before_exists and before_path.is_file():
            before_text, before_text_ok, before_truncated = _read_text_preview(before_path)
            if not before_text_ok:
                before_text = ""
        diff = "\n".join(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=f"before/{rel}",
                tofile=f"after/{rel}",
                lineterm="",
            )
        )
        changes.append(
            {
                "path": rel,
                "before_path": str(before_path),
                "after_path": str(after_path),
                "before_exists": before_exists,
                "before": before_text,
                "after": after_text,
                "diff": diff,
                "before_truncated": before_truncated,
                "after_truncated": after_truncated,
            }
        )
        total += size
    return changes


def _proposal_validation_plan(paths: WorkspacePaths, proposal_detail: dict) -> dict | None:
    plan_id = str(proposal_detail.get("validation_plan_id") or "").strip()
    if not plan_id:
        return None
    path = paths.evolution_validation_plans / f"{plan_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _proposal_detail_dict(proposal, *, workspace_root: Path | None = None) -> dict:
    workspace_root = workspace_root or _proposal_workspace_root(proposal.path)
    paths = WorkspacePaths(root=workspace_root)
    detail: dict = {
        "id": proposal.id,
        "kind": proposal.kind,
        "state": proposal.state,
        "summary": proposal.summary,
        "ts": proposal.ts,
        "path": str(proposal.path),
        "target": proposal.target,
        "evidence_refs": list(proposal.evidence_refs or []),
        "source_event_id": proposal.source_event_id,
        "validation_plan_id": proposal.validation_plan_id,
        "metadata": dict(proposal.metadata or {}),
    }
    for name in ("rationale.md", "diff.patch", "test_plan.md", "rollback.md"):
        p = proposal.path / name
        if p.exists():
            detail[name.replace(".", "_")] = p.read_text(encoding="utf-8")
    files = _proposal_strategy_files(proposal.path)
    if files:
        detail["files"] = files
    changes = _proposal_file_changes(proposal.path, workspace_root)
    if changes:
        detail["file_changes"] = changes
    optimizer_report = _proposal_optimizer_report(
        proposal.path,
        paths=paths,
        strategy_id=str((proposal.metadata or {}).get("strategy_id") or ""),
    )
    if optimizer_report:
        detail["optimizer_report"] = optimizer_report
    validation_plan = _proposal_validation_plan(paths, detail)
    comparison = proposal_backtest_comparison(paths, detail)
    if comparison:
        detail["backtest_comparison"] = comparison
    observations = post_apply_observations_by_proposal(
        paths,
        proposal_id=detail["id"],
    ).get(detail["id"], [])
    monitor = post_apply_monitor(detail, observations)
    if monitor:
        detail["post_apply_monitor"] = monitor
    detail["action_gates"] = proposal_action_gates(paths, proposal)
    why_reused = proposal_why_reused(
        paths,
        detail,
        validation_plan=validation_plan,
        backtest_comparison=comparison,
        post_apply_monitor=monitor,
        file_changes=changes,
    )
    if why_reused:
        detail["why_reused"] = why_reused
    detail["process"] = proposal_process_trace(
        detail,
        validation_plan=validation_plan,
        backtest_comparison=comparison,
        why_reused=why_reused,
    )
    detail["fitness_vector"] = proposal_fitness_vector(
        paths,
        detail,
        validation_plan=validation_plan,
        backtest_comparison=comparison,
        post_apply_monitor=monitor,
    )
    detail["lineage_graph"] = build_lineage_graph(
        detail,
        validation_plan=validation_plan,
        backtest_comparison=comparison,
        post_apply_monitor=monitor,
        why_reused=why_reused,
        action_gates=detail.get("action_gates"),
        file_changes=changes,
    )
    return detail


def _proposal_optimizer_report(
    proposal_path: Path,
    *,
    paths: WorkspacePaths | None = None,
    strategy_id: str | None = None,
) -> dict | None:
    path = proposal_path / "tuning_run.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    report = data.get("optimizer_report") if isinstance(data, dict) else None
    if not isinstance(report, dict) or not report:
        return None
    if paths is not None:
        report = enrich_optimizer_report_with_candidate_decisions(
            paths,
            report,
            strategy_id=strategy_id or None,
        )
    candidates = [
        row for row in report.get("candidates", [])
        if isinstance(row, dict)
    ]
    if not candidates:
        return None
    return {
        "version": report.get("version"),
        "candidate_count": report.get("candidate_count"),
        "evaluated_count": report.get("evaluated_count"),
        "truncated": bool(report.get("truncated")),
        "selected_candidate_id": report.get("selected_candidate_id"),
        "selected_index": report.get("selected_index"),
        "selected_score": report.get("selected_score"),
        "selection_reason": report.get("selection_reason"),
        "outcome_feedback": report.get("outcome_feedback") or {},
        "validation_preview": report.get("validation_preview") or {},
        "backtest_preview": report.get("backtest_preview") or {},
        "candidates": candidates[:12],
    }


def _apply_handler(client, payload):
    """``POST /evolution/apply`` — apply a strategy proposal and emit
    an evidence vault record for the promotion.
    """

    proposal_id = (payload or {}).get("proposal_id")
    result = apply_proposal(client.config.paths, proposal_id)
    # Best-effort vault ingest. Vault failures must never break the
    # promotion call site, so we wrap defensively.
    try:
        if isinstance(result, dict) and result.get("ok"):
            strategy_id = (
                result.get("strategy_id")
                or (result.get("proposal") or {}).get("strategy_id")
                or "unknown"
            )
            title = (
                result.get("title")
                or f"Strategy proposal {proposal_id} applied"
            )
            summary = (
                result.get("summary")
                or f"Proposal {proposal_id} promoted on strategy {strategy_id}."
            )
            _evidence_autoingest.on_strategy_promote(
                client,
                strategy_id=str(strategy_id),
                proposal_id=str(proposal_id),
                title=str(title),
                summary=str(summary),
                body=str(summary),
                tags=["promotion"],
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return result


def routes():
    def list_proposals_route(client, payload):
        payload = payload or {}
        kind = str(payload.get("kind") or "").strip().lower()
        state = str(payload.get("state") or "").strip().lower()
        proposals = list_proposals(client.config.paths)
        if kind:
            proposals = [p for p in proposals if str(p.kind).lower() == kind]
        if state:
            proposals = [p for p in proposals if str(p.state).lower() == state]
        proposals = sorted(
            proposals,
            key=lambda p: (str(p.ts or ""), str(p.id or "")),
            reverse=True,
        )
        limit = int(payload.get("limit") or 0)
        if limit > 0:
            proposals = proposals[:limit]
        return {"proposals": [p.asdict() for p in proposals]}

    def get_proposal_route(client, payload):
        payload = payload or {}
        pid = str(payload.get("proposal_id") or payload.get("id") or "").strip()
        for proposal in list_proposals(client.config.paths):
            if proposal.id == pid:
                return _proposal_detail_dict(
                    proposal,
                    workspace_root=client.config.paths.root,
                )
        return {"_status": 404, "error": "proposal_not_found", "proposal_id": pid}

    def approve_proposal_route(client, payload):
        payload = payload or {}
        pid = str(payload.get("proposal_id") or payload.get("id") or "").strip()
        if not pid:
            return {"_status": 400, "error": "proposal_id required"}
        proposal = next((p for p in list_proposals(client.config.paths) if p.id == pid), None)
        if proposal is None:
            return {"_status": 404, "error": "proposal_not_found", "proposal_id": pid}
        if proposal.state == "applied":
            return {
                "_status": 409,
                "error": "proposal_already_applied",
                "proposal_id": pid,
                "state": proposal.state,
            }
        if proposal.state in {"rejected", "rolled_back"}:
            return {
                "_status": 409,
                "error": "proposal_not_open",
                "proposal_id": pid,
                "state": proposal.state,
            }
        if proposal.state != "approved":
            proposal = set_state(
                client.config.paths,
                pid,
                "approved",
                note=str(payload.get("note") or "approved by operator"),
            ) or proposal
        return _proposal_detail_dict(
            proposal,
            workspace_root=client.config.paths.root,
        )

    def reject_proposal_route(client, payload):
        payload = payload or {}
        pid = str(payload.get("proposal_id") or payload.get("id") or "").strip()
        if not pid:
            return {"_status": 400, "error": "proposal_id required"}
        proposal = next((p for p in list_proposals(client.config.paths) if p.id == pid), None)
        if proposal is None:
            return {"_status": 404, "error": "proposal_not_found", "proposal_id": pid}
        if proposal.state == "applied":
            return {
                "_status": 409,
                "error": "proposal_already_applied",
                "proposal_id": pid,
                "state": proposal.state,
            }
        if proposal.state == "rolled_back":
            return {
                "_status": 409,
                "error": "proposal_already_rolled_back",
                "proposal_id": pid,
                "state": proposal.state,
            }
        if proposal.state != "rejected":
            proposal = set_state(
                client.config.paths,
                pid,
                "rejected",
                note=str(payload.get("note") or "rejected by operator"),
            ) or proposal
        return _proposal_detail_dict(
            proposal,
            workspace_root=client.config.paths.root,
        )

    def delete_proposal_route(client, payload):
        """``POST /evolution/proposals/delete`` — drop a pending proposal.

        Lets the chat UI (and strategies page) delete an agent-generated
        proposal it does not want to keep, without leaving it in the
        pending-review queue. Applied proposals stay protected unless the
        caller passes ``force``.
        """

        payload = payload or {}
        pid = str(payload.get("proposal_id") or payload.get("id") or "").strip()
        if not pid:
            return {"_status": 400, "error": "proposal_id required"}
        result = delete_proposal(
            client.config.paths,
            pid,
            force=bool(payload.get("force", False)),
            note=str(payload.get("note") or ""),
        )
        if not result.get("ok"):
            reason = str(result.get("reason") or "")
            if reason == "not_found":
                return {
                    "_status": 404,
                    "error": "proposal_not_found",
                    "proposal_id": pid,
                }
            if reason == "applied_requires_force":
                return {
                    "_status": 409,
                    "error": "applied_requires_force",
                    "proposal_id": pid,
                    "state": result.get("state"),
                }
            return {"_status": 400, "error": reason or "delete_failed", **result}
        return result

    def post_apply_observation_route(client, payload):
        payload = payload or {}
        pid = str(payload.get("proposal_id") or payload.get("id") or "").strip()
        result = record_post_apply_observation(
            client.config.paths,
            proposal_id=pid,
            status=payload.get("status"),
            summary=str(payload.get("summary") or payload.get("note") or ""),
            source=str(payload.get("source") or "manual"),
            observed_at=payload.get("observed_at"),
            evidence_refs=payload.get("evidence_refs"),
            metrics=payload.get("metrics"),
            backtest_result=payload.get("backtest_result"),
            run_id=payload.get("run_id"),
            operator=payload.get("operator"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            allow_unapplied=bool(payload.get("allow_unapplied", False)),
        )
        if result.get("ok"):
            return result
        reason = str(result.get("reason") or "record_failed")
        if reason == "proposal_not_found":
            return {"_status": 404, "error": reason, **result}
        if reason == "proposal_not_applied":
            return {"_status": 409, "error": reason, **result}
        return {"_status": 400, "error": reason, **result}

    def reflect(client, payload):
        """POST /evolution/reflect — run a reflection tick and return the
        ranked proposal seeds with evidence attached."""
        return evolve(client.config)

    def rank(client, payload):
        """POST /evolution/rank — rank open proposals using attribution.

        consume the evidence surfaces (reflection
        findings and paper/live divergence) to score every open
        proposal on severity, freshness, and scope. Optionally writes
        a snapshot to ``workspace/evolution/ranking.json`` for UIs.
        """
        payload = payload or {}
        strategy_id = payload.get("strategy_id")
        states = payload.get("states")
        persist = bool(payload.get("persist", False))
        ranked = rank_proposals(
            client.config.paths,
            strategy_id=strategy_id,
            states=tuple(states) if states else ("draft", "proposed"),
        )
        out: dict = {
            "strategy_id": strategy_id,
            "count": len(ranked),
            "ranked": [rp.asdict() for rp in ranked],
        }
        if persist:
            out["snapshot"] = write_ranking_snapshot(
                client.config.paths, ranked,
            )
        return out

    def evidence(client, payload):
        """POST /evolution/evidence — return the raw evidence bundle for
        one strategy so an operator can judge ranking quality."""
        sid = (payload or {}).get("strategy_id")
        if not sid:
            return {"error": "strategy_id required"}
        bundle = build_evidence(client.config.paths, sid)
        return {
            "strategy_id": bundle.strategy_id,
            "severity": bundle.severity(),
            "signals": bundle.signals(),
            "divergence": bundle.divergence,
            "counts": {
                "losses": len(bundle.losses),
                "bad_triggers": len(bundle.bad_triggers),
                "high_slippage": len(bundle.high_slippage),
                "stale_data": len(bundle.stale_data),
                "subagent_disagreement": len(bundle.subagent_disagreement),
                "overtrading": len(bundle.overtrading),
                "missed_opportunity": len(bundle.missed_opportunity),
            },
        }

    def evidence_resolve(client, payload):
        """POST /evolution/evidence/resolve — turn evidence ref tokens into
        redacted operator-facing records and artifacts.
        """
        payload = payload or {}
        refs = payload.get("refs")
        if refs is None:
            one = payload.get("ref")
            refs = [one] if one else []
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            return {"_status": 400, "error": "refs must be a list or string"}
        return resolve_evidence_refs(client.config.paths, [str(ref) for ref in refs])

    def signals(client, payload):
        payload = payload or {}
        refresh = str(payload.get("refresh") or "").lower() in {"1", "true", "yes"}
        if refresh:
            collected = collect_signals(
                client.config.paths,
                strategy_id=payload.get("strategy_id"),
                persist=True,
                limit=int(payload.get("limit") or 200),
            )
        else:
            collected = []
        rows = list_signals(
            client.config.paths,
            source=payload.get("source"),
            strategy_id=payload.get("strategy_id"),
            severity=payload.get("severity"),
            kind=payload.get("kind"),
            limit=int(payload.get("limit") or 100),
        )
        return {"signals": rows, "count": len(rows), "collected": collected}

    def events(client, payload):
        payload = payload or {}
        rows = list_events(
            client.config.paths,
            strategy_id=payload.get("strategy_id"),
            proposal_id=payload.get("proposal_id"),
            outcome=payload.get("outcome"),
            limit=int(payload.get("limit") or 100),
        )
        return {"events": rows, "count": len(rows)}

    def assets(client, payload):
        payload = payload or {}
        rows = evolution_assets.search_assets(
            client.config.paths,
            kind=payload.get("kind"),
            query=payload.get("query"),
            strategy_id=payload.get("strategy_id"),
            limit=int(payload.get("limit") or 100),
        )
        rows = annotate_assets_with_gdi(client.config.paths, rows)
        candidates = evolution_assets.list_candidates(
            client.config.paths,
            limit=int(payload.get("candidate_limit") or 100),
        )
        optimizer_feedback = optimizer_feedback_summary(
            client.config.paths,
            strategy_id=payload.get("strategy_id"),
            limit=int(payload.get("limit") or 100),
        )
        return {
            "assets": rows,
            "candidates": candidates,
            "optimizer_feedback": optimizer_feedback,
            "count": len(rows),
            "candidate_count": len(candidates),
        }

    def candidate(client, payload):
        payload = payload or {}
        return evolution_assets.create_candidate(
            client.config.paths,
            kind=str(payload.get("kind") or "capsule"),
            summary=str(payload.get("summary") or ""),
            payload=dict(payload.get("payload") or {}),
            evidence_refs=list(payload.get("evidence_refs") or []),
            source_event_id=payload.get("source_event_id"),
            strategy_id=payload.get("strategy_id"),
        )

    def promote_asset(client, payload):
        payload = payload or {}
        return evolution_assets.promote_candidate(
            client.config.paths,
            str(payload.get("candidate_id") or ""),
            operator=payload.get("operator"),
        )

    def reject_asset(client, payload):
        payload = payload or {}
        return evolution_assets.reject_candidate(
            client.config.paths,
            str(payload.get("candidate_id") or ""),
            reason=str(payload.get("reason") or ""),
            operator=payload.get("operator"),
        )

    def validate(client, payload):
        payload = payload or {}
        return run_validation_plan(
            client.config.paths,
            plan_id=payload.get("plan_id"),
            proposal_id=payload.get("proposal_id"),
            dry_run=bool(payload.get("dry_run", True)),
        )

    def timeline(client, payload):
        payload = payload or {}
        return build_timeline(
            client.config,
            strategy_id=payload.get("strategy_id"),
            query=payload.get("query"),
            limit=int(payload.get("limit") or 120),
        )

    def reflection_schedule(client, payload):
        payload = payload or {}
        if not payload:
            return {
                "ok": True,
                "schedule": get_periodic_reflection(client.config.paths),
            }
        return configure_periodic_reflection(
            client.config.paths,
            enabled=bool(payload.get("enabled", False)),
            time=payload.get("time"),
            cron=payload.get("cron"),
            timezone=payload.get("timezone"),
        )

    def reflection_schedule_run_now(client, payload):
        ensure_periodic_reflection(client.config.paths)
        return client.triggers.run_schedule_now(
            id=PERIODIC_REFLECTION_SCHEDULE_ID,
            reason=str((payload or {}).get("reason") or "operator_dream_now"),
        )

    return [
        ("GET", "/evolution/proposals", list_proposals_route),
        ("POST", "/evolution/proposals", list_proposals_route),
        ("POST", "/evolution/proposals/delete", delete_proposal_route),
        ("GET", "/evolution/proposals/{proposal_id}", get_proposal_route),
        ("POST", "/evolution/proposals/{proposal_id}", get_proposal_route),
        ("POST", "/evolution/proposals/{proposal_id}/approve", approve_proposal_route),
        ("POST", "/evolution/proposals/{proposal_id}/reject", reject_proposal_route),
        ("POST", "/evolution/proposals/{proposal_id}/post_apply_observation", post_apply_observation_route),
        ("POST", "/evolution/post_apply_observation", post_apply_observation_route),
        ("POST", "/evolution/apply", _apply_handler),
        ("POST", "/evolution/rollback",
         lambda client, payload: rollback_proposal(client.config.paths,
                                                   payload["proposal_id"])),
        ("POST", "/evolution/reflect", reflect),
        ("POST", "/evolution/rank", rank),
        ("POST", "/evolution/evidence", evidence),
        ("POST", "/evolution/evidence/resolve", evidence_resolve),
        ("GET", "/evolution/signals", signals),
        ("POST", "/evolution/signals", signals),
        ("GET", "/evolution/events", events),
        ("POST", "/evolution/events", events),
        ("GET", "/evolution/timeline", timeline),
        ("POST", "/evolution/timeline", timeline),
        ("GET", "/evolution/reflection_schedule", reflection_schedule),
        ("POST", "/evolution/reflection_schedule", reflection_schedule),
        ("POST", "/evolution/reflection_schedule/run_now", reflection_schedule_run_now),
        ("GET", "/evolution/assets", assets),
        ("POST", "/evolution/assets", assets),
        ("POST", "/evolution/assets/candidate", candidate),
        ("POST", "/evolution/assets/promote", promote_asset),
        ("POST", "/evolution/assets/reject", reject_asset),
        ("POST", "/evolution/validation/run", validate),
    ]
