"""Structured validation plans for proposal-first evolution."""

from __future__ import annotations

import json
import os
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
from .candidate_bundle import verify_candidate_bundle
from .patch_proposal import list_proposals
from .post_apply_observation import record_post_apply_observation
from .promotion import isolated_candidate_workspaces


run_strategy_backtest = None
NoHistoricalDataError = None


ValidationStepType = Literal[
    "unit_test",
    "static_check",
    "backtest",
    "eval_scenario",
    "shadow_run",
    "canary",
    "manual_review",
]

ALLOWED_STEP_TYPES = {
    "unit_test",
    "static_check",
    "backtest",
    "eval_scenario",
    "shadow_run",
    "canary",
    "manual_review",
}

EXECUTABLE_STEP_TYPES = {"unit_test", "static_check", "backtest", "eval_scenario"}
VALIDATION_RUN_TIMEOUT_SECONDS = 120
VALIDATION_OUTPUT_LIMIT = 12000
VALIDATION_EVAL_OUTPUT_LIMIT = 200000


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
    candidate_bundle_digest: str | None = None
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
    candidate_bundle_digest: str | None = None,
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
        candidate_bundle_digest=_nonempty(candidate_bundle_digest),
        status="blocked" if reasons else "not_run",
        blocked_reasons=reasons,
    )


def write_validation_plan(paths: WorkspacePaths, plan: ValidationPlan) -> str:
    _require_safe_plan_id(plan.id)
    out = paths.evolution_validation_plans / f"{plan.id}.json"
    atomic_write_text(out, json.dumps(plan.asdict(), indent=2, ensure_ascii=False, default=str))
    return plan.id


def load_validation_plan(paths: WorkspacePaths, plan_id: str) -> dict[str, Any] | None:
    if not _safe_plan_id(plan_id):
        return None
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
    requested_proposal_id = _nonempty(proposal_id)
    if requested_proposal_id:
        plan = _plan_for_proposal(paths, proposal_id)
    elif plan_id:
        plan = load_validation_plan(paths, plan_id)
    if plan is None:
        return {"ok": False, "reason": "not_found", "plan_id": plan_id, "proposal_id": proposal_id}
    checked = validate_plan_record(plan)
    binding = _validation_candidate_binding(
        paths,
        plan,
        requested_proposal_id=requested_proposal_id,
    )
    if not binding["ok"]:
        return {
            "ok": False,
            "reason": binding["reason"],
            "dry_run": True,
            "plan_id": plan.get("id") or plan_id,
            "proposal_id": requested_proposal_id or plan.get("proposal_id"),
            "plan": plan,
            "candidate_bundle_digest": binding.get("candidate_bundle_digest"),
            "candidate_bundle": binding.get("candidate_bundle"),
            **checked,
            "status": "blocked",
        }
    if dry_run or not checked["safe_to_run"]:
        return {
            "ok": checked["safe_to_run"],
            "dry_run": True,
            "plan": plan,
            "candidate_bundle_digest": binding.get("candidate_bundle_digest"),
            **checked,
        }
    bound_proposal = None
    if binding.get("bound"):
        bound_proposal = next(
            (item for item in list_proposals(paths)
             if item.id == str(plan.get("proposal_id") or requested_proposal_id or "")),
            None,
        )
    if binding.get("bound") and bound_proposal is not None:
        try:
            with isolated_candidate_workspaces(paths, bound_proposal) as (
                baseline_paths,
                challenger_paths,
                _mutation_plan,
            ):
                run = _execute_validation_plan(
                    challenger_paths,
                    plan,
                    baseline_paths=baseline_paths,
                )
        except Exception as exc:
            run = {
                "id": new_id("vrn"),
                "plan_id": plan.get("id"),
                "proposal_id": plan.get("proposal_id"),
                "strategy_id": plan.get("strategy_id"),
                "started_at": now_iso(),
                "finished_at": now_iso(),
                "duration_ms": 0,
                "status": "failed",
                "reason": f"candidate_workspace_failed:{type(exc).__name__}: {exc}",
                "steps": [],
            }
    else:
        run = _execute_validation_plan(paths, plan)
    if binding.get("bound"):
        run["candidate_bundle_digest"] = binding.get("candidate_bundle_digest")
        # A validation command may mutate the staged candidate.  Do not retain
        # a passing run that was produced against bytes other than the frozen
        # proposal bundle.
        after_binding = _validation_candidate_binding(
            paths,
            plan,
            requested_proposal_id=requested_proposal_id,
        )
        if not after_binding["ok"]:
            run["status"] = "failed"
            run["reason"] = "validation_candidate_bundle_mismatch"
            run["candidate_bundle_conflict"] = after_binding
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
        "candidate_bundle_digest": binding.get("candidate_bundle_digest"),
        "status": run["status"],
    }


def _plan_for_proposal(paths: WorkspacePaths, proposal_id: str) -> dict[str, Any] | None:
    if not _safe_plan_id(proposal_id):
        return None
    meta_path = paths.proposals / proposal_id / "proposal.yml"
    if not meta_path.exists():
        return None
    from ..core import yaml_io

    meta = yaml_io.load(meta_path, default={}) or {}
    plan_id = meta.get("validation_plan_id")
    if not plan_id:
        return None
    return load_validation_plan(paths, str(plan_id))


def _load_proposal_candidate_bundle(proposal) -> dict[str, Any] | None:
    path = proposal.path / "candidate_bundle.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = None
    if isinstance(raw, dict):
        return raw
    try:
        from ..core import yaml_io

        meta = yaml_io.load(proposal.path / "proposal.yml", default={}) or {}
    except Exception:
        return None
    bundle = meta.get("candidate_bundle")
    return bundle if isinstance(bundle, dict) else None


def _validation_candidate_binding(
    paths: WorkspacePaths,
    plan: dict[str, Any],
    *,
    requested_proposal_id: str | None = None,
) -> dict[str, Any]:
    """Check the immutable candidate that proposal validation evidence covers.

    Plans without a proposal id/digest are the old operator-authored form and
    remain runnable when addressed directly by ``plan_id``.  A proposal route
    (or an explicitly frozen digest) takes the strict path.
    """

    embedded_proposal_id = _nonempty(plan.get("proposal_id"))
    requested = _nonempty(requested_proposal_id)
    proposal_id = requested or embedded_proposal_id
    plan_digest = _nonempty(plan.get("candidate_bundle_digest"))
    # Older hand-authored plans sometimes carried a descriptive proposal id
    # without a frozen candidate bundle. Keep those plans runnable when they
    # are addressed by plan id; modern proposals bind the digest at creation.
    if not plan_digest and not requested:
        return {"ok": True, "bound": False}
    strict = bool(requested or plan_digest)
    if not proposal_id and not strict:
        return {"ok": True, "bound": False}
    if requested and embedded_proposal_id and requested != embedded_proposal_id:
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_plan_proposal_mismatch",
            "candidate_bundle_digest": plan_digest,
        }
    if not proposal_id:
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_candidate_bundle_unbound",
            "candidate_bundle_digest": plan_digest,
        }
    proposal = next((p for p in list_proposals(paths) if p.id == proposal_id), None)
    if proposal is None:
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_proposal_not_found",
            "proposal_id": proposal_id,
            "candidate_bundle_digest": plan_digest,
        }
    bundle = _load_proposal_candidate_bundle(proposal)
    if not isinstance(bundle, dict):
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_candidate_bundle_unbound",
            "proposal_id": proposal_id,
            "candidate_bundle_digest": plan_digest,
        }
    bundle_digest = _nonempty(bundle.get("digest"))
    if not plan_digest:
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_candidate_bundle_unbound",
            "proposal_id": proposal_id,
            "candidate_bundle": {"bundle": bundle},
        }
    if plan_digest != bundle_digest:
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_candidate_bundle_mismatch",
            "proposal_id": proposal_id,
            "candidate_bundle_digest": plan_digest,
            "candidate_bundle": {
                "ok": False,
                "reason": "validation_candidate_bundle_mismatch",
                "expected_digest": plan_digest,
                "actual_digest": bundle_digest,
            },
        }
    check = verify_candidate_bundle(paths.root, proposal.path, bundle)
    if not check.get("ok"):
        return {
            "ok": False,
            "bound": True,
            "reason": "validation_candidate_bundle_mismatch",
            "proposal_id": proposal_id,
            "candidate_bundle_digest": plan_digest,
            "candidate_bundle": check,
        }
    return {
        "ok": True,
        "bound": True,
        "proposal_id": proposal_id,
        "candidate_bundle_digest": plan_digest,
        "candidate_bundle": check,
    }


def _execute_validation_plan(
    paths: WorkspacePaths,
    plan: dict[str, Any],
    *,
    baseline_paths: WorkspacePaths | None = None,
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
                candidate_bound=baseline_paths is not None,
            ))
            continue
        if step_type == "eval_scenario" and baseline_paths is not None:
            step_results.append(_run_candidate_eval_step(
                paths,
                baseline_paths=baseline_paths,
                run_id=run_id,
                index=idx,
                command=command,
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


def _run_candidate_eval_step(
    challenger_paths: WorkspacePaths,
    *,
    baseline_paths: WorkspacePaths,
    run_id: str,
    index: int,
    command: str,
    required: bool,
    evidence_ref: str,
) -> dict[str, Any]:
    """Run one eval catalog against identical baseline/challenger roots."""

    if not command:
        return {
            "index": index,
            "type": "eval_scenario",
            "status": "failed" if required else "skipped",
            "required": required,
            "reason": "missing_command",
            "evidence_ref": evidence_ref,
        }
    baseline = _run_validation_command(
        baseline_paths,
        run_id=run_id,
        index=index,
        step_type="eval_scenario_baseline",
        command=command,
        required=False,
        evidence_ref=f"{evidence_ref}:baseline",
        output_limit=VALIDATION_EVAL_OUTPUT_LIMIT,
    )
    challenger = _run_validation_command(
        challenger_paths,
        run_id=run_id,
        index=index,
        step_type="eval_scenario_challenger",
        command=command,
        required=required,
        evidence_ref=f"{evidence_ref}:challenger",
        output_limit=VALIDATION_EVAL_OUTPUT_LIMIT,
    )
    baseline_summary = _parse_eval_summary(baseline.get("stdout"))
    challenger_summary = _parse_eval_summary(challenger.get("stdout"))
    comparison, regressions, improvements = _compare_eval_summaries(
        baseline_summary,
        challenger_summary,
    )
    comparison_error = None
    if baseline_summary is None or challenger_summary is None:
        comparison_error = "eval_summary_unavailable"
    elif any(
        row.get("baseline_passed") is None
        or row.get("challenger_passed") is None
        for row in comparison
    ):
        comparison_error = "eval_scenario_set_mismatch"
    status = "passed"
    if comparison_error or regressions:
        status = "failed" if required else "skipped"
    elif challenger.get("status") != "passed" and challenger_summary is None:
        status = "failed" if required else "skipped"
    return {
        "index": index,
        "type": "eval_scenario",
        "status": status,
        "required": required,
        "command": command,
        "evidence_ref": evidence_ref,
        "baseline": baseline,
        "challenger": challenger,
        "baseline_summary": baseline_summary,
        "challenger_summary": challenger_summary,
        "comparison": comparison,
        "regressions": regressions,
        "improvements": improvements,
        "comparison_error": comparison_error,
        "same_command": True,
        "baseline_workspace": str(baseline_paths.root),
        "challenger_workspace": str(challenger_paths.root),
    }


def _parse_eval_summary(stdout: Any) -> dict[str, Any] | None:
    text = str(stdout or "").strip()
    if not text:
        return None
    candidates = [text, *reversed(text.splitlines())]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("results"), list):
            return value
    return None


def _eval_result_passed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if "passed" in value:
        return bool(value.get("passed"))
    verdict = value.get("verdict")
    return bool(verdict.get("passed")) if isinstance(verdict, dict) else False


def _compare_eval_summaries(
    baseline: dict[str, Any] | None,
    challenger: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if baseline is None or challenger is None:
        return [], [], []
    def index(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for raw in summary.get("results") or []:
            if not isinstance(raw, dict):
                continue
            scenario_id = str(raw.get("scenario_id") or "").strip()
            if scenario_id:
                out[scenario_id] = raw
        return out

    before = index(baseline)
    after = index(challenger)
    comparison: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for scenario_id in sorted(set(before) | set(after)):
        old = before.get(scenario_id)
        new = after.get(scenario_id)
        old_passed = None if old is None else _eval_result_passed(old)
        new_passed = None if new is None else _eval_result_passed(new)
        row = {
            "scenario_id": scenario_id,
            "baseline_passed": old_passed if old is not None else None,
            "challenger_passed": new_passed if new is not None else None,
        }
        comparison.append(row)
        if old_passed is True and new_passed is not True:
            regressions.append(row)
        elif old_passed is False and new_passed is True:
            improvements.append(row)
    return comparison, regressions, improvements


def _run_backtest_validation_step(
    paths: WorkspacePaths,
    *,
    run_id: str,
    index: int,
    raw: dict[str, Any],
    plan: dict[str, Any],
    required: bool,
    evidence_ref: str,
    candidate_bound: bool = False,
) -> dict[str, Any]:
    started = now_iso()
    t0 = time.monotonic()
    proposal_id = _nonempty(raw.get("proposal_id")) or _nonempty(plan.get("proposal_id"))
    strategy_id = _nonempty(raw.get("strategy_id")) or _nonempty(plan.get("strategy_id"))
    if candidate_bound and proposal_id:
        # The challenger already contains the merged after/strategies tree.
        # Resolve the package from that workspace, never from the real
        # proposal staging directory.
        strategy_id = strategy_id or _single_workspace_strategy_id(paths)
        proposal_id = None
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
    if candidate_bound:
        base["baseline_not_run"] = True
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


def _single_workspace_strategy_id(paths: WorkspacePaths) -> str | None:
    root = paths.strategies
    if root.is_symlink() or not root.exists() or not root.is_dir():
        return None
    candidates = [
        child.name
        for child in sorted(root.iterdir())
        if child.is_dir() and not child.is_symlink()
    ]
    return candidates[0] if len(candidates) == 1 else None


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


def _safe_plan_id(value: Any) -> bool:
    text = str(value or "").strip()
    return (
        bool(text)
        and "/" not in text
        and "\\" not in text
        and Path(text).name == text
        and text not in {".", ".."}
    )


def _require_safe_plan_id(value: Any) -> None:
    if not _safe_plan_id(value):
        raise ValueError(f"invalid validation plan id: {value!r}")


def _run_validation_command(
    paths: WorkspacePaths,
    *,
    run_id: str,
    index: int,
    step_type: str,
    command: str,
    required: bool,
    evidence_ref: str,
    output_limit: int = VALIDATION_OUTPUT_LIMIT,
) -> dict[str, Any]:
    # Re-check immediately before spawning.  ``validate_plan_record`` protects
    # the normal path, but a loaded plan is mutable on disk and callers may
    # invoke this helper from an integration boundary; never rely on a stale
    # preflight result for code execution.
    policy = validate_validation_command(command, workspace=paths.root)
    if not policy.ok:
        return {
            "index": index,
            "type": step_type,
            "status": "failed" if required else "skipped",
            "required": required,
            "command": command,
            "reason": "command_policy_blocked",
            "blocked_reasons": list(policy.reasons),
            "evidence_ref": evidence_ref,
        }
    argv = shlex.split(command)
    exec_argv = [sys.executable, *argv[1:]] if argv and argv[0] == "python" else argv
    started = now_iso()
    t0 = time.monotonic()
    try:
        env = os.environ.copy()
        # Explicit candidate roots must win over the operator's active profile.
        env.pop("NERYA_PROFILE", None)
        env["NERYA_WORKSPACE"] = str(paths.root)
        completed = subprocess.run(
            exec_argv,
            cwd=paths.root,
            env=env,
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
            "stdout": _limit_output(completed.stdout, limit=output_limit),
            "stderr": _limit_output(completed.stderr, limit=output_limit),
            "evidence_ref": evidence_ref,
            "cwd": str(paths.root),
            "workspace": str(paths.root),
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
            "stdout": _limit_output(exc.stdout or "", limit=output_limit),
            "stderr": _limit_output(exc.stderr or "", limit=output_limit),
            "evidence_ref": evidence_ref,
            "cwd": str(paths.root),
            "workspace": str(paths.root),
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
            "cwd": str(paths.root),
            "workspace": str(paths.root),
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
    if not _safe_plan_id(plan_id):
        return
    out = paths.evolution_validation_plans / f"{plan_id}.json"
    atomic_write_text(out, json.dumps(plan, indent=2, ensure_ascii=False, default=str))


def _limit_output(text: Any, *, limit: int = VALIDATION_OUTPUT_LIMIT) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


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
    if n in {"eval", "evals", "eval_scenario", "agent_eval"}:
        return "eval_scenario"
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
