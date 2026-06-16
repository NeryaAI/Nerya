"""Product-shaped timeline for Nerya self-evolution telemetry."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.redaction import redact_display_dict, redact_text
from . import assets as evolution_assets
from .backtest_comparison import proposal_backtest_comparison
from .event_store import list_events, list_signals
from .fitness import proposal_fitness_vector
from .lineage_graph import build_lineage_graph
from .optimizer_feedback import optimizer_feedback_summary
from .patch_proposal import list_proposals
from .post_apply_observation import (
    post_apply_monitor as _post_apply_monitor,
    post_apply_observations_by_proposal,
)
from .promotion import proposal_action_gates


_OPEN_PROPOSAL_STATES = {"draft", "pending_review", "proposed", "approved"}
_TERMINAL_NEGATIVE_STATES = {"rejected", "rolled_back"}
_PASSED_VALIDATION_STATES = {"passed", "safe", "ready", "ok"}
_POST_APPLY_HEALTHY_STATES = {"healthy", "passed", "ok", "stable", "improved"}
_POST_APPLY_NEGATIVE_STATES = {"regressed", "failed", "degraded", "rollback_recommended"}
_MATERIALIZED_PROPOSAL_KINDS = {
    "strategy_tuning_proposal",
    "strategy_package_proposal",
    "strategy_config_patch",
    "script_proposal",
    "skill_proposal",
    "trigger_route_patch",
}
_INBOX_GROUPS = [
    {
        "id": "needs_evidence",
        "tone": "danger",
        "stage": "signal",
        "action": "Resolve evidence refs",
    },
    {
        "id": "needs_materialization",
        "tone": "danger",
        "stage": "proposal",
        "action": "Materialize file changes",
    },
    {
        "id": "needs_validation",
        "tone": "warn",
        "stage": "validation",
        "action": "Run or fix validation",
    },
    {
        "id": "needs_approval",
        "tone": "brand",
        "stage": "proposal",
        "action": "Review and approve",
    },
    {
        "id": "monitoring",
        "tone": "ok",
        "stage": "outcome",
        "action": "Watch post-apply effect",
    },
    {
        "id": "reusable_learning",
        "tone": "ok",
        "stage": "asset",
        "action": "Reuse or promote learning",
    },
    {
        "id": "negative_learning",
        "tone": "danger",
        "stage": "asset",
        "action": "Avoid repeating this path",
    },
]


def build_timeline(
    config,
    *,
    strategy_id: str | None = None,
    query: str | None = None,
    limit: int = 120,
) -> dict[str, Any]:
    """Return a dashboard-ready evolution timeline.

    The raw evolution stores are intentionally append-only and module-shaped.
    This reducer keeps the runtime source of truth unchanged while giving the
    dashboard a single operator-facing chain: signal -> event -> proposal ->
    validation -> reusable asset/outcome.
    """

    paths = config.paths
    capped = max(1, min(int(limit or 120), 500))
    read_limit = max(capped, 80)

    signals = list_signals(paths, strategy_id=strategy_id, limit=read_limit)
    events = list_events(paths, strategy_id=strategy_id, limit=read_limit)
    proposals = [
        p.asdict()
        for p in list_proposals(paths)
        if not strategy_id or _proposal_matches_strategy(p.asdict(), strategy_id)
    ]
    asset_rows = evolution_assets.search_assets(
        paths,
        strategy_id=strategy_id,
        limit=read_limit,
    )
    candidates = evolution_assets.list_candidates(paths, limit=read_limit)
    if strategy_id:
        candidates = [
            row for row in candidates
            if str(row.get("strategy_id") or "") == strategy_id
        ]
    validation_plans = _list_validation_plans(
        paths.evolution_validation_plans,
        strategy_id=strategy_id,
        limit=read_limit,
    )
    strategy_audits = _strategy_tuning_audits(
        paths,
        strategy_id=strategy_id,
        limit=min(read_limit, 20),
    )
    optimizer_feedback = optimizer_feedback_summary(
        paths,
        strategy_id=strategy_id,
        limit=read_limit,
    )

    proposals_by_id = {str(p.get("id") or ""): p for p in proposals}
    plans_by_id = {str(p.get("id") or ""): p for p in validation_plans}
    post_apply_by_proposal = post_apply_observations_by_proposal(paths, limit=read_limit)
    audits_by_run = {
        str(a.get("run_id") or ""): a
        for a in strategy_audits
        if a.get("run_id")
    }
    audits_by_proposal = {
        str(a.get("proposal_id") or ""): a
        for a in strategy_audits
        if a.get("proposal_id")
    }
    audits_by_event = {
        str(a.get("source_event_id") or ""): a
        for a in strategy_audits
        if a.get("source_event_id")
    }
    candidates_by_source = _group_by(candidates, "source_event_id")
    capsules_by_source = _group_by(
        [row for row in asset_rows if row.get("kind") == "capsule"],
        "source_event_id",
    )

    items: list[dict[str, Any]] = []
    for row in signals:
        items.append(_signal_item(
            row,
            audit=_audit_for_refs(audits_by_run, row.get("evidence_refs")),
        ))
    for row in events:
        proposal = proposals_by_id.get(str(row.get("proposal_id") or ""))
        items.append(
            _event_item(
                row,
                proposal=proposal,
                validation_plan=_event_validation_plan(row, proposal, plans_by_id),
                audit=(
                    audits_by_event.get(str(row.get("id") or ""))
                    or _audit_for_refs(audits_by_run, row.get("evidence_refs"))
                ),
                candidates=candidates_by_source.get(str(row.get("id") or ""), []),
                capsules=capsules_by_source.get(str(row.get("id") or ""), []),
            )
        )
    for row in proposals:
        validation_plan = plans_by_id.get(str(row.get("validation_plan_id") or ""))
        comparison = proposal_backtest_comparison(paths, row)
        post_apply_monitor = _post_apply_monitor(row, post_apply_by_proposal.get(str(row.get("id") or ""), []))
        fitness = proposal_fitness_vector(
            paths,
            row,
            validation_plan=validation_plan,
            backtest_comparison=comparison,
            post_apply_monitor=post_apply_monitor,
        )
        action_gates = proposal_action_gates(paths, row.get("id") or "")
        audit = (
            audits_by_proposal.get(str(row.get("id") or ""))
            or _audit_for_refs(audits_by_run, row.get("evidence_refs"))
        )
        why_reused = proposal_why_reused(
            paths,
            row,
            audit=audit,
            validation_plan=validation_plan,
            backtest_comparison=comparison,
            post_apply_monitor=post_apply_monitor,
        )
        lineage_graph = build_lineage_graph(
            row,
            validation_plan=validation_plan,
            backtest_comparison=comparison,
            post_apply_monitor=post_apply_monitor,
            why_reused=why_reused,
            action_gates=action_gates,
        )
        items.append(_proposal_item(
            row,
            validation_plan,
            audit=audit,
            backtest_comparison=comparison,
            post_apply_monitor=post_apply_monitor,
            fitness_vector=fitness,
            why_reused=why_reused,
            action_gates=action_gates,
            lineage_graph=lineage_graph,
        ))
    for row in validation_plans:
        items.append(_validation_item(row))
    for row in candidates:
        items.append(_candidate_item(row))
    for row in asset_rows:
        if row.get("kind") == "capsule":
            items.append(_capsule_item(row))

    q = (query or "").strip().lower()
    if q:
        items = [item for item in items if q in _search_blob(item)]

    items.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    items = items[:capped]

    summary = _summary(
        items=items,
        signals=signals,
        events=events,
        proposals=proposals,
        assets=asset_rows,
        candidates=candidates,
        validation_plans=validation_plans,
    )
    inbox = _build_inbox(items)

    return {
        "ok": True,
        "timeline": items,
        "inbox": inbox,
        "summary": summary,
        "config": _config_snapshot(config, strategy_id=strategy_id),
        "raw": {
            "signals": signals,
            "events": events,
            "proposals": proposals,
            "assets": asset_rows,
            "candidates": candidates,
            "validation_plans": validation_plans,
            "strategy_audits": strategy_audits,
            "optimizer_feedback": optimizer_feedback,
        },
    }


def _signal_item(
    row: dict[str, Any],
    *,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = str(row.get("id") or "")
    kind = str(row.get("kind") or "signal")
    severity = str(row.get("severity") or "info")
    return {
        "id": f"signal:{sid}",
        "record_id": sid,
        "type": "signal",
        "stage": "signal",
        "ts": row.get("ts") or "",
        "title": _title(f"{kind} signal"),
        "summary": row.get("summary") or "",
        "status": severity,
        "severity": severity,
        "source": row.get("source"),
        "strategy_id": row.get("strategy_id"),
        "signal_ids": [sid] if sid else [],
        "evidence_refs": _str_list(row.get("evidence_refs")),
        "why": row.get("summary") or f"Runtime emitted {kind}.",
        "next_step": _next_step_for_signal(severity),
        "process": _process_trace(row=row, audit=audit),
        "raw": row,
    }


def _event_item(
    row: dict[str, Any],
    *,
    proposal: dict[str, Any] | None,
    validation_plan: dict[str, Any] | None,
    audit: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    capsules: list[dict[str, Any]],
) -> dict[str, Any]:
    eid = str(row.get("id") or "")
    outcome = str(row.get("outcome") or "candidate")
    metadata = dict(row.get("metadata") or {})
    validation_plan_id = (
        metadata.get("validation_plan_id")
        or (proposal or {}).get("validation_plan_id")
        or None
    )
    asset_ids = [
        str(x.get("id") or "")
        for x in [*candidates, *capsules]
        if x.get("id")
    ]
    summary = str(row.get("summary") or "")
    if summary.startswith("Agent turn "):
        title = "Agent turn completed"
    elif summary.startswith("Agent session "):
        title = "Agent session ended"
    else:
        title = _title(f"{outcome} evolution event")
    return {
        "id": f"event:{eid}",
        "record_id": eid,
        "type": "event",
        "stage": _stage_for_outcome(outcome),
        "ts": row.get("ts") or "",
        "title": title,
        "summary": summary,
        "status": outcome,
        "outcome": outcome,
        "strategy_id": row.get("strategy_id"),
        "proposal_id": row.get("proposal_id"),
        "validation_plan_id": validation_plan_id,
        "validation_status": row.get("validation_status"),
        "signal_ids": _str_list(row.get("signals")),
        "asset_ids": asset_ids,
        "evidence_refs": _str_list(row.get("evidence_refs")),
        "why": _why_for_event(row),
        "next_step": _next_step_for_event(outcome, row.get("validation_status")),
        "process": _process_trace(
            row=row,
            proposal=proposal,
            validation_plan=validation_plan,
            audit=audit,
        ),
        "optimizer_report": _optimizer_report_digest(audit or {}),
        "raw": row,
    }


def _proposal_item(
    row: dict[str, Any],
    validation_plan: dict[str, Any] | None,
    *,
    audit: dict[str, Any] | None = None,
    backtest_comparison: dict[str, Any] | None = None,
    post_apply_monitor: dict[str, Any] | None = None,
    fitness_vector: dict[str, Any] | None = None,
    why_reused: dict[str, Any] | None = None,
    action_gates: dict[str, Any] | None = None,
    lineage_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pid = str(row.get("id") or "")
    state = str(row.get("state") or "draft")
    evidence_refs = _str_list(row.get("evidence_refs"))
    if post_apply_monitor:
        evidence_refs = _unique_strings([
            *evidence_refs,
            *_str_list(post_apply_monitor.get("evidence_refs")),
        ])
    return {
        "id": f"proposal:{pid}",
        "record_id": pid,
        "type": "proposal",
        "stage": "proposal",
        "ts": row.get("ts") or "",
        "title": _proposal_title(row),
        "summary": row.get("summary") or "",
        "status": state,
        "strategy_id": _proposal_strategy(row),
        "proposal_id": pid,
        "validation_plan_id": row.get("validation_plan_id"),
        "validation_status": (validation_plan or {}).get("status"),
        "source_event_id": row.get("source_event_id"),
        "evidence_refs": evidence_refs,
        "why": row.get("summary") or "Proposal was created from evolution evidence.",
        "next_step": _next_step_for_proposal(
            state,
            bool(row.get("validation_plan_id")),
            post_apply_monitor=post_apply_monitor,
        ),
        "process": _process_trace(
            row=row,
            proposal=row,
            validation_plan=validation_plan,
            audit=audit,
            backtest_comparison=backtest_comparison,
            why_reused=why_reused,
        ),
        "backtest_comparison": backtest_comparison,
        "post_apply_monitor": post_apply_monitor,
        "fitness_vector": fitness_vector,
        "why_reused": why_reused,
        "action_gates": action_gates,
        "lineage_graph": lineage_graph,
        "optimizer_report": _optimizer_report_digest(audit or {}),
        "raw": row,
    }


def _validation_item(row: dict[str, Any]) -> dict[str, Any]:
    vid = str(row.get("id") or "")
    status = str(row.get("status") or "not_run")
    blocked = _str_list(row.get("blocked_reasons"))
    return {
        "id": f"validation:{vid}",
        "record_id": vid,
        "type": "validation",
        "stage": "validation",
        "ts": row.get("created_at") or "",
        "title": "Validation plan",
        "summary": _validation_summary(row),
        "status": "blocked" if blocked else status,
        "strategy_id": row.get("strategy_id"),
        "proposal_id": row.get("proposal_id"),
        "validation_plan_id": vid,
        "validation_status": status,
        "blocked_reasons": blocked,
        "evidence_refs": _validation_evidence(row),
        "why": "Validation gates define what must pass before promotion.",
        "next_step": (
            "Resolve blocked validation commands before review."
            if blocked else "Run a dry-run from the dashboard before approval."
        ),
        "process": _process_trace(row=row, validation_plan=row),
        "raw": row,
    }


def _candidate_item(row: dict[str, Any]) -> dict[str, Any]:
    cid = str(row.get("id") or "")
    blocked = _str_list(row.get("blocked_reasons"))
    status = "blocked" if blocked else str(row.get("state") or "candidate")
    return {
        "id": f"asset_candidate:{cid}",
        "record_id": cid,
        "type": "asset_candidate",
        "stage": "asset",
        "ts": row.get("ts") or "",
        "title": _title(f"{row.get('kind') or 'asset'} candidate"),
        "summary": row.get("summary") or "",
        "status": status,
        "strategy_id": row.get("strategy_id"),
        "source_event_id": row.get("source_event_id"),
        "asset_ids": [cid] if cid else [],
        "evidence_refs": _str_list(row.get("evidence_refs")),
        "blocked_reasons": blocked,
        "why": row.get("summary") or "Runtime proposed a reusable learning asset.",
        "next_step": (
            "Reject or fix blocked reasons before promotion."
            if blocked else "Promote to make this learning reusable."
        ),
        "raw": row,
    }


def _capsule_item(row: dict[str, Any]) -> dict[str, Any]:
    cid = str(row.get("id") or "")
    score = row.get("outcome_score")
    return {
        "id": f"asset:{cid}",
        "record_id": cid,
        "type": "asset",
        "stage": "asset",
        "ts": row.get("ts") or "",
        "title": "Promoted capsule",
        "summary": row.get("summary") or "",
        "status": "promoted",
        "strategy_id": row.get("strategy_id"),
        "source_event_id": row.get("source_event_id"),
        "asset_ids": [cid] if cid else [],
        "evidence_refs": _str_list(row.get("evidence_refs")),
        "outcome_score": score,
        "why": "A validated outcome was stored as a reusable evolution capsule.",
        "next_step": "Future memory recall can reuse this capsule when similar signals appear.",
        "raw": row,
    }


def _list_validation_plans(
    root: Path,
    *,
    strategy_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    return rows[-max(1, int(limit)) :]


def _strategy_tuning_audits(
    paths,
    *,
    strategy_id: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    source_rows: list[dict[str, Any]] = []
    for row in jsonl.read_all(paths.journal("strategy_evolution")):
        if row.get("kind") != "strategy.tuning":
            continue
        sid = str(row.get("strategy_id") or "")
        if strategy_id and sid != strategy_id:
            continue
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
        source_rows.append(row)
    rows: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] | None = None
    for row in source_rows[-max(1, int(limit)) :]:
        sid = str(row.get("strategy_id") or "")
        run_id = str(row.get("run_id") or "")
        review_path = paths.strategy(sid) / "reviews" / f"tuning_{run_id}.md"
        audit_path = paths.strategy(sid) / "reviews" / f"tuning_{run_id}_audit.json"
        audit_json = _read_json_file(audit_path) if audit_path.exists() else None
        merged = {
            **row,
            "review_path": str(review_path) if review_path.exists() else row.get("review_path"),
            "audit_path": str(audit_path) if audit_path.exists() else row.get("audit_path"),
        }
        if isinstance(audit_json, dict):
            for key in (
                "subagent", "tier", "provider", "model", "model_calls",
                "ok", "tokens", "usd", "wall_ms",
                "prompt_path", "role_prompt", "payload", "prompt_records",
                "selected_assets", "raw_subagent_output", "optimizer_report",
                "subagent_output", "metrics", "steps", "redacted",
            ):
                if key in audit_json:
                    merged[key] = audit_json[key]
        if not (merged.get("provider") and merged.get("model") and merged.get("model_calls")):
            # ponytail: only scan the old llm journal for legacy audits missing model metadata.
            llm_calls = llm_calls if llm_calls is not None else _strategy_tuning_llm_calls(paths)
            _enrich_tuning_model_metadata(merged, llm_calls)
        rows.append(merged)
    return rows


def _strategy_tuning_llm_calls(paths) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(jsonl.read_all(paths.journal("llm"))):
        if row.get("kind") != "llm.call":
            continue
        if row.get("task") != "subagent_analysis":
            continue
        if str(row.get("caller") or "") != "subagent:strategy_tuner":
            continue
        if not (row.get("provider") or row.get("model")):
            continue
        enriched = dict(row)
        enriched["_journal_index"] = index
        enriched["_ts_seconds"] = _iso_seconds(row.get("ts"))
        rows.append(enriched)
    return rows


def _enrich_tuning_model_metadata(
    audit: dict[str, Any],
    llm_calls: list[dict[str, Any]],
) -> None:
    if audit.get("provider") and audit.get("model") and audit.get("model_calls"):
        return
    match = _match_tuning_llm_call(audit, llm_calls)
    if not match:
        return
    evidence_ref = f"journal:llm:{int(match.get('_journal_index') or 0)}"
    if not audit.get("provider"):
        audit["provider"] = match.get("provider")
    if not audit.get("model"):
        audit["model"] = match.get("model")
    if not audit.get("tokens"):
        audit["tokens"] = match.get("tokens")
    if not audit.get("usd"):
        audit["usd"] = match.get("usd")
    if not audit.get("model_calls"):
        audit["model_calls"] = [{
            "iteration": 0,
            "provider": match.get("provider"),
            "model": match.get("model"),
            "tier": match.get("tier") or audit.get("tier"),
            "tokens": match.get("tokens"),
            "usd": match.get("usd"),
            "ts": match.get("ts"),
            "evidence_ref": evidence_ref,
            "source": "llm_journal",
        }]
    audit["model_metadata_source"] = "llm_journal"
    audit["model_metadata_evidence_ref"] = evidence_ref


def _match_tuning_llm_call(
    audit: dict[str, Any],
    llm_calls: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not llm_calls:
        return None
    audit_ts = _iso_seconds(audit.get("created_at") or audit.get("ts"))
    audit_tokens = _int_or_none(audit.get("tokens"))
    audit_usd = _float_or_none(audit.get("usd"))
    prompt_chars = {
        _int_or_none(record.get("prompt_chars"))
        for record in (audit.get("prompt_records") or [])
        if isinstance(record, dict)
    }
    prompt_chars.discard(None)

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in llm_calls:
        score = 0.0
        diff = None
        if audit_ts is not None and row.get("_ts_seconds") is not None:
            diff = abs(float(row["_ts_seconds"]) - float(audit_ts))
            if diff <= 60:
                score += 60
            elif diff <= 10 * 60:
                score += 35
            else:
                continue
        if audit_tokens is not None and _int_or_none(row.get("tokens")) == audit_tokens:
            score += 40
        if audit_usd is not None:
            row_usd = _float_or_none(row.get("usd"))
            if row_usd is not None and abs(row_usd - audit_usd) <= 0.0001:
                score += 20
        if prompt_chars and _int_or_none(row.get("prompt_len")) in prompt_chars:
            score += 25
        if score >= 60:
            scored.append((score - float(diff or 0) / 1000.0, row))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _iso_seconds(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _process_trace(
    *,
    row: dict[str, Any],
    proposal: dict[str, Any] | None = None,
    validation_plan: dict[str, Any] | None = None,
    audit: dict[str, Any] | None = None,
    backtest_comparison: dict[str, Any] | None = None,
    why_reused: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []

    if audit:
        prompt_records = [
            rec for rec in (audit.get("prompt_records") or [])
            if isinstance(rec, dict)
        ]
        if audit.get("role_prompt") or prompt_records:
            prompt_artifacts: list[dict[str, Any]] = []
            if audit.get("role_prompt"):
                prompt_artifacts.append(_inline_artifact(
                    "role_prompt",
                    "Role prompt",
                    str(audit.get("role_prompt") or ""),
                    kind="prompt",
                    language="markdown",
                    path=str(audit.get("prompt_path") or ""),
                ))
            for rec in prompt_records[:3]:
                prompt_artifacts.append(_inline_artifact(
                    f"prompt_iteration_{rec.get('iteration', len(prompt_artifacts))}",
                    f"Rendered prompt #{rec.get('iteration', 0)}",
                    str(rec.get("prompt") or ""),
                    kind="prompt",
                    language="text",
                    metadata={
                        "iteration": rec.get("iteration"),
                        "prompt_chars": rec.get("prompt_chars"),
                        "redacted": rec.get("redacted", True),
                    },
                ))
            sections.append({
                "id": "prompt_inputs",
                "title": "Prompt / inputs",
                "summary": "Redacted role prompt, rendered prompt, and payload sent into the subagent.",
                "artifacts": prompt_artifacts,
            })
            artifacts.extend(prompt_artifacts)
        if audit.get("payload"):
            payload_artifact = _inline_json_artifact(
                "subagent_payload",
                "Subagent payload",
                audit.get("payload"),
                kind="input",
            )
            sections.append({
                "id": "inputs",
                "title": "Structured inputs",
                "summary": "Snapshot, manifest, objectives, guardrails, allowed targets, and tuning prompt.",
                "artifacts": [payload_artifact],
            })
            artifacts.append(payload_artifact)
        decision_context = _strategy_decision_context_digest(audit)
        if decision_context:
            context_artifact = _inline_json_artifact(
                "strategy_decision_context",
                "Market & risk context",
                decision_context,
                kind="input",
            )
            sections.append({
                "id": "strategy_decision_context",
                "title": "Market / risk context",
                "summary": (
                    "Recent market features, news, trade performance, and "
                    "risk metrics used by the strategy tuning run."
                ),
                "artifacts": [context_artifact],
            })
            artifacts.append(context_artifact)
        runtime_feedback = _runtime_feedback_digest(audit)
        if runtime_feedback:
            feedback_artifact = _inline_json_artifact(
                "runtime_feedback",
                "Runtime feedback",
                runtime_feedback,
                kind="input",
            )
            sections.append({
                "id": "runtime_feedback",
                "title": "Runtime feedback",
                "summary": (
                    "Weighted post-apply observations and paper/live/shadow "
                    "runtime evidence sent into the tuning run."
                ),
                "artifacts": [feedback_artifact],
            })
            artifacts.append(feedback_artifact)
        selected_assets = _selected_assets_digest(audit)
        if selected_assets:
            selected_artifact = _inline_json_artifact(
                "reused_evolution_assets",
                "Reused evolution assets",
                selected_assets,
                kind="asset",
            )
            sections.append({
                "id": "reused_assets",
                "title": "Reused Gene/Capsule context",
                "summary": (
                    "Genes, Capsules, negative Capsules, and GDI rationale "
                    "selected for this tuning run."
                ),
                "artifacts": [selected_artifact],
            })
            artifacts.append(selected_artifact)
        if why_reused:
            rationale_artifact = _inline_json_artifact(
                "reuse_rationale",
                "Reuse rationale",
                why_reused,
                kind="asset",
            )
            sections.append({
                "id": "reuse_rationale",
                "title": "Why these assets were reused",
                "summary": (
                    "Connects selected Gene/Capsule relevance to the proposal "
                    "diff, validation evidence, and post-apply outcome."
                ),
                "artifacts": [rationale_artifact],
            })
            artifacts.append(rationale_artifact)
        optimizer_report = _optimizer_report_digest(audit)
        if optimizer_report:
            optimizer_artifact = _inline_json_artifact(
                "candidate_optimizer",
                "Candidate optimizer",
                optimizer_report,
                kind="evaluation",
            )
            sections.append({
                "id": "candidate_optimizer",
                "title": "Candidate optimizer",
                "summary": (
                    "Deterministic local scoring used to choose one strategy "
                    "tuning candidate before proposal creation."
                ),
                "artifacts": [optimizer_artifact],
            })
            artifacts.append(optimizer_artifact)

    proposal_artifacts = _proposal_artifacts(proposal)
    if proposal_artifacts:
        sections.append({
            "id": "proposal_files",
            "title": "Proposal files",
            "summary": "Files generated under the proposal directory for operator review.",
            "artifacts": proposal_artifacts,
        })
        artifacts.extend(proposal_artifacts)

    generated_docs = _generated_docs(row=row, audit=audit)
    if generated_docs:
        sections.append({
            "id": "generated_docs",
            "title": "Generated docs",
            "summary": "Review and audit documents written by the self-evolution run.",
            "artifacts": generated_docs,
        })
        artifacts.extend(generated_docs)

    if validation_plan:
        validation_artifact = _inline_json_artifact(
            "validation_plan",
            "Validation plan",
            validation_plan,
            kind="validation",
        )
        sections.append({
            "id": "validation",
            "title": "Validation plan",
            "summary": _validation_summary(validation_plan),
            "artifacts": [validation_artifact],
        })
        artifacts.append(validation_artifact)

    if backtest_comparison:
        comparison_artifact = _inline_json_artifact(
            "backtest_comparison",
            "Backtest before/after",
            backtest_comparison,
            kind="validation",
        )
        sections.append({
            "id": "backtest_comparison",
            "title": "Backtest before/after",
            "summary": str(backtest_comparison.get("summary") or ""),
            "artifacts": [comparison_artifact],
        })
        artifacts.append(comparison_artifact)

    if audit and audit.get("subagent_output"):
        output_artifact = _inline_json_artifact(
            "subagent_output",
            "Subagent output",
            audit.get("subagent_output"),
            kind="output",
        )
        sections.append({
            "id": "subagent_output",
            "title": "Subagent output",
            "summary": "Final structured answer returned by the tuning subagent.",
            "artifacts": [output_artifact],
        })
        artifacts.append(output_artifact)

    return {
        "run": _process_run_metadata(audit),
        "has_prompt": any(a.get("kind") == "prompt" for a in artifacts),
        "has_inputs": any(a.get("kind") == "input" for a in artifacts),
        "has_outputs": any(a.get("kind") == "output" for a in artifacts),
        "has_generated_docs": any(a.get("kind") == "document" for a in artifacts),
        "has_file_changes": any(a.get("kind") == "change" for a in artifacts),
        "has_validation": any(a.get("kind") == "validation" for a in artifacts),
        "sections": sections,
        "artifacts": artifacts,
    }


def proposal_process_trace(
    proposal: dict[str, Any],
    *,
    validation_plan: dict[str, Any] | None = None,
    backtest_comparison: dict[str, Any] | None = None,
    why_reused: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit = None
    raw_path = str(proposal.get("path") or "")
    if raw_path:
        audit_path = Path(raw_path) / "tuning_audit.json"
        audit = _read_json_file(audit_path) if audit_path.exists() else None
    return _process_trace(
        row=proposal,
        proposal=proposal,
        validation_plan=validation_plan,
        audit=audit,
        backtest_comparison=backtest_comparison,
        why_reused=why_reused,
    )


def _process_run_metadata(audit: dict[str, Any] | None) -> dict[str, Any] | None:
    if not audit:
        return None
    model_calls = [
        row for row in (audit.get("model_calls") or [])
        if isinstance(row, dict)
    ][:8]
    data = {
        "subagent": audit.get("subagent"),
        "tier": audit.get("tier"),
        "provider": audit.get("provider"),
        "model": audit.get("model"),
        "ok": audit.get("ok"),
        "tokens": audit.get("tokens"),
        "usd": audit.get("usd"),
        "wall_ms": audit.get("wall_ms"),
        "model_calls": model_calls,
        "model_metadata_source": audit.get("model_metadata_source"),
        "model_metadata_evidence_ref": audit.get("model_metadata_evidence_ref"),
        "redacted": audit.get("redacted", True),
    }
    if not any(value not in (None, "", [], {}) for value in data.values()):
        return None
    return data


def _proposal_artifacts(proposal: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not proposal:
        return []
    pdir_raw = proposal.get("path")
    if not pdir_raw:
        return []
    pdir = Path(str(pdir_raw))
    if not pdir.exists() or not pdir.is_dir():
        return []
    wanted = {
        "proposal.yml", "target.yml", "rationale.md", "test_plan.md",
        "rollback.md", "diff.patch", "reflection.json", "ranked_seeds.json",
        "signals.json", "selected_assets.json", "provider_capabilities.json",
        "strategy_versions.json", "indicator_state.json", "tuning_run.json",
        "tuning_review.md", "tuning_audit.json",
    }
    paths = [p for p in sorted(pdir.iterdir()) if p.is_file() and p.name in wanted]
    artifacts = [
        _file_artifact(
            path,
            kind=_artifact_kind(path.name),
            metadata={"scope": "proposal"},
        )
        for path in paths[:20]
    ]
    after_root = pdir / "after"
    if after_root.exists() and after_root.is_dir():
        for path in _proposal_after_files(after_root)[:40]:
            rel = path.relative_to(pdir).as_posix()
            workspace_rel = path.relative_to(after_root).as_posix()
            artifacts.append(_file_artifact(
                path,
                kind="change",
                metadata={
                    "scope": "after",
                    "operation": "proposed_write",
                    "proposal_path": rel,
                    "workspace_path": workspace_rel,
                },
            ))
    return artifacts[:60]


def _selected_assets_digest(audit: dict[str, Any]) -> dict[str, Any] | None:
    selected = audit.get("selected_assets")
    if not isinstance(selected, dict):
        payload = audit.get("payload") if isinstance(audit.get("payload"), dict) else {}
        selected = payload.get("selected_assets") if isinstance(payload, dict) else None
    if not isinstance(selected, dict):
        return None
    genes = [
        _asset_digest(row, kind="gene")
        for row in selected.get("genes", [])
        if isinstance(row, dict)
    ][:8]
    capsules = [
        _asset_digest(row, kind="capsule")
        for row in selected.get("capsules", [])
        if isinstance(row, dict)
    ][:8]
    signals = [
        _selection_signal_digest(row)
        for row in selected.get("selection_signals", [])
        if isinstance(row, dict)
    ][:8]
    gdi = selected.get("gdi") if isinstance(selected.get("gdi"), dict) else {}
    if not genes and not capsules and not signals:
        return None
    negative_capsules = [
        row for row in capsules
        if float(row.get("outcome_score") or 0.0) < 0.0
        or str(((row.get("gdi") or {}).get("polarity") or "")).lower() == "negative"
    ]
    return {
        "counts": {
            "genes": len(genes),
            "capsules": len(capsules),
            "negative_capsules": len(negative_capsules),
            "selection_signals": len(signals),
        },
        "gdi": gdi,
        "selection_signals": signals,
        "genes": genes,
        "capsules": capsules,
        "negative_capsules": negative_capsules,
    }


def _optimizer_report_digest(audit: dict[str, Any]) -> dict[str, Any] | None:
    report = audit.get("optimizer_report")
    if not isinstance(report, dict) or not report:
        return None
    candidates = [
        {
            "candidate_id": row.get("candidate_id"),
            "index": row.get("index"),
            "score": row.get("score"),
            "status": row.get("status"),
            "summary": row.get("summary"),
            "accepted_count": row.get("accepted_count"),
            "materialized_count": row.get("materialized_count"),
            "unmaterialized_count": row.get("unmaterialized_count"),
            "dropped_count": row.get("dropped_count"),
            "validation_status": row.get("validation_status"),
            "validation_types": row.get("validation_types") or [],
            "blocked_reasons": row.get("blocked_reasons") or [],
            "risk_flags": row.get("risk_flags") or [],
            "materialized_files": row.get("materialized_files") or [],
            "reasons": row.get("reasons") or [],
            "outcome_feedback": row.get("outcome_feedback") or {},
            "asset_candidate": row.get("asset_candidate") or {},
            "validation_preview": _optimizer_candidate_validation_preview_digest(
                row.get("validation_preview") if isinstance(row.get("validation_preview"), dict) else {},
            ),
            "backtest_preview": _optimizer_candidate_backtest_preview_digest(
                row.get("backtest_preview") if isinstance(row.get("backtest_preview"), dict) else {},
            ),
        }
        for row in report.get("candidates", [])
        if isinstance(row, dict)
    ][:8]
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
        "candidates": candidates,
    }


def _optimizer_candidate_validation_preview_digest(preview: dict[str, Any]) -> dict[str, Any]:
    if not preview:
        return {}
    validation = preview.get("validation") if isinstance(preview.get("validation"), dict) else {}
    blockers = [
        {
            key: value
            for key, value in {
                "code": row.get("code"),
                "message": row.get("message"),
                "path": row.get("path"),
            }.items()
            if value not in (None, "", [], {})
        }
        for row in validation.get("blockers", [])
        if isinstance(row, dict)
    ][:4]
    return {
        key: value
        for key, value in {
            "version": preview.get("version"),
            "status": preview.get("status"),
            "reason": preview.get("reason"),
            "score_delta": preview.get("score_delta"),
            "requested_step_types": _str_list(preview.get("requested_step_types"))[:8],
            "executed_step_types": _str_list(preview.get("executed_step_types"))[:8],
            "deferred_step_types": _str_list(preview.get("deferred_step_types"))[:8],
            "blocked_reasons": _str_list(preview.get("blocked_reasons"))[:8],
            "warning_count": preview.get("warning_count"),
            "blocker_count": preview.get("blocker_count"),
            "evidence_refs": _str_list(preview.get("evidence_refs"))[:8],
            "validation": {
                "ok": validation.get("ok"),
                "blockers": blockers,
            } if validation else {},
        }.items()
        if value not in (None, "", [], {})
    }


def _optimizer_candidate_backtest_preview_digest(preview: dict[str, Any]) -> dict[str, Any]:
    if not preview:
        return {}
    result = preview.get("backtest_result") if isinstance(preview.get("backtest_result"), dict) else {}
    result_digest = {
        key: result.get(key)
        for key in (
            "ok",
            "reason",
            "verdict",
            "coverage_ok",
            "coverage_message",
            "total_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "profit_factor",
            "win_rate_pct",
            "total_trades",
        )
        if result.get(key) not in (None, "", [], {})
    }
    return {
        key: value
        for key, value in {
            "version": preview.get("version"),
            "status": preview.get("status"),
            "reason": preview.get("reason"),
            "score_delta": preview.get("score_delta"),
            "preset": preview.get("preset"),
            "allow_mock": preview.get("allow_mock"),
            "blocked_reasons": _str_list(preview.get("blocked_reasons"))[:8],
            "evidence_refs": _str_list(preview.get("evidence_refs"))[:8],
            "backtest_result": result_digest,
        }.items()
        if value not in (None, "", [], {})
    }


def proposal_why_reused(
    paths,
    proposal: dict[str, Any],
    *,
    audit: dict[str, Any] | None = None,
    validation_plan: dict[str, Any] | None = None,
    backtest_comparison: dict[str, Any] | None = None,
    post_apply_monitor: dict[str, Any] | None = None,
    file_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return a compact operator-facing explanation for reused assets."""

    audit = audit or _audit_for_proposal(paths, proposal)
    selected = _selected_assets_digest(audit or {})
    if not selected:
        return None
    counts = selected.get("counts") if isinstance(selected.get("counts"), dict) else {}
    signals = [
        row for row in selected.get("selection_signals", [])
        if isinstance(row, dict)
    ][:8]
    genes = [
        _reuse_asset_digest(row)
        for row in selected.get("genes", [])
        if isinstance(row, dict)
    ][:6]
    capsules = [
        _reuse_asset_digest(row)
        for row in selected.get("capsules", [])
        if isinstance(row, dict)
    ][:6]
    negative_capsules = [
        _reuse_asset_digest(row)
        for row in selected.get("negative_capsules", [])
        if isinstance(row, dict)
    ][:4]
    change_paths = _proposal_change_paths(proposal, file_changes=file_changes)
    validation = _validation_reuse_digest(
        proposal,
        validation_plan=validation_plan,
        backtest_comparison=backtest_comparison,
    )
    post_apply = _post_apply_reuse_digest(post_apply_monitor)
    trigger_context = _proposal_trigger_context(proposal, signals=signals)
    evidence_refs = _unique_strings([
        *_str_list(proposal.get("evidence_refs")),
        *[
            ref
            for row in [*signals, *genes, *capsules, *negative_capsules]
            for ref in _str_list(row.get("evidence_refs"))
        ],
        *_str_list(validation.get("evidence_refs") if validation else []),
        *_str_list(post_apply.get("evidence_refs") if post_apply else []),
    ])[-16:]
    parts = []
    if counts.get("genes"):
        parts.append(f"{counts.get('genes')} Genes")
    if counts.get("capsules"):
        parts.append(f"{counts.get('capsules')} Capsules")
    if counts.get("negative_capsules"):
        parts.append(f"{counts.get('negative_capsules')} cautionary Capsules")
    summary = "Reused " + ", ".join(parts) if parts else "Reused prior evolution assets"
    if signals:
        summary += f" for {len(signals)} matching trigger signals"
    if change_paths:
        summary += f" and linked them to {len(change_paths)} proposed file changes"
    return {
        "version": "why_reused_v1",
        "summary": summary + ".",
        "counts": counts,
        "trigger_context": trigger_context,
        "selection_signals": signals,
        "genes": genes,
        "capsules": capsules,
        "negative_capsules": negative_capsules,
        "proposal_diff": {
            "change_count": len(change_paths),
            "paths": change_paths[:12],
            "materialized": bool(_proposal_metadata(proposal).get("materialized")) or bool(change_paths),
            "advisory_only": bool(_proposal_metadata(proposal).get("advisory_only")),
        },
        "validation": validation,
        "post_apply": post_apply,
        "evidence_refs": evidence_refs,
    }


def _audit_for_proposal(paths, proposal: dict[str, Any]) -> dict[str, Any] | None:
    pid = str(proposal.get("id") or "")
    strategy_id = _proposal_strategy(proposal)
    run_ids = _strategy_tuning_run_ids(proposal.get("evidence_refs"))
    pdir = Path(str(proposal.get("path") or ""))
    audit_path = pdir / "tuning_audit.json"
    if audit_path.exists():
        audit = _read_json_file(audit_path)
        if isinstance(audit, dict):
            return audit
    audits = _strategy_tuning_audits(paths, strategy_id=strategy_id, limit=120)
    for audit in audits:
        if pid and str(audit.get("proposal_id") or "") == pid:
            return audit
    for audit in audits:
        if str(audit.get("run_id") or "") in run_ids:
            return audit
    return None


def _strategy_tuning_run_ids(refs: Any) -> set[str]:
    out: set[str] = set()
    for ref in _str_list(refs):
        if ref.startswith("strategy_tuning:"):
            run_id = ref.split(":", 1)[1].strip()
            if run_id:
                out.add(run_id)
    return out


def _reuse_asset_digest(row: dict[str, Any]) -> dict[str, Any]:
    gdi = row.get("gdi") if isinstance(row.get("gdi"), dict) else {}
    relevance = gdi.get("relevance") if isinstance(gdi.get("relevance"), dict) else {}
    return {
        key: value
        for key, value in {
            "kind": row.get("kind"),
            "id": row.get("id"),
            "summary": row.get("summary"),
            "signals_match": _str_list(row.get("signals_match")),
            "outcome_score": row.get("outcome_score"),
            "evidence_refs": _str_list(row.get("evidence_refs")),
            "gdi_score": gdi.get("score"),
            "polarity": gdi.get("polarity"),
            "relevance_score": relevance.get("score"),
            "relevance_source": relevance.get("source"),
            "relevance_gene_id": relevance.get("gene_id"),
            "matched_signals": _str_list(relevance.get("matched_signals") or gdi.get("matched_signals")),
            "matched_context": relevance.get("matched_context") if isinstance(relevance.get("matched_context"), dict) else {},
            "rationale": gdi.get("rationale"),
        }.items()
        if value not in (None, "", [], {})
    }


def _proposal_metadata(proposal: dict[str, Any]) -> dict[str, Any]:
    return proposal.get("metadata") if isinstance(proposal.get("metadata"), dict) else {}


def _proposal_trigger_context(
    proposal: dict[str, Any],
    *,
    signals: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata = _proposal_metadata(proposal)
    context = metadata.get("evolution_trigger_context")
    if isinstance(context, dict) and context:
        return {
            key: value
            for key, value in context.items()
            if value not in (None, "", [], {})
        }
    return {
        key: value
        for key, value in {
            "signal_kinds": _unique_strings([
                str(row.get("kind") or "")
                for row in signals
                if row.get("kind")
            ]),
            "evidence_refs": _unique_strings([
                ref
                for row in signals
                for ref in _str_list(row.get("evidence_refs"))
            ])[-12:],
        }.items()
        if value not in (None, "", [], {})
    }


def _proposal_change_paths(
    proposal: dict[str, Any],
    *,
    file_changes: list[dict[str, Any]] | None = None,
) -> list[str]:
    if file_changes:
        return _unique_strings([
            str(row.get("path") or "")
            for row in file_changes
            if isinstance(row, dict) and row.get("path")
        ])
    metadata = _proposal_metadata(proposal)
    materialized = [
        str(path)
        for path in (metadata.get("materialized_files") or [])
        if path
    ]
    if materialized:
        return _unique_strings(materialized)
    pdir_raw = proposal.get("path")
    if not pdir_raw:
        return []
    after_root = Path(str(pdir_raw)) / "after"
    if not after_root.exists() or not after_root.is_dir():
        return []
    return _unique_strings([
        path.relative_to(after_root).as_posix()
        for path in _proposal_after_files(after_root)[:40]
    ])


def _validation_reuse_digest(
    proposal: dict[str, Any],
    *,
    validation_plan: dict[str, Any] | None,
    backtest_comparison: dict[str, Any] | None,
) -> dict[str, Any] | None:
    plan = validation_plan if isinstance(validation_plan, dict) else {}
    comparison = backtest_comparison if isinstance(backtest_comparison, dict) else {}
    if not plan and not comparison and not proposal.get("validation_plan_id"):
        return None
    evidence_refs = _unique_strings([
        *_validation_evidence(plan),
        *_str_list(comparison.get("evidence_refs")),
    ])
    return {
        key: value
        for key, value in {
            "plan_id": proposal.get("validation_plan_id") or plan.get("id"),
            "status": plan.get("status"),
            "summary": _validation_summary(plan) if plan else None,
            "backtest_status": comparison.get("status"),
            "backtest_summary": comparison.get("summary"),
            "evidence_refs": evidence_refs,
        }.items()
        if value not in (None, "", [], {})
    }


def _post_apply_reuse_digest(monitor: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(monitor, dict) or not monitor:
        return None
    weighted = monitor.get("weighted_summary") if isinstance(monitor.get("weighted_summary"), dict) else {}
    observations = monitor.get("observations") if isinstance(monitor.get("observations"), list) else []
    return {
        key: value
        for key, value in {
            "status": monitor.get("status"),
            "summary": monitor.get("summary"),
            "observed_at": monitor.get("observed_at"),
            "observation_count": len(observations),
            "weighted_negative_count": weighted.get("weighted_negative_count"),
            "weighted_healthy_count": weighted.get("weighted_healthy_count"),
            "weighted_observing_count": weighted.get("weighted_observing_count"),
            "evidence_refs": _str_list(monitor.get("evidence_refs")),
        }.items()
        if value not in (None, "", [], {})
    }


def _runtime_feedback_digest(audit: dict[str, Any]) -> dict[str, Any] | None:
    payload = audit.get("payload") if isinstance(audit.get("payload"), dict) else {}
    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    feedback = performance.get("evolution_context") if isinstance(performance.get("evolution_context"), dict) else {}
    if not feedback or int(float(feedback.get("recent_count") or 0)) <= 0:
        return None
    wanted = {
        "post_apply_observation_count",
        "recent_count",
        "by_status",
        "by_source",
        "negative_count",
        "healthy_count",
        "observing_count",
        "weighted_by_status",
        "weighted_by_source",
        "weighted_negative_count",
        "weighted_healthy_count",
        "weighted_observing_count",
        "dominant_sources",
        "last_observed_at",
        "decay",
        "evidence_refs",
        "recent_observations",
    }
    return {
        key: feedback.get(key)
        for key in wanted
        if key in feedback
    }


def _strategy_decision_context_digest(audit: dict[str, Any]) -> dict[str, Any] | None:
    payload = audit.get("payload") if isinstance(audit.get("payload"), dict) else {}
    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    if not performance:
        return None
    digest = {
        "strategy_id": payload.get("strategy_id") or performance.get("strategy_id"),
        "package_hash": performance.get("package_hash"),
        "generated_at": performance.get("generated_at"),
        "lookback_runs": performance.get("lookback_runs"),
        "runs_considered": performance.get("runs_considered"),
        "run_metrics": _pick_keys(
            performance.get("run_metrics"),
            [
                "total", "ok", "hold", "submitted", "error", "ok_rate",
                "hold_rate", "error_rate", "median_duration_ms",
                "p95_duration_ms", "modes",
            ],
        ),
        "trade_metrics": _pick_keys(
            performance.get("trade_metrics"),
            [
                "intents", "orders", "fills", "fill_rate", "pnl_total_usd",
                "max_drawdown_usd", "wins", "losses", "closed", "win_rate",
                "current_win_streak", "current_loss_streak", "avg_slippage",
                "slippage_samples", "paper_live_divergence_bps",
                "paper_live_divergence_samples",
            ],
        ),
        "risk_metrics": _pick_keys(
            performance.get("risk_metrics"),
            [
                "risk_rows", "risk_rejects", "risk_blocks",
                "decision_rows", "decision_holds",
            ],
        ),
        "cost_metrics": _pick_keys(
            performance.get("cost_metrics"),
            ["subagent_invocations", "subagent_by_name"],
        ),
        "market_context": _market_context_digest(performance.get("market_context")),
        "news_context": _news_context_digest(performance.get("news_context")),
        "notes": _str_list(performance.get("notes"))[:8],
    }
    return {
        key: value
        for key, value in digest.items()
        if value not in (None, "", [], {})
    }


def _market_context_digest(value: Any) -> dict[str, Any]:
    context = value if isinstance(value, dict) else {}
    items = [
        _market_item_digest(row)
        for row in context.get("items", [])
        if isinstance(row, dict)
    ][:8]
    return {
        key: value
        for key, value in {
            "timeframe": context.get("timeframe"),
            "markets": _str_list(context.get("markets")),
            "items": items,
            "notes": _str_list(context.get("notes"))[:6],
        }.items()
        if value not in (None, "", [], {})
    }


def _market_item_digest(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "market": row.get("market"),
            "timeframe": row.get("timeframe"),
            "candles_count": row.get("candles_count"),
            "features": row.get("features") if isinstance(row.get("features"), dict) else {},
            "_envelope": row.get("_envelope") if isinstance(row.get("_envelope"), dict) else None,
        }.items()
        if value not in (None, "", [], {})
    }


def _news_context_digest(value: Any) -> dict[str, Any]:
    context = value if isinstance(value, dict) else {}
    items = [
        _news_item_digest(row)
        for row in context.get("items", [])
        if isinstance(row, dict)
    ][:8]
    return {
        key: value
        for key, value in {
            "count": context.get("count"),
            "symbols": _str_list(context.get("symbols")),
            "items": items,
            "errors": _str_list(context.get("errors"))[:8],
            "notes": _str_list(context.get("notes"))[:6],
        }.items()
        if value not in (None, "", [], {})
    }


def _news_item_digest(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "source": row.get("source"),
            "title": row.get("title"),
            "summary": _bounded_text(row.get("summary") or row.get("body"), limit=500),
            "published_at": row.get("published_at") or row.get("ts"),
            "link": row.get("link"),
            "tickers": _str_list(row.get("tickers")),
            "matched_tickers": _str_list(row.get("matched_tickers")),
            "_envelope": row.get("_envelope") if isinstance(row.get("_envelope"), dict) else None,
        }.items()
        if value not in (None, "", [], {})
    }


def _pick_keys(value: Any, keys: list[str]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        key: source.get(key)
        for key in keys
        if source.get(key) not in (None, "", [], {})
    }


def _bounded_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _asset_digest(row: dict[str, Any], *, kind: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": kind,
        "id": row.get("id"),
        "summary": row.get("summary"),
        "evidence_refs": _str_list(row.get("evidence_refs")),
    }
    for key in (
        "category", "signals_match", "strategy_id", "gene_id",
        "outcome_score", "validation_results",
    ):
        if key in row:
            out[key] = row.get(key)
    if isinstance(row.get("gdi"), dict):
        out["gdi"] = _gdi_digest(row.get("gdi") or {})
    return {key: value for key, value in out.items() if value not in (None, "", [])}


def _gdi_digest(gdi: dict[str, Any]) -> dict[str, Any]:
    relevance = gdi.get("relevance") if isinstance(gdi.get("relevance"), dict) else {}
    post_apply = (
        gdi.get("post_apply_weighted")
        if isinstance(gdi.get("post_apply_weighted"), dict)
        else {}
    )
    return {
        key: value
        for key, value in {
            "version": gdi.get("version"),
            "score": gdi.get("score"),
            "polarity": gdi.get("polarity"),
            "components": gdi.get("components") if isinstance(gdi.get("components"), dict) else {},
            "matched_signals": _str_list(gdi.get("matched_signals")),
            "usage_count": gdi.get("usage_count"),
            "post_apply_status": gdi.get("post_apply_status"),
            "post_apply_weighted": _pick_keys(
                post_apply,
                [
                    "count",
                    "weighted_negative_count",
                    "weighted_healthy_count",
                    "weighted_observing_count",
                ],
            ),
            "relevance": _pick_keys(
                relevance,
                [
                    "version", "score", "source", "matched_signals",
                    "trigger_signal_kinds", "gene_id", "matched_context",
                ],
            ),
            "rationale": gdi.get("rationale"),
        }.items()
        if value not in (None, "", [], {})
    }


def _selection_signal_digest(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "severity": row.get("severity"),
        "summary": row.get("summary"),
        "confidence": row.get("confidence"),
        "evidence_refs": _str_list(row.get("evidence_refs")),
    }
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if metadata:
        out["metadata"] = metadata
    return {key: value for key, value in out.items() if value not in (None, "", [])}


def _proposal_after_files(after_root: Path) -> list[Path]:
    text_suffixes = {
        ".cfg", ".conf", ".css", ".csv", ".diff", ".env", ".ini", ".js",
        ".json", ".md", ".mjs", ".patch", ".py", ".toml", ".ts", ".tsx",
        ".txt", ".yaml", ".yml",
    }
    out: list[Path] = []
    for path in sorted(after_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "SKILL.md" or path.suffix.lower() in text_suffixes:
            out.append(path)
    return out


def _generated_docs(
    *,
    row: dict[str, Any],
    audit: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for raw in [
        row.get("review_path"),
        row.get("audit_path"),
        (audit or {}).get("review_path"),
        (audit or {}).get("audit_path"),
    ]:
        if raw:
            path = Path(str(raw))
            if path.exists() and path.is_file() and path not in paths:
                paths.append(path)
    return [_file_artifact(path, kind=_artifact_kind(path.name)) for path in paths[:8]]


def _file_artifact(
    path: Path,
    *,
    kind: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = _read_text_file(path)
    return {
        "id": _safe_artifact_id(path.name),
        "title": path.name,
        "kind": kind,
        "path": str(path),
        "language": _language_for_path(path.name),
        "size": path.stat().st_size if path.exists() else 0,
        "preview": _preview_text(text, path.name),
        "truncated": len(text) > _preview_limit(path.name),
        "redacted": True,
        "metadata": metadata or {},
    }


def _inline_artifact(
    aid: str,
    title: str,
    content: str,
    *,
    kind: str,
    language: str,
    path: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe = redact_text(content or "")
    return {
        "id": aid,
        "title": title,
        "kind": kind,
        "path": path,
        "language": language,
        "size": len(safe),
        "preview": _preview_text(safe, title),
        "truncated": len(safe) > _preview_limit(title),
        "metadata": metadata or {},
        "redacted": True,
    }


def _inline_json_artifact(
    aid: str,
    title: str,
    data: Any,
    *,
    kind: str,
) -> dict[str, Any]:
    safe = redact_display_dict(data)
    text = json.dumps(safe, ensure_ascii=False, indent=2, default=str)
    return _inline_artifact(
        aid,
        title,
        text,
        kind=kind,
        language="json",
        metadata={"redacted": True},
    )


def _read_text_file(path: Path) -> str:
    try:
        return redact_text(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _preview_limit(name: str) -> int:
    lowered = name.lower()
    if "optimizer" in lowered:
        return 24000
    if "prompt" in lowered or "audit" in lowered or name.endswith(".json"):
        return 12000
    return 8000


def _preview_text(text: str, name: str = "") -> str:
    limit = _preview_limit(name)
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _language_for_path(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith((".yml", ".yaml")):
        return "yaml"
    if lowered.endswith(".json"):
        return "json"
    if lowered.endswith(".md"):
        return "markdown"
    if lowered.endswith(".patch"):
        return "diff"
    return "text"


def _artifact_kind(name: str) -> str:
    lowered = name.lower()
    if "audit" in lowered or "prompt" in lowered:
        return "prompt"
    if lowered in {"proposal.yml", "target.yml"}:
        return "proposal"
    if lowered in {"rationale.md", "test_plan.md", "rollback.md", "tuning_review.md"}:
        return "document"
    if "validation" in lowered or lowered == "test_plan.md":
        return "validation"
    if lowered.endswith(".json"):
        return "input"
    return "document"


def _safe_artifact_id(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_") or "artifact"


def _audit_for_refs(
    audits_by_run: dict[str, dict[str, Any]],
    refs: Any,
) -> dict[str, Any] | None:
    for ref in _str_list(refs):
        if ref.startswith("strategy_tuning:"):
            run_id = ref.split(":", 1)[1]
            if run_id in audits_by_run:
                return audits_by_run[run_id]
    return None


def _event_validation_plan(
    row: dict[str, Any],
    proposal: dict[str, Any] | None,
    plans_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    plan_id = (
        metadata.get("validation_plan_id")
        or (proposal or {}).get("validation_plan_id")
        or row.get("validation_plan_id")
    )
    return plans_by_id.get(str(plan_id or ""))


def _config_snapshot(config, *, strategy_id: str | None = None) -> dict[str, Any]:
    from .periodic_reflection import get_periodic_reflection
    return {
        "periodic_reflection": get_periodic_reflection(config.paths),
    }


def _summary(
    *,
    items: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    events: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    validation_plans: list[dict[str, Any]],
) -> dict[str, Any]:
    terminal_outcomes = {"applied", "rejected", "rolled_back"}
    latest = max((str(item.get("ts") or "") for item in items), default="")
    return {
        "signals": len(signals),
        "events": len(events),
        "assets": len(assets),
        "capsules": sum(1 for row in assets if row.get("kind") == "capsule"),
        "candidates": len(candidates),
        "blocked_candidates": sum(
            1 for row in candidates
            if row.get("blocked_reasons") or row.get("safe_to_promote") is False
        ),
        "proposals": len(proposals),
        "open_proposals": sum(
            1 for row in proposals
            if str(row.get("state") or "") in _OPEN_PROPOSAL_STATES
        ),
        "validation_plans": len(validation_plans),
        "blocked_validation_plans": sum(
            1 for row in validation_plans
            if row.get("blocked_reasons")
        ),
        "terminal_outcomes": sum(
            1 for row in events
            if str(row.get("outcome") or "") in terminal_outcomes
        ),
        "timeline_items": len(items),
        "last_activity_ts": latest or None,
    }


def _build_inbox(items: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {
        group["id"]: {
            **group,
            "items": [],
            "count": 0,
        }
        for group in _INBOX_GROUPS
    }
    for item in items:
        group_id = _inbox_group_for_item(item)
        if not group_id:
            continue
        entry = _inbox_entry(item, group_id)
        groups[group_id]["items"].append(entry)

    ordered = []
    for group in _INBOX_GROUPS:
        row = groups[group["id"]]
        row["items"].sort(key=lambda entry: str(entry.get("ts") or ""), reverse=True)
        row["count"] = len(row["items"])
        ordered.append(row)
    return {
        "total": sum(group["count"] for group in ordered),
        "groups": ordered,
    }


def _inbox_group_for_item(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type") or "")
    status = str(item.get("status") or "").lower()
    outcome = str(item.get("outcome") or "").lower()
    validation_status = str(item.get("validation_status") or "").lower()

    if status in _TERMINAL_NEGATIVE_STATES or outcome in _TERMINAL_NEGATIVE_STATES:
        return "negative_learning"
    if item_type == "asset" and _numeric(item.get("outcome_score")) < 0:
        return "negative_learning"
    post_apply_status = _post_apply_status_for_item(item)
    if post_apply_status in _POST_APPLY_NEGATIVE_STATES:
        return "negative_learning"
    if post_apply_status in _POST_APPLY_HEALTHY_STATES:
        return "reusable_learning"
    if status == "applied" or outcome == "applied":
        return "monitoring"
    if item_type == "asset":
        return "reusable_learning"
    if item_type == "asset_candidate" and _candidate_safe_to_promote(item):
        return "reusable_learning"

    if _needs_evidence(item):
        return "needs_evidence"
    if _needs_materialization(item):
        return "needs_materialization"
    if _needs_validation(item, status=status, validation_status=validation_status):
        return "needs_validation"
    if _needs_approval(item, status=status, validation_status=validation_status):
        return "needs_approval"
    return None


def _inbox_entry(item: dict[str, Any], group_id: str) -> dict[str, Any]:
    return {
        "id": f"{group_id}:{item.get('id') or item.get('record_id')}",
        "item_id": item.get("id"),
        "record_id": item.get("record_id"),
        "type": item.get("type"),
        "stage": item.get("stage"),
        "status": item.get("status"),
        "title": item.get("title") or "",
        "summary": item.get("summary") or item.get("why") or "",
        "ts": item.get("ts") or "",
        "strategy_id": item.get("strategy_id"),
        "proposal_id": item.get("proposal_id"),
        "validation_plan_id": item.get("validation_plan_id"),
        "post_apply_monitor": item.get("post_apply_monitor"),
        "evidence_refs": _str_list(item.get("evidence_refs")),
        "reasons": _inbox_reasons(item, group_id),
        "next_step": item.get("next_step") or "",
    }


def _inbox_reasons(item: dict[str, Any], group_id: str) -> list[str]:
    if group_id == "needs_evidence":
        return ["no_resolvable_evidence_refs_recorded"]
    if group_id == "needs_materialization":
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        if isinstance((raw or {}).get("metadata"), dict) and raw["metadata"].get("advisory_only"):
            return ["advisory_only_no_applyable_after_files"]
        return ["mutation_proposal_has_no_after_files"]
    if group_id == "needs_validation":
        reasons: list[str] = []
        if not item.get("validation_plan_id"):
            reasons.append("missing_validation_plan")
        if item.get("validation_status"):
            reasons.append(f"validation_status:{item.get('validation_status')}")
        reasons.extend(_str_list(item.get("blocked_reasons")))
        return reasons or ["validation_not_passed"]
    if group_id == "needs_approval":
        state = str(item.get("status") or "")
        if state == "approved":
            return ["approved_waiting_for_governed_apply"]
        return ["validated_proposal_waiting_for_operator_review"]
    if group_id == "monitoring":
        status = _post_apply_status_for_item(item)
        if status == "pending":
            return ["post_apply_observation_pending"]
        if status:
            return [f"post_apply_observation:{status}"]
        return ["post_apply_observation_pending"]
    if group_id == "reusable_learning":
        status = _post_apply_status_for_item(item)
        if status in _POST_APPLY_HEALTHY_STATES:
            return [f"post_apply_observation:{status}"]
        if str(item.get("type") or "") == "asset_candidate":
            return ["safe_candidate_waiting_for_promotion"]
        return ["promoted_learning_available_for_reuse"]
    if group_id == "negative_learning":
        status = _post_apply_status_for_item(item)
        if status in _POST_APPLY_NEGATIVE_STATES:
            return [f"post_apply_observation:{status}"]
        return ["rejected_or_rolled_back_outcome_should_downweight_future_reuse"]
    return []


def _needs_evidence(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "") not in {"signal", "event", "proposal", "asset_candidate"}:
        return False
    if _str_list(item.get("evidence_refs")):
        return False
    if str(item.get("status") or "").lower() in _TERMINAL_NEGATIVE_STATES:
        return False
    return True


def _needs_materialization(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "") != "proposal":
        return False
    status = str(item.get("status") or "").lower()
    if status not in _OPEN_PROPOSAL_STATES:
        return False
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    kind = str((raw or {}).get("kind") or "").lower()
    if kind not in _MATERIALIZED_PROPOSAL_KINDS:
        return False
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    process = item.get("process") if isinstance(item.get("process"), dict) else {}
    if metadata.get("advisory_only") or metadata.get("materialized") is False:
        return True
    return not bool((process or {}).get("has_file_changes"))


def _needs_validation(
    item: dict[str, Any],
    *,
    status: str,
    validation_status: str,
) -> bool:
    item_type = str(item.get("type") or "")
    if item_type == "validation":
        return status not in _PASSED_VALIDATION_STATES
    if item_type != "proposal":
        return False
    if status not in _OPEN_PROPOSAL_STATES:
        return False
    if not item.get("validation_plan_id"):
        return True
    return validation_status not in _PASSED_VALIDATION_STATES


def _needs_approval(
    item: dict[str, Any],
    *,
    status: str,
    validation_status: str,
) -> bool:
    if str(item.get("type") or "") != "proposal":
        return False
    if status not in _OPEN_PROPOSAL_STATES:
        return False
    if status == "approved":
        return True
    return validation_status in _PASSED_VALIDATION_STATES


def _candidate_safe_to_promote(item: dict[str, Any]) -> bool:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    return bool((raw or {}).get("safe_to_promote"))


def _numeric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _proposal_matches_strategy(row: dict[str, Any], strategy_id: str) -> bool:
    if _proposal_strategy(row) == strategy_id:
        return True
    blob = _search_blob(row)
    return f"strategies/{strategy_id}/" in blob or f"strategy:{strategy_id}" in blob


def _proposal_strategy(row: dict[str, Any]) -> str | None:
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    direct = row.get("strategy_id") or meta.get("strategy_id")
    if direct:
        return str(direct)
    target = str(row.get("target") or "")
    parts = target.replace("\\", "/").split("/")
    if "strategies" in parts:
        idx = parts.index("strategies")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _post_apply_status_for_item(item: dict[str, Any]) -> str:
    monitor = item.get("post_apply_monitor")
    if not isinstance(monitor, dict):
        return ""
    return str(monitor.get("status") or "").lower()


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if value:
            out.setdefault(value, []).append(row)
    return out


def _stage_for_outcome(outcome: str) -> str:
    if outcome == "proposed":
        return "proposal"
    if outcome in {"approved", "applied", "rejected", "rolled_back"}:
        return "outcome"
    return "reflection"


def _why_for_event(row: dict[str, Any]) -> str:
    signals = _str_list(row.get("signals"))
    if signals:
        return f"Linked to {len(signals)} signal(s): {', '.join(signals[:3])}."
    return row.get("summary") or "Evolution runtime recorded an outcome."


def _next_step_for_signal(severity: str) -> str:
    if severity == "critical":
        return "Run reflection and open a proposal before the next risky change."
    if severity == "warn":
        return "Review matching genes and consider a focused proposal."
    return "Keep collecting evidence until a repeated pattern appears."


def _next_step_for_event(outcome: str, validation_status: Any) -> str:
    if outcome == "candidate":
        return "Attach validation evidence or convert the finding into a proposal."
    if outcome == "proposed":
        return "Inspect the proposal, validation plan, and rollback notes."
    if outcome == "applied":
        return "Confirm the promoted change becomes a reusable capsule."
    if outcome in {"rejected", "rolled_back"}:
        return "Capture the negative outcome so future candidates are less likely."
    if validation_status == "failed":
        return "Fix validation failures before any approval."
    return "Review linked evidence and decide whether this should become an asset."


def _next_step_for_proposal(
    state: str,
    has_plan: bool,
    *,
    post_apply_monitor: dict[str, Any] | None = None,
) -> str:
    if state in {"applied", "rolled_back", "rejected"}:
        monitor_status = str((post_apply_monitor or {}).get("status") or "").lower()
        if state == "applied" and monitor_status in _POST_APPLY_NEGATIVE_STATES:
            return "Inspect the post-apply regression and decide whether to rollback."
        if state == "applied" and monitor_status in _POST_APPLY_HEALTHY_STATES:
            return "Keep the verified outcome available for future reuse."
        if state == "applied":
            return "Attach post-apply paper/live/backtest evidence before calling the evolution successful."
        return "No operator action is pending; keep the outcome for history."
    if not has_plan:
        return "Add or generate a validation plan before approval."
    if state == "approved":
        return "Apply only through the governed proposal workflow."
    return "Run validation dry-run, then review in the Action Inbox."


def _validation_summary(row: dict[str, Any]) -> str:
    steps = row.get("steps") if isinstance(row.get("steps"), list) else []
    required = sum(1 for step in steps if isinstance(step, dict) and step.get("required", True))
    return f"{len(steps)} validation step(s), {required} required."


def _validation_evidence(row: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for step in row.get("steps") or []:
        if isinstance(step, dict) and step.get("evidence_ref"):
            refs.append(str(step["evidence_ref"]))
    return refs


def _title(text: str) -> str:
    return " ".join(part.capitalize() for part in str(text or "").replace("_", " ").split())


def _proposal_title(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "evolution").replace("_", " ").strip()
    if kind.lower().endswith("proposal"):
        return _title(kind)
    return _title(f"{kind} proposal")


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


def _search_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str).lower()
    except Exception:
        return str(value).lower()


__all__ = ["build_timeline"]
