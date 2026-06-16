"""Structured validation plans for proposal-first evolution."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..core.errors import TradingError
from ..core.atomic_write import atomic_write_text
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .asset_policy import validate_validation_command
from .patch_proposal import list_proposals
from .post_apply_observation import record_post_apply_observation


run_strategy_backtest = None
NoHistoricalDataError = None


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

EXECUTABLE_STEP_TYPES = {"unit_test", "static_check", "backtest"}
VALIDATION_RUN_TIMEOUT_SECONDS = 120
VALIDATION_OUTPUT_LIMIT = 12000


@dataclass(frozen=True)
class ValidationStep:
    type: ValidationStepType
    command: str | None = None
    required: bool = True
    status: str = "not_run"
    evidence_ref: str | None = None
    notes: str = ""
    preset: str | None = None
    config_path: str | None = None
    allow_mock: bool = False
    proposal_id: str | None = None
    strategy_id: str | None = None

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
    run = _execute_validation_plan(paths, plan)
    _write_validation_run(paths, run)
    updated_plan = _apply_validation_run_to_plan(plan, run)
    _write_validation_plan_record(paths, updated_plan)
    return {
        **checked,
        "ok": run["status"] == "passed",
        "dry_run": False,
        "validation_run_id": run["id"],
        "run": run,
        "plan": updated_plan,
        "status": run["status"],
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


def _execute_validation_plan(
    paths: WorkspacePaths,
    plan: dict[str, Any],
) -> dict[str, Any]:
    run_id = new_id("vrn")
    started = now_iso()
    t0 = time.monotonic()
    step_results: list[dict[str, Any]] = []
    for idx, raw in enumerate(plan.get("steps") or []):
        if not isinstance(raw, dict):
            step_results.append({
                "index": idx,
                "type": "unknown",
                "status": "failed",
                "required": True,
                "reason": "step_not_object",
                "evidence_ref": f"validation:{run_id}:step:{idx}",
            })
            continue
        step_type = str(raw.get("type") or "")
        required = bool(raw.get("required", True))
        command = str(raw.get("command") or "").strip()
        evidence_ref = f"validation:{run_id}:step:{idx}"
        if step_type not in EXECUTABLE_STEP_TYPES:
            step_results.append({
                "index": idx,
                "type": step_type,
                "status": "deferred",
                "required": required,
                "command": command or None,
                "reason": "execution_not_enabled_for_step_type",
                "evidence_ref": evidence_ref,
            })
            continue
        if step_type == "backtest":
            step_results.append(_run_backtest_validation_step(
                paths,
                run_id=run_id,
                index=idx,
                raw=raw,
                plan=plan,
                required=required,
                evidence_ref=evidence_ref,
            ))
            continue
        if not command:
            step_results.append({
                "index": idx,
                "type": step_type,
                "status": "failed" if required else "skipped",
                "required": required,
                "reason": "missing_command",
                "evidence_ref": evidence_ref,
            })
            continue
        step_results.append(_run_validation_command(
            paths,
            run_id=run_id,
            index=idx,
            step_type=step_type,
            command=command,
            required=required,
            evidence_ref=evidence_ref,
        ))
    status = _validation_run_status(step_results)
    return {
        "id": run_id,
        "plan_id": plan.get("id"),
        "proposal_id": plan.get("proposal_id"),
        "strategy_id": plan.get("strategy_id"),
        "started_at": started,
        "finished_at": now_iso(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "status": status,
        "steps": step_results,
    }


def _run_backtest_validation_step(
    paths: WorkspacePaths,
    *,
    run_id: str,
    index: int,
    raw: dict[str, Any],
    plan: dict[str, Any],
    required: bool,
    evidence_ref: str,
) -> dict[str, Any]:
    started = now_iso()
    t0 = time.monotonic()
    proposal_id = _nonempty(raw.get("proposal_id")) or _nonempty(plan.get("proposal_id"))
    strategy_id = _nonempty(raw.get("strategy_id")) or _nonempty(plan.get("strategy_id"))
    preset = _nonempty(raw.get("preset")) or "default"
    config_path, config_error = _workspace_scoped_config_path(paths, raw.get("config_path"))
    requested_allow_mock = bool(raw.get("allow_mock", False))
    base: dict[str, Any] = {
        "index": index,
        "type": "backtest",
        "required": required,
        "preset": preset,
        "allow_mock": False,
        "requested_allow_mock": requested_allow_mock,
        "evidence_ref": evidence_ref,
        "started_at": started,
        "target": {
            "proposal_id": proposal_id,
            "strategy_id": None if proposal_id else strategy_id,
        },
    }
    if requested_allow_mock:
        base["allow_mock_note"] = "mock data is not accepted as validation evidence"
    if config_error:
        return {
            **base,
            "status": "failed" if required else "skipped",
            "reason": config_error,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
    if not proposal_id and not strategy_id:
        return {
            **base,
            "status": "failed" if required else "skipped",
            "reason": "missing_backtest_target",
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
    global run_strategy_backtest, NoHistoricalDataError
    if run_strategy_backtest is None:
        from ..skills.builtin.backtest.scripts.backtest_run import (
            run_strategy_backtest as _run_strategy_backtest,
        )

        run_strategy_backtest = _run_strategy_backtest
    if NoHistoricalDataError is None:
        from ..skills.builtin.backtest.scripts.data_cache import (
            NoHistoricalDataError as _NoHistoricalDataError,
        )

        NoHistoricalDataError = _NoHistoricalDataError

    try:
        result = run_strategy_backtest(
            proposal_id=proposal_id,
            strategy_id=None if proposal_id else strategy_id,
            preset=preset,
            config_path=config_path,
            workspace=paths.root,
            allow_mock=False,
        )
        status, reason = _backtest_validation_status(result, required=required)
        step = {
            **base,
            "status": status,
            "reason": reason,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "backtest_result": _summarize_backtest_result(result),
            "artifacts": _backtest_artifacts(paths, result),
        }
        _attach_post_apply_observation(
            paths,
            step,
            proposal_id=proposal_id,
            validation_run_id=run_id,
            step_index=index,
        )
        return step
    except NoHistoricalDataError as exc:
        return {
            **base,
            "status": "failed" if required else "skipped",
            "reason": "no_historical_data",
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "backtest_result": {
                "ok": False,
                "reason": "no_historical_data",
                "coverage_ok": False,
                "coverage_message": str(exc),
            },
        }
    except TradingError as exc:
        return {
            **base,
            "status": "failed" if required else "skipped",
            "reason": f"backtest_error:{exc}",
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "backtest_result": {"ok": False, "reason": str(exc)},
        }
    except Exception as exc:
        return {
            **base,
            "status": "failed" if required else "skipped",
            "reason": f"{type(exc).__name__}: {exc}",
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }


def _backtest_validation_status(result: dict[str, Any], *, required: bool) -> tuple[str, str | None]:
    if not result.get("ok"):
        reason = str(result.get("reason") or "backtest_failed")
        return ("failed" if required else "skipped"), reason
    verdict = str(result.get("verdict") or "").upper()
    if verdict == "FAIL":
        return ("failed" if required else "skipped"), "backtest_verdict_fail"
    if result.get("coverage_ok") is False:
        return ("failed" if required else "skipped"), "backtest_coverage_failed"
    return "passed", None


def _summarize_backtest_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ok",
        "reason",
        "strategy_id",
        "proposal_id",
        "backtest_ts",
        "verdict",
        "coverage_ok",
        "recommended_coverage_ok",
        "coverage_message",
        "total_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "profit_factor",
        "win_rate_pct",
        "total_trades",
        "total_fees_usd",
        "total_slippage_usd",
        "primary_timeframe",
        "timeframes",
        "requested_primary_timeframe",
        "timeframe_fallback",
        "timeframe_fallback_message",
        "operator_summary",
        "operator_summary_text",
        "metrics_display",
    )
    return {key: result.get(key) for key in keys if key in result}


def _backtest_artifacts(paths: WorkspacePaths, result: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for kind, key in (
        ("metrics", "metrics_path"),
        ("report", "report_path"),
        ("trades", "trades_path"),
        ("config", "config_path"),
    ):
        artifact = _backtest_artifact(paths, kind, result.get(key))
        if artifact:
            artifacts.append(artifact)
    out_dir = result.get("out_dir")
    if out_dir:
        artifact = _backtest_artifact(paths, "chart", Path(str(out_dir)) / "chart.json")
        if artifact:
            artifacts.append(artifact)
    return artifacts


def _backtest_artifact(
    paths: WorkspacePaths,
    kind: str,
    value: Any,
) -> dict[str, Any] | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = paths.root / path
    return {
        "kind": f"backtest_{kind}",
        "title": path.name,
        "path": str(path),
        "evidence_ref": f"file:{path}",
    }


def _attach_post_apply_observation(
    paths: WorkspacePaths,
    step: dict[str, Any],
    *,
    proposal_id: str | None,
    validation_run_id: str,
    step_index: int,
) -> None:
    if not proposal_id or not _proposal_is_applied(paths, proposal_id):
        return
    evidence_refs = [
        str(step.get("evidence_ref") or ""),
        *[
            str(artifact.get("evidence_ref") or "")
            for artifact in step.get("artifacts") or []
            if isinstance(artifact, dict)
        ],
    ]
    result = record_post_apply_observation(
        paths,
        proposal_id=proposal_id,
        source="validation_backtest",
        summary=_post_apply_backtest_summary(step),
        evidence_refs=evidence_refs,
        backtest_result=step.get("backtest_result") if isinstance(step.get("backtest_result"), dict) else {},
        run_id=validation_run_id,
        metadata={
            "validation_run_id": validation_run_id,
            "validation_step_index": step_index,
            "validation_step_status": step.get("status"),
            "validation_step_reason": step.get("reason"),
        },
    )
    if result.get("ok"):
        step["post_apply_observation"] = {
            "id": (result.get("observation") or {}).get("id"),
            "status": result.get("status"),
            "journal_ref": result.get("journal_ref"),
            "evidence_refs": result.get("evidence_refs") or [],
        }
    else:
        step["post_apply_observation_error"] = {
            "reason": result.get("reason"),
        }


def _proposal_is_applied(paths: WorkspacePaths, proposal_id: str) -> bool:
    for proposal in list_proposals(paths):
        if proposal.id == proposal_id:
            return str(proposal.state or "").lower() == "applied"
    return False


def _post_apply_backtest_summary(step: dict[str, Any]) -> str:
    result = step.get("backtest_result") if isinstance(step.get("backtest_result"), dict) else {}
    verdict = result.get("verdict")
    status = step.get("status")
    reason = step.get("reason")
    parts = [f"Post-apply backtest validation {status or 'completed'}"]
    if verdict:
        parts.append(f"with verdict {verdict}")
    if reason:
        parts.append(f"({reason})")
    return " ".join(parts) + "."


def _workspace_scoped_config_path(paths: WorkspacePaths, raw: Any) -> tuple[str | None, str | None]:
    value = _nonempty(raw)
    if not value:
        return None, None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = paths.root / path
    try:
        resolved = path.resolve()
        root = paths.root.resolve()
    except OSError:
        return None, "config_path_not_found"
    if not resolved.is_relative_to(root):
        return None, "config_path_outside_workspace"
    return str(resolved), None


def _nonempty(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _run_validation_command(
    paths: WorkspacePaths,
    *,
    run_id: str,
    index: int,
    step_type: str,
    command: str,
    required: bool,
    evidence_ref: str,
) -> dict[str, Any]:
    argv = shlex.split(command)
    exec_argv = [sys.executable, *argv[1:]] if argv and argv[0] == "python" else argv
    started = now_iso()
    t0 = time.monotonic()
    try:
        completed = subprocess.run(
            exec_argv,
            cwd=paths.root,
            capture_output=True,
            text=True,
            timeout=VALIDATION_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        return {
            "index": index,
            "type": step_type,
            "status": "passed" if completed.returncode == 0 else "failed",
            "required": required,
            "command": command,
            "returncode": completed.returncode,
            "started_at": started,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "stdout": _limit_output(completed.stdout),
            "stderr": _limit_output(completed.stderr),
            "evidence_ref": evidence_ref,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "index": index,
            "type": step_type,
            "status": "failed" if required else "skipped",
            "required": required,
            "command": command,
            "reason": "timeout",
            "timeout_seconds": VALIDATION_RUN_TIMEOUT_SECONDS,
            "started_at": started,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "stdout": _limit_output(exc.stdout or ""),
            "stderr": _limit_output(exc.stderr or ""),
            "evidence_ref": evidence_ref,
        }
    except Exception as exc:
        return {
            "index": index,
            "type": step_type,
            "status": "failed" if required else "skipped",
            "required": required,
            "command": command,
            "reason": f"{type(exc).__name__}: {exc}",
            "started_at": started,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "evidence_ref": evidence_ref,
        }


def _validation_run_status(steps: list[dict[str, Any]]) -> str:
    required = [step for step in steps if step.get("required", True)]
    if any(step.get("status") == "failed" for step in required):
        return "failed"
    if any(step.get("status") == "deferred" for step in required):
        return "partial"
    if not steps:
        return "not_run"
    return "passed"


def _apply_validation_run_to_plan(
    plan: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(plan)
    by_index = {
        int(step.get("index")): step
        for step in run.get("steps") or []
        if isinstance(step, dict) and step.get("index") is not None
    }
    steps: list[dict[str, Any]] = []
    for idx, raw in enumerate(plan.get("steps") or []):
        step = dict(raw) if isinstance(raw, dict) else {"type": "unknown"}
        result = by_index.get(idx)
        if result:
            step["status"] = result.get("status") or "not_run"
            step["evidence_ref"] = result.get("evidence_ref")
            if result.get("reason"):
                step["notes"] = result.get("reason")
        steps.append(step)
    updated["steps"] = steps
    updated["status"] = run.get("status") or "not_run"
    updated["last_run_id"] = run.get("id")
    updated["last_run_at"] = run.get("finished_at")
    return updated


def _write_validation_run(paths: WorkspacePaths, run: dict[str, Any]) -> str:
    out = paths.evolution / "validation_runs" / f"{run['id']}.json"
    atomic_write_text(out, json.dumps(run, indent=2, ensure_ascii=False, default=str))
    return str(out)


def _write_validation_plan_record(paths: WorkspacePaths, plan: dict[str, Any]) -> None:
    plan_id = str(plan.get("id") or "")
    if not plan_id:
        return
    out = paths.evolution_validation_plans / f"{plan_id}.json"
    atomic_write_text(out, json.dumps(plan, indent=2, ensure_ascii=False, default=str))


def _limit_output(text: Any) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= VALIDATION_OUTPUT_LIMIT:
        return text
    return text[:VALIDATION_OUTPUT_LIMIT] + "\n... [truncated]"


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
                preset=str(item.get("preset") or "").strip() or None,
                config_path=str(item.get("config_path") or item.get("config") or "").strip() or None,
                allow_mock=bool(item.get("allow_mock", False)),
                proposal_id=str(item.get("proposal_id") or "").strip() or None,
                strategy_id=str(item.get("strategy_id") or "").strip() or None,
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
