"""Structured validation plans for proposal-first evolution."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.atomic_write import atomic_write_text
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .asset_policy import validate_validation_command


ValidationStepType = Literal[
    "unit_test",
    "static_check",
    "backtest",
    "shadow_run",
    "canary",
    "manual_review",
]

ALLOWED_STEP_TYPES = {
    "unit_test",
    "static_check",
    "backtest",
    "shadow_run",
    "canary",
    "manual_review",
}


@dataclass(frozen=True)
class ValidationStep:
    type: ValidationStepType
    command: str | None = None
    required: bool = True
    status: str = "not_run"
    evidence_ref: str | None = None
    notes: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationPlan:
    id: str
    source: str
    created_at: str
    steps: list[ValidationStep]
    proposal_id: str | None = None
    strategy_id: str | None = None
    status: str = "not_run"
    blocked_reasons: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "steps": [s.asdict() for s in self.steps],
            "safe_to_run": not self.blocked_reasons,
        }


def build_validation_plan(
    raw: Any,
    *,
    source: str,
    proposal_id: str | None = None,
    strategy_id: str | None = None,
    require: bool = False,
) -> ValidationPlan:
    steps, reasons = _coerce_steps(raw)
    if require and not steps:
        reasons.append("validation_plan_required")
    for step in steps:
        if step.command:
            policy = validate_validation_command(step.command)
            reasons.extend(policy.reasons)
    return ValidationPlan(
        id=new_id("vpl"),
        source=source,
        created_at=now_iso(),
        steps=steps,
        proposal_id=proposal_id,
        strategy_id=strategy_id,
        status="blocked" if reasons else "not_run",
        blocked_reasons=reasons,
    )


def write_validation_plan(paths: WorkspacePaths, plan: ValidationPlan) -> str:
    out = paths.evolution_validation_plans / f"{plan.id}.json"
    atomic_write_text(out, json.dumps(plan.asdict(), indent=2, ensure_ascii=False, default=str))
    return plan.id


def load_validation_plan(paths: WorkspacePaths, plan_id: str) -> dict[str, Any] | None:
    path = paths.evolution_validation_plans / f"{plan_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def validate_plan_record(plan: dict[str, Any]) -> dict[str, Any]:
    blocked: list[str] = []
    for idx, raw in enumerate(plan.get("steps") or []):
        if not isinstance(raw, dict):
            blocked.append(f"step_{idx}_not_object")
            continue
        step_type = str(raw.get("type") or "")
        if step_type not in ALLOWED_STEP_TYPES:
            blocked.append(f"step_{idx}_type_not_allowed:{step_type}")
        command = str(raw.get("command") or "").strip()
        if command:
            blocked.extend(validate_validation_command(command).reasons)
    return {
        "ok": not blocked,
        "safe_to_run": not blocked,
        "blocked_reasons": blocked,
        "status": "not_run" if not blocked else "blocked",
    }


def run_validation_plan(
    paths: WorkspacePaths,
    *,
    plan_id: str | None = None,
    proposal_id: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    plan = None
    if plan_id:
        plan = load_validation_plan(paths, plan_id)
    elif proposal_id:
        plan = _plan_for_proposal(paths, proposal_id)
    if plan is None:
        return {"ok": False, "reason": "not_found", "plan_id": plan_id, "proposal_id": proposal_id}
    checked = validate_plan_record(plan)
    if dry_run or not checked["safe_to_run"]:
        return {"ok": checked["safe_to_run"], "dry_run": True, "plan": plan, **checked}
    # The API deliberately does not execute shell commands yet. Actual
    # execution belongs in an operator-approved validation runner.
    return {
        "ok": False,
        "dry_run": False,
        "reason": "execution_not_enabled",
        "plan": plan,
        **checked,
    }


def _plan_for_proposal(paths: WorkspacePaths, proposal_id: str) -> dict[str, Any] | None:
    meta_path = paths.proposals / proposal_id / "proposal.yml"
    if not meta_path.exists():
        return None
    from ..core import yaml_io

    meta = yaml_io.load(meta_path, default={}) or {}
    plan_id = meta.get("validation_plan_id")
    if not plan_id:
        return None
    return load_validation_plan(paths, str(plan_id))


def _coerce_steps(raw: Any) -> tuple[list[ValidationStep], list[str]]:
    reasons: list[str] = []
    steps: list[ValidationStep] = []
    if raw is None:
        return steps, reasons
    if isinstance(raw, dict) and "steps" in raw:
        raw = raw.get("steps")
    if not isinstance(raw, list):
        return steps, ["validation_plan_not_list"]
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            step_type = _map_step_name(item)
            steps.append(ValidationStep(type=step_type))
            continue
        if not isinstance(item, dict):
            reasons.append(f"step_{idx}_not_object")
            continue
        raw_type = str(item.get("type") or item.get("kind") or item.get("name") or "")
        step_type = _map_step_name(raw_type)
        if step_type not in ALLOWED_STEP_TYPES:
            reasons.append(f"step_{idx}_type_not_allowed:{raw_type}")
            continue
        command = item.get("command")
        steps.append(
            ValidationStep(
                type=step_type,
                command=str(command).strip() if command else None,
                required=bool(item.get("required", True)),
                notes=str(item.get("notes") or item.get("description") or ""),
            )
        )
    return steps, reasons


def _map_step_name(name: str) -> ValidationStepType:
    n = (name or "").strip().lower()
    if n in {"unit", "pytest", "unit_test", "test"}:
        return "unit_test"
    if n in {"static", "static_check", "typecheck", "lint", "tsc"}:
        return "static_check"
    if n in {"fixture_replay", "manual", "review", "manual_review"}:
        return "manual_review"
    if n == "backtest":
        return "backtest"
    if n == "shadow_run":
        return "shadow_run"
    if n == "canary":
        return "canary"
    return "manual_review"


__all__ = [
    "ALLOWED_STEP_TYPES",
    "ValidationPlan",
    "ValidationStep",
    "build_validation_plan",
    "load_validation_plan",
    "run_validation_plan",
    "validate_plan_record",
    "write_validation_plan",
]
