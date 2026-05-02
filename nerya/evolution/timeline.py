"""Product-shaped timeline for Nerya self-evolution telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core import yaml_io
from ..core.redaction import redact_display_dict, redact_text
from . import assets as evolution_assets
from .event_store import list_events, list_signals
from .patch_proposal import list_proposals
from .validation_plan import ALLOWED_STEP_TYPES


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
    read_limit = max(capped, 250)

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
    strategy_audits = _strategy_tuning_audits(paths, strategy_id=strategy_id)

    proposals_by_id = {str(p.get("id") or ""): p for p in proposals}
    plans_by_id = {str(p.get("id") or ""): p for p in validation_plans}
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
        items.append(_proposal_item(
            row,
            plans_by_id.get(str(row.get("validation_plan_id") or "")),
            audit=(
                audits_by_proposal.get(str(row.get("id") or ""))
                or _audit_for_refs(audits_by_run, row.get("evidence_refs"))
            ),
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

    return {
        "ok": True,
        "timeline": items,
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
    return {
        "id": f"event:{eid}",
        "record_id": eid,
        "type": "event",
        "stage": _stage_for_outcome(outcome),
        "ts": row.get("ts") or "",
        "title": _title(f"{outcome} evolution event"),
        "summary": row.get("summary") or "",
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
        "raw": row,
    }


def _proposal_item(
    row: dict[str, Any],
    validation_plan: dict[str, Any] | None,
    *,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pid = str(row.get("id") or "")
    state = str(row.get("state") or "draft")
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
        "evidence_refs": _str_list(row.get("evidence_refs")),
        "why": row.get("summary") or "Proposal was created from evolution evidence.",
        "next_step": _next_step_for_proposal(state, bool(row.get("validation_plan_id"))),
        "process": _process_trace(
            row=row,
            proposal=row,
            validation_plan=validation_plan,
            audit=audit,
        ),
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


def _strategy_tuning_audits(paths, *, strategy_id: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in jsonl.read_all(paths.journal("strategy_evolution")):
        if row.get("kind") != "strategy.tuning":
            continue
        sid = str(row.get("strategy_id") or "")
        if strategy_id and sid != strategy_id:
            continue
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
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
                "prompt_path", "role_prompt", "payload", "prompt_records",
                "subagent_output", "metrics", "steps", "redacted",
            ):
                if key in audit_json:
                    merged[key] = audit_json[key]
        rows.append(merged)
    return rows


def _process_trace(
    *,
    row: dict[str, Any],
    proposal: dict[str, Any] | None = None,
    validation_plan: dict[str, Any] | None = None,
    audit: dict[str, Any] | None = None,
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
        "has_prompt": any(a.get("kind") == "prompt" for a in artifacts),
        "has_inputs": any(a.get("kind") == "input" for a in artifacts),
        "has_outputs": any(a.get("kind") == "output" for a in artifacts),
        "has_generated_docs": any(a.get("kind") == "document" for a in artifacts),
        "has_validation": any(a.get("kind") == "validation" for a in artifacts),
        "sections": sections,
        "artifacts": artifacts,
    }


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
    return [
        _file_artifact(path, kind=_artifact_kind(path.name))
        for path in paths[:20]
    ]


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


def _file_artifact(path: Path, *, kind: str) -> dict[str, Any]:
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
    tuning = _strategy_tuning_snapshot(config.paths, strategy_id=strategy_id)
    return {
        "hooks": {
            "enabled": bool(config.get("agent.native.evolution_hooks_enabled", True)),
            "sources": ["tool", "turn", "memory", "session"],
        },
        "signal_collection": {
            "manual_refresh_endpoint": "/evolution/signals",
            "reflection_endpoint": "/evolution/reflect",
            "dedupe_window": 500,
        },
        "memory_quality_gate": {
            "enabled": True,
            "minimum_score": 0.55,
            "requires_evidence_refs": True,
            "blocks_possible_secrets": True,
        },
        "validation": {
            "dry_run_only": True,
            "execution_enabled": False,
            "allowed_step_types": sorted(ALLOWED_STEP_TYPES),
        },
        "strategy_tuning": tuning,
    }


def _strategy_tuning_snapshot(
    paths,
    *,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    root = paths.strategies
    if root.exists():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if strategy_id and child.name != strategy_id:
                continue
            raw = yaml_io.load(child / "strategy.yml", default={}) or {}
            tuning = raw.get("tuning") if isinstance(raw, dict) else {}
            if not isinstance(tuning, dict):
                tuning = {}
            schedule = tuning.get("schedule") if isinstance(tuning.get("schedule"), dict) else {}
            rows.append({
                "strategy_id": child.name,
                "enabled": bool(tuning.get("enabled", False)),
                "schedule": schedule,
                "objectives": tuning.get("objectives") or [],
                "guardrails": tuning.get("guardrails") or {},
                "lookback": tuning.get("lookback") or {},
            })
    return {
        "total_strategies": len(rows),
        "enabled_strategies": sum(1 for row in rows if row.get("enabled")),
        "strategies": rows[:50],
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
    open_states = {"draft", "pending_review", "proposed", "approved"}
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
            if str(row.get("state") or "") in open_states
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


def _next_step_for_proposal(state: str, has_plan: bool) -> str:
    if state in {"applied", "rolled_back", "rejected"}:
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


def _search_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str).lower()
    except Exception:
        return str(value).lower()


__all__ = ["build_timeline"]
