"""Apply an approved proposal. Only allowed for non-protected scopes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..core import jsonl, yaml_io
from ..core.errors import ProtectedScopeViolation
from ..core.paths import WorkspacePaths
from .patch_proposal import list_proposals, set_state, is_protected


MATERIALIZED_PROPOSAL_KINDS = {
    "strategy_tuning_proposal",
    "strategy_package_proposal",
    "strategy_config_patch",
    "script_proposal",
    "skill_proposal",
    "trigger_route_patch",
}

PASSED_VALIDATION_STATES = {"passed", "safe", "ready", "ok"}


def apply_proposal(paths: WorkspacePaths, pid: str) -> dict[str, Any]:
    prop = next((p for p in list_proposals(paths) if p.id == pid), None)
    if prop is None:
        return {"ok": False, "reason": "not_found"}
    if prop.state != "approved":
        return {"ok": False, "reason": f"state_{prop.state}"}

    target = prop.path / "target.yml"   # optional declared target
    meta_target = None
    if target.exists():
        decl = yaml_io.load(target, default={}) or {}
        meta_target = decl.get("target")

    if meta_target and is_protected(meta_target):
        raise ProtectedScopeViolation(f"cannot apply to protected scope: {meta_target}")

    # Back up any file the proposal brings as `after/<path>` into evolution/artifacts/<pid>/before/
    after_dir = prop.path / "after"
    artifacts = paths.evolution / "artifacts" / pid
    gates = proposal_action_gates(paths, prop)
    after_files = _after_files(after_dir)
    if not gates.get("can_apply"):
        blockers = list(gates.get("blockers") or [])
        reason = str(blockers[0] if blockers else "proposal_gate_blocked")
        return {
            "ok": False,
            "proposal_id": pid,
            "reason": reason,
            "kind": prop.kind,
            "state": prop.state,
            "action_gates": gates,
        }
    applied_files: list[str] = []
    if after_dir.exists():
        (artifacts / "before").mkdir(parents=True, exist_ok=True)
        (artifacts / "after").mkdir(parents=True, exist_ok=True)
        for src in after_files:
            rel = src.relative_to(after_dir)
            rel_posix = rel.as_posix()
            if is_protected(rel_posix):
                raise ProtectedScopeViolation(
                    f"proposal {pid} tries to write protected path: {rel_posix}"
                )
            dst = paths.root / rel
            if dst.exists():
                before_dst = artifacts / "before" / rel
                before_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, before_dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            after_dst = artifacts / "after" / rel
            after_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, after_dst)
            applied_files.append(rel_posix)

    set_state(paths, pid, "applied", note="applied by operator")
    version_records = _record_strategy_versions(paths, prop, applied_files)
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.applied",
        "proposal_id": pid,
        "artifacts": str(artifacts),
        "strategy_versions": version_records,
    })
    try:
        from .assets import record_capsule_from_proposal
        from .event_store import record_event

        capsule = record_capsule_from_proposal(paths, pid, outcome_score=1.0)
        record_event(
            paths,
            parent_id=prop.source_event_id,
            proposal_id=pid,
            mutation_scope=[prop.target] if prop.target else [],
            validation_status="passed" if capsule else "not_run",
            outcome="applied",
            outcome_score=1.0,
            summary=f"Proposal {pid} applied.",
            evidence_refs=list(prop.evidence_refs or []),
            metadata={
                "artifacts": str(artifacts),
                "capsule": capsule,
                "applied_files": applied_files,
                "strategy_versions": version_records,
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "proposal_id": pid,
        "artifacts": str(artifacts),
        "applied_files": applied_files,
        "strategy_versions": version_records,
        "action_gates": gates,
    }


def proposal_action_gates(paths: WorkspacePaths, proposal_or_id: Any) -> dict[str, Any]:
    """Return the hard action gates for applying an evolution proposal."""

    prop = proposal_or_id
    if isinstance(proposal_or_id, str):
        prop = next((p for p in list_proposals(paths) if p.id == proposal_or_id), None)
    if prop is None:
        return {
            "version": "proposal_action_gates_v1",
            "can_apply": False,
            "blockers": ["proposal_not_found"],
            "warnings": [],
        }
    metadata = prop.metadata or {}
    after_files = _after_files(prop.path / "after")
    materialized_required = prop.kind in MATERIALIZED_PROPOSAL_KINDS
    blockers: list[str] = []
    warnings: list[str] = []
    if prop.state != "approved":
        blockers.append(f"state_{prop.state}")
    validation = _proposal_validation_gate(paths, prop, required=materialized_required)
    evidence_refs = _unique_strings([
        *list(prop.evidence_refs or []),
        *[
            str(ref)
            for ref in (validation.get("evidence_refs") or [])
            if ref
        ],
    ])
    if materialized_required:
        if metadata.get("advisory_only"):
            blockers.append("advisory_only")
        if not after_files:
            blockers.append("no_materialized_changes")
        if not evidence_refs:
            blockers.append("missing_evidence_refs")
        if not validation.get("ok"):
            reason = str(validation.get("reason") or "validation_not_passed")
            blockers.append(reason)
    if prop.kind == "strategy_tuning_proposal":
        strategy_id = str(metadata.get("strategy_id") or "").strip()
        expected_hash = str(metadata.get("package_hash") or "").strip()
        if not strategy_id:
            blockers.append("missing_strategy_id")
        if not expected_hash:
            blockers.append("missing_strategy_package_hash")
        if strategy_id and expected_hash:
            try:
                from ..strategies.package import load_package

                current_hash = load_package(paths, strategy_id).content_hash
            except Exception:
                current_hash = ""
            if current_hash != expected_hash:
                blockers.append("strategy_package_changed")
    return {
        "version": "proposal_action_gates_v1",
        "can_apply": not blockers,
        "blockers": _unique_strings(blockers),
        "warnings": _unique_strings(warnings),
        "state": prop.state,
        "kind": prop.kind,
        "materialization": {
            "required": materialized_required,
            "after_file_count": len(after_files),
            "paths": [
                src.relative_to(prop.path / "after").as_posix()
                for src in after_files[:20]
            ],
            "advisory_only": bool(metadata.get("advisory_only")),
        },
        "evidence": {
            "required": materialized_required,
            "count": len(evidence_refs),
            "refs": evidence_refs[:20],
        },
        "validation": validation,
    }


def _after_files(after_dir: Path) -> list[Path]:
    if not after_dir.exists() or not after_dir.is_dir():
        return []
    return [src for src in sorted(after_dir.rglob("*")) if src.is_file()]


def _proposal_validation_gate(
    paths: WorkspacePaths,
    prop,
    *,
    required: bool = True,
) -> dict[str, Any]:
    plan_id = str(prop.validation_plan_id or "").strip()
    if plan_id:
        plan = _load_validation_plan(paths, plan_id)
        if not isinstance(plan, dict):
            return {
                "ok": False,
                "required": required,
                "source": "validation_plan",
                "plan_id": plan_id,
                "reason": "validation_plan_not_found",
            }
        return _validation_plan_gate(plan, required=required)
    report = _load_validation_report(prop.path)
    if isinstance(report, dict):
        ok = bool(report.get("ok")) and not _validation_report_blockers(report)
        return {
            "ok": ok,
            "required": required,
            "source": "validation_report",
            "status": "passed" if ok else "failed",
            "reason": None if ok else "validation_report_failed",
            "evidence_refs": [f"file:{prop.path / 'validation_report.json'}"],
            "report": {
                "ok": report.get("ok"),
                "blockers": _validation_report_blockers(report)[:12],
                "warnings": _validation_report_warnings(report)[:12],
            },
        }
    return {
        "ok": not required,
        "required": required,
        "source": "none",
        "status": "missing",
        "reason": "missing_validation_evidence" if required else None,
    }


def _load_validation_plan(paths: WorkspacePaths, plan_id: str) -> dict[str, Any] | None:
    path = paths.evolution_validation_plans / f"{plan_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _validation_plan_gate(plan: dict[str, Any], *, required: bool) -> dict[str, Any]:
    status = str(plan.get("status") or "not_run").lower()
    blocked_reasons = [
        str(reason)
        for reason in (plan.get("blocked_reasons") or [])
        if reason
    ]
    required_steps = [
        step for step in (plan.get("steps") or [])
        if isinstance(step, dict) and bool(step.get("required", True))
    ]
    failed_required = [
        f"step_{idx}:{step.get('status') or 'not_run'}"
        for idx, step in enumerate(required_steps)
        if str(step.get("status") or "not_run").lower() not in PASSED_VALIDATION_STATES
    ]
    missing_evidence = [
        f"step_{idx}:{step.get('type') or 'unknown'}"
        for idx, step in enumerate(required_steps)
        if not str(step.get("evidence_ref") or "").strip()
    ]
    ok = (
        not blocked_reasons
        and status in PASSED_VALIDATION_STATES
        and not failed_required
        and not missing_evidence
    )
    reason = None
    if blocked_reasons:
        reason = "validation_plan_blocked"
    elif failed_required:
        reason = "validation_required_steps_not_passed"
    elif missing_evidence:
        reason = "validation_required_steps_missing_evidence"
    elif status not in PASSED_VALIDATION_STATES:
        reason = f"validation_not_passed:{status}"
    return {
        "ok": ok or not required,
        "required": required,
        "source": "validation_plan",
        "plan_id": plan.get("id"),
        "status": status,
        "reason": reason if required else None,
        "blocked_reasons": blocked_reasons,
        "failed_required_steps": failed_required,
        "missing_evidence_steps": missing_evidence,
        "evidence_refs": [
            str(step.get("evidence_ref"))
            for step in required_steps
            if str(step.get("evidence_ref") or "").strip()
        ],
    }


def _load_validation_report(proposal_path: Path) -> dict[str, Any] | None:
    path = proposal_path / "validation_report.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _validation_report_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or issue.get("level") or "").lower()
        code = str(issue.get("code") or issue.get("message") or "issue")
        if severity in {"error", "blocker", "critical"}:
            blockers.append(code)
    for value in report.get("blockers") or []:
        if value:
            blockers.append(str(value))
    return _unique_strings(blockers)


def _validation_report_warnings(report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or issue.get("level") or "").lower()
        code = str(issue.get("code") or issue.get("message") or "issue")
        if severity in {"warning", "warn"}:
            warnings.append(code)
    for value in report.get("warnings") or []:
        if value:
            warnings.append(str(value))
    return _unique_strings(warnings)


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _record_strategy_versions(
    paths: WorkspacePaths,
    prop,
    applied_files: list[str],
) -> list[dict[str, Any]]:
    strategy_ids = _strategy_ids_from_applied_files(applied_files)
    if not strategy_ids:
        return []
    records: list[dict[str, Any]] = []
    for strategy_id in strategy_ids:
        try:
            from ..strategies.package import load_package
            from ..strategies.state import StrategyVersionRegistry

            package = load_package(paths, strategy_id)
            record = StrategyVersionRegistry(paths, strategy_id).record(
                package,
                promoted_by="proposal_apply",
                proposal_id=prop.id,
                notes=f"Applied {prop.kind} via evolution proposal.",
            )
            records.append(record.asdict())
        except Exception:
            continue
    return records


def _strategy_ids_from_applied_files(applied_files: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in applied_files:
        parts = str(raw or "").replace("\\", "/").split("/")
        if len(parts) < 3 or parts[0] != "strategies":
            continue
        strategy_id = parts[1].strip()
        if not strategy_id or strategy_id in seen:
            continue
        seen.add(strategy_id)
        out.append(strategy_id)
    return out
