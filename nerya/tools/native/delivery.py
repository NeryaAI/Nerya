"""Native tools for the durable task-delivery projection."""
from __future__ import annotations

import json
import re
from typing import Any

from ...task_delivery import (
    DELIVERY_START_SCHEMA, DELIVERY_UPDATE_SCHEMA, DELIVERY_FINALIZE_SCHEMA,
    DeliveryError, finalize_delivery, read_delivery, start_delivery, update_delivery, validate_delivery_input,
)
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult
from .skill import skill_access_error


def _guidance(saved: dict | None = None) -> dict:
    """Bounded host instructions and receipt identities, never model artifact prose."""
    saved = saved or {}
    examples = [
        {"artifact_id": "candidate", "ref": "<source_binding.artifact_id>"},
        {"artifact_id": "report", "ref": "<producer-returned report_path>"},
        {"artifact_id": "run_receipt", "ref": "<producer-returned run_receipt>"},
    ]
    # Only producer-owned passing checks supply concrete examples. In particular,
    # a registered report path, file existence or model claim is not an attestation.
    for example, check_id, ref_key in zip(examples,
            ("source_binding", "report_output", "historical_run"),
            ("artifact_id", "evidence_ref", "evidence_ref")):
        for check in saved.get("checks", [])[:128]:
            ref, revision = check.get(ref_key, ""), check.get("revision", "")
            if (check.get("check_id") == check_id and check.get("producer_owned") is True
                    and check.get("required") is True and check.get("status") == "passed"
                    and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", ref)
                    and not ref.startswith("/") and "://" not in ref and ".." not in ref
                    and re.fullmatch(r"[a-f0-9]{64}", revision)):
                example.update(ref=ref, revision=revision)
                break
    stage_ids = [stage["id"] for stage in saved.get("stages", [])[:4]
                 if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", stage.get("id", ""))]
    required = [identity for identity in
                saved.get("workflow_contract", {}).get("required_artifacts", [])[:4]
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identity)]
    hint = {
        "action": "repair_delivery_patch", "stage_ids": stage_ids,
        "required_artifact_ids": required, "artifact_examples": examples,
        "stage_example": {"id": stage_ids[0] if stage_ids else "deliver",
                          "required_artifacts": required or ["candidate", "report", "run_receipt"]},
        "producer_checks_read_only": True, "side_effects": "none",
        "hint": "Stage required_artifacts are artifact IDs, not paths; register artifacts as {artifact_id,ref}. "
                "Replace placeholders only with producer-returned source identities/paths. "
                "Checks are producer-owned: omit checks; never set any status. "
                "Use the latest delivery_revision as expected_revision; reread after a CAS conflict. "
                "A handwritten missing-history diagnostic remains unverified and cannot stand in for a backtest report. "
                "Missing/stale receipts and prohibited work keep the completion floor unmet; "
                "report incomplete/partial, preserve the candidate and do not repeat completed producers just to repair delivery.",
    }
    if saved:
        hint.update(delivery_revision=saved["delivery_revision"], expected_revision=saved["delivery_revision"])
        hint["completion"] = saved["completion"]
    return hint


def _recovery_summary(hint: dict) -> str:
    """Expose only the bounded repair fields the model needs to continue."""
    summary = {}
    if isinstance(hint.get("expected_revision"), int):
        summary["expected_revision"] = hint["expected_revision"]
    required = hint.get("required_artifact_ids")
    if isinstance(required, list):
        summary["required_artifacts"] = [str(item)[:128] for item in required[:8]]
    examples = hint.get("artifact_examples")
    if isinstance(examples, list):
        summary["artifact_examples"] = [
            {key: str(item[key])[:256] for key in ("artifact_id", "ref", "revision") if key in item}
            for item in examples[:8] if isinstance(item, dict)
        ]
    summary["rules"] = [
        "required_artifacts are artifact IDs, not paths",
        "checks are producer-owned",
        "use the latest delivery_revision",
    ]
    return "Recovery: " + json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def _error(call: ToolCall, exc: Exception, deps: Any = None) -> ToolResult:
    code = getattr(exc, "code", "delivery_failed")
    saved = getattr(exc, "delivery", None)
    if deps is not None:
        try:
            paths,sid,cid=_context(deps)
            saved=read_delivery(paths,command_id=cid,session_id=sid)
        except (DeliveryError,AttributeError,KeyError,TypeError,ValueError):
            pass
    hint = _guidance(saved)
    raw_code = str(code)
    # DeliveryError codes are runtime outcomes, not all input-schema errors.
    # Preserve the distinction so the UI/agent can choose read, reconcile,
    # permission, or repair semantics without guessing from one generic kind.
    if raw_code == "delivery_revision_conflict":
        error_kind = ToolErrorKind.CONFLICT
        retryable = True
    elif raw_code == "delivery_skill_access_denied":
        error_kind = ToolErrorKind.PERMISSION_DENIED
        retryable = False
    elif raw_code.startswith("trusted_delivery_workflow_missing; availableWorkflows: "):
        # An unknown workflow is caller input, not a broken trusted contract.
        # Keep the contract strict, but let the model repair the call in the
        # same turn using the canonical id returned below.
        error_kind = ToolErrorKind.SCHEMA_VALIDATION
        retryable = True
    elif raw_code.startswith("trusted_delivery_") or raw_code == "untrusted_delivery_contract_source":
        error_kind = ToolErrorKind.EXECUTION_ERROR
        retryable = False
    elif raw_code.startswith((
        "invalid_delivery_", "duplicate_delivery_", "unknown_delivery_",
        "too_many_delivery_", "delivery_check_owned_by_producer",
        "delivery_unknown_required_checks;",
    )):
        error_kind = ToolErrorKind.SCHEMA_VALIDATION
        retryable = True
    else:
        error_kind = ToolErrorKind.EXECUTION_ERROR
        retryable = False
    if raw_code in {"trusted_delivery_contract_missing", "trusted_delivery_contract_invalid", "untrusted_delivery_contract_source"}:
        message = "This Skill has no usable trusted delivery contract. Follow its documented producer workflow instead."
        hint = {
            "action": "read_skill_workflow", "retryable": False, "side_effects": "none",
            "hint": "Do not retry delivery_start with invented workflow ids or different stages. "
                    "Read the Skill and use its documented producer tools and returned artifacts. "
                    "Do not claim delivery-contract completion or bypass approval.",
        }
    elif raw_code.startswith("trusted_delivery_workflow_missing; availableWorkflows: "):
        workflows = [item for item in raw_code.split(": ", 1)[1].split(",") if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item)]
        hint["available_workflows"] = workflows
        if len(workflows) == 1:
            hint["canonical_workflow_id"] = workflows[0]
            message = ("Read the Skill and confirm that the requested work needs this workflow. "
                       "For standalone read-only inspection or status, return the tool observations without starting a delivery. "
                       "If this workflow was requested, use workflow_id=" + repr(workflows[0])
                       + "; do not derive a task, market, model or replay title.")
        else:
            message = "Delivery contract needs a trusted workflow. Choose one of the available workflow ids in recovery_hint."
    elif raw_code == "delivery_workflow_binding_conflict":
        message = "delivery_workflow_binding_conflict: the delivery workflow is already bound to this task; current state was preserved."
    elif raw_code == "delivery_skill_access_denied":
        message = "delivery_skill_access_denied: this skill is not allowed to update the delivery contract; current state was preserved."
    elif raw_code == "delivery_revision_conflict":
        message = ("delivery_revision_conflict: current state was preserved; use recovery_hint.expected_revision "
                   "for the revised delivery patch. Do not repeat completed validation or backtest operations.")
    elif raw_code == "delivery_not_started":
        hint = {
            "code": raw_code,
            "action": "read_current_command_or_start_authorized_workflow",
            "retryable": False,
            "side_effects": "none",
            "current_command_has_delivery": False,
            "historical_snapshots_read_only": True,
            "read_only": {
                "action": "report_recorded_completion_and_reasons",
                "do_not_call": ["delivery_finalize", "delivery_start"],
                "hint": "Historical command snapshots are read-only evidence; they do not provide the current command delivery or revision.",
            },
            "authorized_execution": {
                "first_action": "load_named_skill",
                "delivery_command": _context(deps)[2] if deps is not None else "",
                "next_action": "delivery_start_current_command",
                "hint": "Only an explicitly authorized workflow may create delivery for this current command before update or finalize.",
            },
        }
        message = ("delivery_not_started: this current command has no delivery. Historical command snapshots are "
                   "read-only evidence, not the current delivery or revision. For a read-only request, report "
                   "recorded completion and reasons; do not call delivery_finalize or delivery_start. "
                   "For explicitly authorized execution, load the named Skill and delivery_start for the current command first.")
    elif raw_code == "delivery_check_owned_by_producer":
        message = ("delivery_check_owned_by_producer: producer-owned checks are read-only; "
                   "omit checks with any status and update only the affected stage reason or limitations.")
        hint["hint"] = ("Producer-owned checks are read-only: omit checks regardless of status. "
                           "On failure, update only the affected stage with structured reason or limitations.")
    else:
        message = "Delivery contract update was not accepted. Follow recovery_hint and preserve the current task state."
    if str(code).startswith("delivery_unknown_required_checks; availableIDs: "):
        # This admission error already exposes only the trusted allowed IDs;
        # preserve its established diagnostic contract.
        message = str(code)
    if not str(code).startswith("delivery_unknown_required_checks; availableIDs: "):
        message += " " + _recovery_summary(hint)
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=error_kind, message=message, detail={"code": raw_code}, retryable=retryable, recovery_hint=hint),
    )


def _context(deps: Any) -> tuple[Any, str, str]:
    config = deps.config
    if config is None or deps.paths is None or not deps.active_session_id:
        raise DeliveryError("delivery_context_missing", 400)
    command_id = str(config.get("runtime.command_id", "") or "")
    if not command_id:
        raise DeliveryError("delivery_command_missing", 400)
    return deps.paths, deps.active_session_id, command_id


def _contract(call: ToolCall, deps: Any, skill_id: str, workflow_id: str) -> dict:
    if skill_access_error(call, skill_id) is not None:
        raise DeliveryError("delivery_skill_access_denied", 403)
    index = getattr(deps, "skill_index", None)
    if index is None:
        return {"error": "trusted_delivery_contract_missing"}
    try:
        return index.delivery_contract(call, skill_id, workflow_id)
    except DeliveryError as exc:
        return {"error": exc.code}


def _validate_required_checks(args: dict, contract: dict) -> None:
    """Only the trusted workflow floor names model-declarable required checks."""
    available = set(contract["required_checks"])
    requested = {check_id for stage in args.get("stages", [])
                 for check_id in stage.get("required_checks", [])}
    requested.update(row["check_id"] for row in args.get("checks", [])
                     if row.get("required", True))
    if requested - available:
        # Return trusted IDs only; do not echo arbitrary model input.
        raise DeliveryError(
            "delivery_unknown_required_checks; availableIDs: " + ",".join(sorted(available)), 400)


def delivery_start_handler(call: ToolCall, *, deps: Any) -> ToolResult:
    try:
        paths, session_id, command_id = _context(deps)
        args = call.arguments or {}
        validate_delivery_input(args, DELIVERY_START_SCHEMA)
        try:
            existing = read_delivery(paths, command_id=command_id, session_id=session_id)
        except DeliveryError as exc:
            if exc.code != "delivery_not_started":
                raise
            existing = None
        if existing is not None and any(existing[key] != args[key] for key in ("skill_id", "workflow_id")):
            raise DeliveryError("delivery_workflow_binding_conflict")
        contract = _contract(call, deps, args["skill_id"], args["workflow_id"])
        if existing is None and contract.get("error"):
            raise DeliveryError(contract["error"], 400)
        if existing is None:
            _validate_required_checks(args, contract)
        result = start_delivery(
            paths, command_id=command_id, session_id=session_id,
            skill_id=str(args.get("skill_id") or ""),
            workflow_id=str(args.get("workflow_id") or ""),
            requested_scope=args.get("requested_scope") if isinstance(args.get("requested_scope"), dict) else {},
            stages=args.get("stages") if isinstance(args.get("stages"), list) else [],
            workflow_contract=contract,
        )
        from ...strategies.tool_verification import current_command_delivery_receipts
        receipts, versions = current_command_delivery_receipts(deps.config, command_id, session_id)
        result = update_delivery(paths, command_id=command_id, session_id=session_id,
                                 expected_revision=result["delivery_revision"], producer_receipts=receipts,
                                 source_versions=versions, reconcile_only=True, workflow_contract=contract)
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result,
                                    semantic_success=not bool(result.get("workflow_contract_error")), result_protocol="nerya.task_delivery.v1")
    except (DeliveryError, TypeError, ValueError) as exc:
        return _error(call, exc, deps)


def delivery_update_handler(call: ToolCall, *, deps: Any) -> ToolResult:
    try:
        paths, session_id, command_id = _context(deps)
        args = call.arguments or {}
        validate_delivery_input(args, DELIVERY_UPDATE_SCHEMA)
        delivery = read_delivery(paths, command_id=command_id, session_id=session_id)
        contract = _contract(call, deps, delivery["skill_id"], delivery["workflow_id"])
        # Preserve sticky contract invalidation and legacy readback. Validate
        # new model requirements only when this update can mutate content.
        if (not contract.get("error") and not delivery.get("workflow_contract_error")
                and delivery.get("workflow_contract", contract) == contract):
            _validate_required_checks(args, contract)
        result = update_delivery(
            paths, command_id=command_id, session_id=session_id,
            expected_revision=int(args.get("expected_revision")),
            stages=args.get("stages") if "stages" in args else None,
            artifacts=args.get("artifacts") if "artifacts" in args else None,
            checks=args.get("checks") if "checks" in args else None,
            limitations=args.get("limitations") if "limitations" in args else None,
            recovery=args.get("recovery") if isinstance(args.get("recovery"), dict) else None,
            source_revision=str(args.get("source_revision") or ""),
            workflow_contract=contract,
        )
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result,
                                    semantic_success=not bool(result.get("workflow_contract_error")), result_protocol="nerya.task_delivery.v1")
    except (DeliveryError, TypeError, ValueError) as exc:
        return _error(call, exc, deps)


def delivery_finalize_handler(call: ToolCall, *, deps: Any) -> ToolResult:
    try:
        paths, session_id, command_id = _context(deps)
        args = call.arguments or {}
        validate_delivery_input(args, DELIVERY_FINALIZE_SCHEMA)
        from ...strategies.tool_verification import delivery_source_versions, current_command_delivery_receipts
        delivery = read_delivery(paths, command_id=command_id, session_id=session_id)
        contract = _contract(call, deps, delivery["skill_id"], delivery["workflow_id"])
        receipts, versions = current_command_delivery_receipts(deps.config, command_id, session_id)
        versions.update(delivery_source_versions(deps.config, delivery))
        result = finalize_delivery(paths, command_id=command_id, session_id=session_id,
                                   expected_revision=args["expected_revision"],
                                   source_versions=versions, producer_receipts=receipts, workflow_contract=contract)
        allowed = result["completion"]["allowed"]
        result = {"ok": allowed, "status": result["completion"]["status"], **result}
        if not allowed:
            result["delivery_guidance"] = _guidance(result)
            result["next_action"] = (
                "A handwritten missing-history diagnostic remains unverified and cannot stand in for a backtest report. "
                "Resolve completion.reasons with producer receipts; use artifact IDs in stage required_artifacts, "
                "returned refs in artifacts, and current delivery_revision as expected_revision. "
                "Checks are producer-owned; never set any status or waive the completion floor. "
                "Preserve incomplete/partial work and do not repeat completed writes.")
            if result.get("workflow_contract_error"):
                result["next_action"] = "Contract binding invalidated. Retain this command for readback; a new command with a trusted contract is required. Do not repeat completed writes."
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result, semantic_success=allowed, result_protocol="nerya.task_delivery.v1")
    except (DeliveryError, TypeError, ValueError) as exc:
        return _error(call, exc, deps)


__all__ = [
    "DELIVERY_START_SCHEMA", "DELIVERY_UPDATE_SCHEMA", "DELIVERY_FINALIZE_SCHEMA",
    "delivery_start_handler", "delivery_update_handler", "delivery_finalize_handler",
]
