"""Small, durable task-delivery contract shared by Skills and the workbench.

The contract is deliberately separate from command execution state and domain
verification. It records what a workflow promises to deliver, while producer
owned checks remain the authority for whether that promise is complete.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from .db.sqlite import connect
from .tools.verification import read_verification_receipt

PROTOCOL = "nerya.task_delivery.v1"
STAGE_STATES = frozenset({"pending", "in_progress", "completed", "blocked", "skipped"})
CHECK_STATES = frozenset({"passed", "failed", "missing", "pending", "stale", "unknown"})
EVENT_KINDS = frozenset({"delivery.stage", "delivery.check", "delivery.artifact", "delivery.recovery"})


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_TEXT = {"type": "string", "maxLength": 2048}
_ID = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9_.:-]+$"}


def _array(items, maximum=32, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


_STAGE_SCHEMA = _object({
    "id": _ID, "label": _TEXT, "state": {"enum": sorted(STAGE_STATES)},
    "required_artifacts": _array(_TEXT), "required_checks": _array(_ID), "reason": _TEXT,
}, ["id"])
_CHECK_SCHEMA = _object({
    "check_id": _ID, "status": {"enum": sorted(CHECK_STATES - {"passed"})},
    "required": {"type": "boolean"}, "label": _TEXT, "message": _TEXT,
    "revision": _TEXT, "evidence_ref": _TEXT,
}, ["check_id", "status"])
_ARTIFACT_SCHEMA = {"oneOf": [
    {"type": "string", "minLength": 1, "maxLength": 1024},
    _object({"artifact_id": _TEXT, "kind": _TEXT, "label": _TEXT, "ref": _TEXT,
             "revision": _TEXT, "status": {"enum": ["recorded"]}}, ["artifact_id", "ref"]),
]}
_RECOVERY_SCHEMA = _object({
    "action": _TEXT, "safe_to_retry": {"type": "boolean"},
    "requires_receipt_reconciliation": {"type": "boolean"}, "reason": _TEXT,
})
DELIVERY_START_SCHEMA = _object({
    "skill_id": _ID, "workflow_id": _ID,
    "requested_scope": _object({"summary": {**_TEXT, "minLength": 1}, "limitations": _array(_TEXT)}, ["summary"]),
    "stages": _array(_STAGE_SCHEMA, minimum=1),
}, ["skill_id", "workflow_id", "requested_scope", "stages"])
_DELIVERY_UPDATE_INTERNAL_SCHEMA = _object({
    "expected_revision": {"type": "integer", "minimum": 1},
    "stages": _array(_STAGE_SCHEMA), "artifacts": _array(_ARTIFACT_SCHEMA, 128),
    "checks": _array(_CHECK_SCHEMA, 128), "limitations": _array(_TEXT),
    "recovery": _RECOVERY_SCHEMA, "source_revision": _TEXT,
}, ["expected_revision"])
DELIVERY_UPDATE_SCHEMA = {
    **_DELIVERY_UPDATE_INTERNAL_SCHEMA,
    "properties": {key: value for key, value in _DELIVERY_UPDATE_INTERNAL_SCHEMA["properties"].items() if key != "checks"},
}
DELIVERY_FINALIZE_SCHEMA = _object({"expected_revision": {"type": "integer", "minimum": 1}}, ["expected_revision"])


def validate_delivery_input(value: Any, schema: dict) -> None:
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        path = ".".join(map(str, error.absolute_path)) or "arguments"
        # Do not echo input values (which can contain credentials).
        hint = ("allowed fields: " + ",".join(error.schema["properties"]) if "properties" in error.schema
                else "allowed values: " + ",".join(map(str, error.schema["enum"])) if "enum" in error.schema
                else "use the advertised delivery schema")
        raise DeliveryError(f"invalid_delivery_input:{path}:{error.validator}; {hint}", 400)
    for key, identity in (("stages", "id"), ("checks", "check_id")):
        rows = value.get(key, [])
        ids = [row[identity] for row in rows]
        if len(ids) != len(set(ids)):
            raise DeliveryError(f"duplicate_delivery_{key}", 400)


class DeliveryError(ValueError):
    def __init__(self, code: str, status: int = 409, *, delivery: dict | None = None):
        self.code, self.status = code, status
        self.delivery = delivery
        super().__init__(code)


def _text(value: Any, limit: int = 1024) -> str:
    return str(value or "").strip()[:limit]


def _scope_hash(summary: str, limitations: list[str]) -> str:
    payload = json.dumps([summary, limitations], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _safe_ref(value: Any) -> str:
    value = _text(value, 1024)
    # Delivery records may be returned to a browser. Keep references useful
    # without leaking a host path or an embedded credential.
    value = re.sub(r"(?i)(?:vault://|https?://)[^\s]+", "[redacted]", value)
    if value.startswith("/"):
        value = Path(value).name
    return value


def _stage(row: Mapping[str, Any]) -> dict[str, Any]:
    stage_id = _text(row.get("id") or row.get("stage_id"), 128)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", stage_id):
        raise DeliveryError("invalid_delivery_stage", 400)
    state = _text(row.get("state") or "pending", 32)
    if state not in STAGE_STATES:
        raise DeliveryError("invalid_delivery_stage_state", 400)
    return {
        "id": stage_id,
        "label": _text(row.get("label") or stage_id, 160),
        "state": state,
        "required_artifacts": [_safe_ref(x) for x in list(row.get("required_artifacts") or [])[:32]],
        "required_checks": [_safe_ref(x) for x in list(row.get("required_checks") or [])[:32]],
        "reason": _text(row.get("reason"), 256),
    }


def _check(row: Mapping[str, Any], *, default_revision: str = "") -> dict[str, Any]:
    check_id = _safe_ref(row.get("check_id") or row.get("id"))
    if not check_id:
        raise DeliveryError("invalid_delivery_check", 400)
    status = _text(row.get("status") or "pending", 32)
    if status not in CHECK_STATES:
        raise DeliveryError("invalid_delivery_check_state", 400)
    return {
        "check_id": check_id,
        "status": status,
        "required": type(row.get("required", True)) is bool and row.get("required", True),
        "revision": _safe_ref(row.get("revision") or default_revision),
        "label": _text(row.get("label") or check_id, 160),
        "message": _text(row.get("message"), 512),
        "evidence_ref": _safe_ref(row.get("evidence_ref")),
    }


def _artifact(row: Any) -> dict[str, Any]:
    if isinstance(row, str):
        row = {"artifact_id": row}
    if not isinstance(row, Mapping):
        raise DeliveryError("invalid_delivery_artifact", 400)
    artifact_id = _safe_ref(row.get("artifact_id") or row.get("id") or row.get("ref"))
    if not artifact_id:
        raise DeliveryError("invalid_delivery_artifact", 400)
    return {
        "artifact_id": artifact_id,
        "kind": _text(row.get("kind") or "artifact", 64),
        "label": _text(row.get("label") or artifact_id, 160),
        "ref": _safe_ref(row.get("ref") or artifact_id),
        "revision": _safe_ref(row.get("revision")),
        "status": _text(row.get("status") or "recorded", 32),
    }


def _recovery(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = value if isinstance(value, Mapping) else {}
    return {
        "action": _text(value.get("action") or "resume", 32),
        "safe_to_retry": type(value.get("safe_to_retry", True)) is bool and value.get("safe_to_retry", True),
        "requires_receipt_reconciliation": type(value.get("requires_receipt_reconciliation", False)) is bool and value.get("requires_receipt_reconciliation", False),
        "reason": _text(value.get("reason"), 256),
    }


def replay_visualization_requested(text: str) -> bool:
    """Explicit report expansion only; ordinary replay deliveries keep v1 behavior."""
    return bool(re.search(
        r"报告.{0,20}(?:展开|图表|曲线|表格|明细)|(?:展开|图表|曲线|表格|明细).{0,20}报告"
        r"|(?:expand|detailed).{0,30}(?:backtest|replay|report)"
        r"|(?:report|backtest|replay).{0,40}(?:charts?|tables?|curves?|trade details)",
        text, re.IGNORECASE))


def _completion(delivery: Mapping[str, Any]) -> dict[str, Any]:
    stages = list(delivery.get("stages") or [])
    artifact_rows = [row for row in delivery.get("artifacts") or [] if isinstance(row, Mapping)]
    artifacts = {str(row.get("artifact_id")) for row in artifact_rows}
    checks = [row for row in delivery.get("checks") or [] if isinstance(row, Mapping)]
    stage_checks = {check_id for stage in stages for check_id in stage.get("required_checks") or []}
    stage_artifacts = {artifact_id for stage in stages for artifact_id in stage.get("required_artifacts") or []}
    contract = delivery.get("workflow_contract") or {}
    stage_checks.update(contract.get("required_checks") or [])
    stage_artifacts.update(contract.get("required_artifacts") or [])
    # Skills sometimes persist a producer ref in required_artifacts while the
    # delivery projection stores the same artifact under its logical ID.
    # Resolve that alias for the floor calculation; this does not attest a
    # check or turn an unverified artifact into producer evidence.
    artifact_aliases = {
        str(row.get("ref")): str(row.get("artifact_id"))
        for row in artifact_rows
        if _text(row.get("ref")) and _text(row.get("artifact_id"))
    }
    stage_artifacts = {artifact_aliases.get(str(value), str(value)) for value in stage_artifacts}
    if ("historical_run" in stage_checks and replay_visualization_requested(
            str((delivery.get("requested_scope") or {}).get("summary") or ""))):
        stage_checks.add("visualization_output")
    required_checks = [row for row in checks if row.get("required", True) or row.get("check_id") in stage_checks]

    def verified(row):
        return (row.get("status") == "passed" and row.get("producer_owned") is True
                and row.get("required", True) is True
                and bool(_text(row.get("artifact_id"))) and bool(_text(row.get("revision"))))

    reasons: list[str] = []
    if not contract:
        reasons.append("trusted_delivery_contract_missing")
    if delivery.get("workflow_contract_error"):
        reasons.append(delivery["workflow_contract_error"])
    if not stage_checks:
        reasons.append("required_checks_missing")
    if not stage_artifacts:
        reasons.append("required_artifacts_missing")
    has_completed_stage = any(row.get("state") == "completed" for row in stages)
    if not has_completed_stage:
        reasons.append("completed_stage_missing")
    verified_checks = [row for row in required_checks if row.get("check_id") in stage_checks and verified(row)]
    if not verified_checks:
        reasons.append("producer_evidence_missing")
    evidence_sources = {(row["artifact_id"], row["revision"]) for row in verified_checks}
    output_checks = [row for row in required_checks if verified(row)
                     and (row["artifact_id"], row["revision"]) in evidence_sources]
    # Aliases may point to a producer source identity or an attested output ref.
    # A model-supplied path/revision alone is never evidence of that output.
    verified_artifacts = {artifact.get("artifact_id") for artifact in artifact_rows
        if _text(artifact.get("ref")) and any(
            (not artifact.get("revision") or artifact.get("revision") == check.get("revision")) and
            (artifact.get("ref") == check.get("artifact_id") or
             artifact.get("ref") == check.get("evidence_ref"))
            for check in output_checks)}
    has_verified_artifact = bool(stage_artifacts & verified_artifacts)
    if not has_verified_artifact:
        reasons.append("verified_artifact_missing")
    if any(row.get("state") == "blocked" for row in stages):
        reasons.append("stage_blocked")
    if any(row.get("state") not in {"completed", "skipped"} for row in stages):
        reasons.append("required_stage_pending")
    # Neither skipped stages nor limitations waive the trusted floor.
    for artifact in sorted(stage_artifacts):
        if artifact not in artifacts:
            reasons.append(f"artifact_missing:{artifact}")
        elif artifact not in verified_artifacts:
            reasons.append(f"artifact_unverified:{artifact}")
    for check_id in sorted(stage_checks):
        if not any(row.get("check_id") == check_id for row in checks):
            reasons.append(f"check_missing:{check_id}")
    for row in required_checks:
        if row.get("status") != "passed":
            reasons.append(f"check_{row.get('status') or 'unknown'}:{row['check_id']}")
        elif not verified(row):
            reasons.append(f"check_unverified:{row['check_id']}")
    reasons = list(dict.fromkeys(reasons))
    allowed = bool(stages) and not reasons
    # Optional producer checks do not block a scoped delivery, but their gaps
    # remain partial even if model-authored limitations are empty or cleared.
    optional_gap = any(row.get("producer_owned") is True and row.get("required") is False
                       and row.get("status") != "passed" for row in checks)
    partial = bool(delivery.get("limitations")) or optional_gap or any(row.get("state") == "skipped" for row in stages)
    status = ("partial" if partial else "complete") if allowed else "incomplete"
    if not allowed and has_completed_stage and has_verified_artifact and all(row.get("state") in {"completed", "skipped"} for row in stages) and delivery.get("limitations"):
        status = "partial"
    return {"allowed": allowed, "status": status, "reasons": reasons[:64], "computed_at": time.time()}


def _bind_contract(delivery: dict, contract: Mapping[str, Any] | None) -> bool:
    """Host-only input, excluded from every model-facing schema.

    Invalidation is sticky. Rebinding requires a new command; repeated start,
    resource rollback, or producer receipts cannot reset an existing binding.
    """
    if contract is not None and not delivery.get("workflow_contract_error"):
        old = delivery.get("workflow_contract")
        if contract.get("error"):
            delivery["workflow_contract_error"] = str(contract["error"])
        elif any(contract.get(key) != delivery.get(key) for key in ("skill_id", "workflow_id")):
            delivery["workflow_contract_error"] = "delivery_contract_binding_mismatch"
        elif old is not None and old != contract:
            delivery["workflow_contract_error"] = "delivery_contract_stale"
        else:
            delivery["workflow_contract"] = json.loads(json.dumps(contract))
    return bool(delivery.get("workflow_contract")) and not delivery.get("workflow_contract_error")


def new_delivery(*, command_id: str, skill_id: str, workflow_id: str, requested_scope: Mapping[str, Any], stages: list[Mapping[str, Any]], workflow_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    validate_delivery_input(dict(skill_id=skill_id, workflow_id=workflow_id, requested_scope=requested_scope, stages=stages), DELIVERY_START_SCHEMA)
    summary = _text(requested_scope.get("summary") if isinstance(requested_scope, Mapping) else requested_scope, 2048)
    limitations = [_text(x, 256) for x in (requested_scope.get("limitations") or [])[:32]] if isinstance(requested_scope, Mapping) else []
    row = {
        "protocol": PROTOCOL, "delivery_id": "delivery_" + uuid.uuid4().hex,
        "command_id": _safe_ref(command_id), "skill_id": _safe_ref(skill_id), "workflow_id": _safe_ref(workflow_id),
        "requested_scope": {"summary": summary, "scope_hash": _scope_hash(summary, limitations), "limitations": limitations},
        "stages": [_stage(x) for x in stages], "artifacts": [], "checks": [], "limitations": limitations,
        "completion": {"allowed": False, "status": "incomplete", "reasons": ["required_stage_pending"]},
        "recovery": _recovery(), "delivery_revision": 1, "updated_at": time.time(),
    }
    _bind_contract(row, workflow_contract)
    row["completion"] = _completion(row)
    return row


def delivery_command_chain(paths, con, command_id: str):
    """Current command followed only by durably verified decision predecessors."""
    from .core.config import Config
    from .agent.continuation_binding import trusted_continuation_source

    row = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()
    if not row:
        raise DeliveryError("command_not_found", 404)
    chain = []
    config = Config(paths=paths)
    while row is not None:
        if len(chain) >= 32:
            raise DeliveryError("delivery_continuation_invalid")
        chain.append(row)
        try:
            row = trusted_continuation_source(config, row, connection=con)
        except ValueError as exc:
            raise DeliveryError("delivery_continuation_invalid") from exc
    return chain


def _load(con, command_id: str, paths) -> tuple[dict[str, Any], int, str, str]:
    current = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()
    if not current:
        raise DeliveryError("command_not_found", 404)
    delivery = json.loads(current["delivery_json"] or "null")
    if isinstance(delivery, dict) and delivery.get("protocol") == PROTOCOL:
        if delivery.get("command_id") != command_id:
            raise DeliveryError("delivery_continuation_invalid")
        # Reading an already-bound local snapshot is not evidence inheritance.
        # Receipt discovery still validates the full continuation chain.
        return delivery, int(current["revision"]), str(current["session_id"]), str(current["turn_id"])
    chain = delivery_command_chain(paths, con, command_id)
    current = chain[0]
    for row in chain:
        delivery = json.loads(row["delivery_json"] or "null")
        if isinstance(delivery, dict) and delivery.get("protocol") == PROTOCOL:
            if delivery.get("command_id") != row["command_id"]:
                raise DeliveryError("delivery_continuation_invalid")
            delivery["command_id"] = command_id
            return delivery, int(current["revision"]), str(current["session_id"]), str(current["turn_id"])
    raise DeliveryError("delivery_not_started", 409)


def _event(con, command_id: str, kind: str, payload: Mapping[str, Any]) -> None:
    event_id = f"{kind}:{command_id}:{uuid.uuid4().hex}"
    con.execute("INSERT OR IGNORE INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)",
                (command_id, event_id, json.dumps({"kind": kind, "command_id": command_id, **dict(payload)}, ensure_ascii=False, separators=(",", ":"))))


def inherit_delivery(paths, con, command_id: str) -> None:
    """Bind trusted continuation evidence before its first tool can execute."""
    if con.execute("SELECT delivery_json FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()[0]:
        return
    try:
        delivery, revision, _, _ = _load(con, command_id, paths)
    except DeliveryError as exc:
        # A legacy/unbound continuation has no delivery to inherit. Keep its
        # command behavior unchanged; never substitute session-wide evidence.
        if exc.code in {"delivery_not_started", "delivery_continuation_invalid"}:
            return
        raise
    changed = con.execute(
        "UPDATE agent_commands SET delivery_json=?,revision=revision+1 WHERE command_id=? AND revision=? AND delivery_json IS NULL",
        (json.dumps(delivery, ensure_ascii=False, separators=(",", ":")), command_id, revision),
    ).rowcount
    if changed != 1:
        raise DeliveryError("delivery_revision_conflict", 409,
                            delivery=_load(con, command_id, paths)[0])
    _event(con, command_id, "delivery.stage", {"delivery": delivery, "delivery_revision": delivery["delivery_revision"]})


def start_delivery(paths, *, command_id: str, session_id: str, skill_id: str, workflow_id: str, requested_scope: Mapping[str, Any], stages: list[Mapping[str, Any]], workflow_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    delivery = new_delivery(command_id=command_id, skill_id=skill_id, workflow_id=workflow_id, requested_scope=requested_scope, stages=stages, workflow_contract=workflow_contract)
    with closing(connect(paths.db)) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT delivery_json,session_id,revision,request_json FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()
        if not row or str(row["session_id"]) != str(session_id):
            raise DeliveryError("command_not_found", 404)
        try:
            existing, _, _, _ = _load(con, command_id, paths)
        except DeliveryError as exc:
            if exc.code != "delivery_not_started":
                raise
        else:
            if existing.get("skill_id") != skill_id or existing.get("workflow_id") != workflow_id:
                raise DeliveryError("delivery_workflow_binding_conflict")
            return existing
        request=json.loads(row["request_json"])
        original=str((request.get("payload") or {}).get("text") or request.get("continuation_feedback") or "").strip()
        if request.get("source")=="goal_continue":
            goal=con.execute("SELECT objective FROM agent_goals WHERE session_id=?",(session_id,)).fetchone()
            original=goal[0] if goal else original
        if original:
            # A model may describe assumptions/limitations, but cannot amend
            # the operator request by rewriting its own delivery scope.
            scope=delivery["requested_scope"]
            scope["model_interpretation"]=scope["summary"]
            scope["summary"]=_text(original,16000)
            scope["authority"]="admitted_request"
            scope["scope_hash"]=_scope_hash(original,[])
            scope["limitations_are_authorization"]=False
        con.execute("UPDATE agent_commands SET delivery_json=?,revision=revision+1,updated_at=? WHERE command_id=? AND delivery_json IS NULL", (json.dumps(delivery, ensure_ascii=False, separators=(",", ":")), time.time(), command_id))
        _event(con, command_id, "delivery.stage", {"delivery": delivery, "delivery_revision": delivery["delivery_revision"]})
        con.commit()
    return delivery


def update_delivery(paths, *, command_id: str, session_id: str, expected_revision: int, stages: list[Mapping[str, Any]] | None = None, artifacts: list[Any] | None = None, checks: list[Mapping[str, Any]] | None = None, limitations: list[str] | None = None, recovery: Mapping[str, Any] | None = None, source_revision: str = "", producer_receipt: dict | None = None, source_versions: Mapping[str | tuple[str, str], str] | None = None, producer_receipts: list[dict] | None = None, reconcile_only: bool = False, workflow_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    args = dict(expected_revision=expected_revision, stages=stages, artifacts=artifacts, checks=checks,
                limitations=limitations, recovery=recovery, source_revision=source_revision)
    validate_delivery_input({key: value for key, value in args.items() if value is not None}, _DELIVERY_UPDATE_INTERNAL_SCHEMA)
    receipt = read_verification_receipt(producer_receipt) if producer_receipt is not None else None
    if producer_receipt is not None and receipt is None:
        raise DeliveryError("invalid_delivery_producer_receipt", 400)
    receipts = [read_verification_receipt(item) for item in producer_receipts or []]
    if any(item is None for item in receipts):
        raise DeliveryError("invalid_delivery_producer_receipt", 400)
    if receipt is not None:
        receipts.append(receipt)
    with closing(connect(paths.db)) as con:
        con.execute("BEGIN IMMEDIATE")
        delivery, command_revision, owner_session, _ = _load(con, command_id, paths)
        if owner_session != str(session_id):
            raise DeliveryError("command_not_found", 404)
        if receipt is None and int(delivery.get("delivery_revision") or 0) != int(expected_revision):
            raise DeliveryError("delivery_revision_conflict", 409, delivery=delivery)
        before_update = json.dumps(delivery, sort_keys=True)
        if workflow_contract is not None and not _bind_contract(delivery, workflow_contract):
            # Persist invalidation under the same CAS; reject content mutation.
            stages = artifacts = checks = limitations = recovery = None
            receipts, source_versions, source_revision = [], None, ""
        if stages is not None:
            by_id = {row["id"]: row for row in stages}
            if set(by_id) - {row["id"] for row in delivery["stages"]}:
                raise DeliveryError("unknown_delivery_stage", 400)
            for old in delivery["stages"]:
                patch = by_id.get(old["id"], {})
                merged = _stage({**old, **patch})
                for key in ("required_artifacts", "required_checks"):
                    merged[key] = list(dict.fromkeys(old[key] + merged[key]))
                old.update(merged)
        if artifacts is not None:
            existing = {row["artifact_id"]: row for row in delivery.get("artifacts", [])}
            existing.update({_artifact(row)["artifact_id"]: _artifact(row) for row in artifacts})
            if len(existing) > 128:
                raise DeliveryError("too_many_delivery_artifacts", 400)
            delivery["artifacts"] = list(existing.values())
        if checks is not None or receipts or source_versions or source_revision:
            existing = {(row.get("artifact_id", ""), row["check_id"]): row for row in delivery.get("checks", [])}
            for row in checks or []:
                item = _check(row, default_revision=source_revision)
                if any(old["check_id"] == item["check_id"] and old.get("producer_owned") for old in existing.values()):
                    raise DeliveryError("delivery_check_owned_by_producer", 400)
                old = existing.get(("", item["check_id"]), {})
                item["required"] = old.get("required", False) or item["required"]
                existing[("", item["check_id"])] = item
            versions = dict(source_versions or {})
            for incoming in receipts:
                artifact_id, revision = incoming["artifact_id"], incoming["revision"]
                # Explicit current-source observations dominate old receipts.
                if artifact_id not in (source_versions or {}):
                    versions[artifact_id] = revision
                for row in incoming["checks"]:
                    item = {**_check(row, default_revision=revision), "artifact_id": artifact_id, "producer_owned": True}
                    existing.pop(("", item["check_id"]), None)
                    old = existing.get((artifact_id, item["check_id"]), {})
                    # Model placeholders cannot promote optional producer checks.
                    item["required"] = item["required"] or old.get("required", False)
                    existing[(artifact_id, item["check_id"])] = item
                # Producer receipts already attest the durable output refs.
                # Project the three delivery-contract artifacts from those
                # checks so Skills do not have to rewrite producer paths.
                producer_artifacts = {
                    "source_binding": ("candidate", "strategy_candidate"),
                    "report_output": ("report", "backtest_report"),
                    "historical_run": ("run_receipt", "run_receipt"),
                }
                for check in incoming["checks"]:
                    alias = producer_artifacts.get(check.get("check_id"))
                    ref = check.get("evidence_ref")
                    if (alias and check.get("status") == "passed"
                            and isinstance(ref, str) and ref):
                        artifact_id, kind = alias
                        delivery.setdefault("artifacts", [])
                        existing_artifacts = {row.get("artifact_id") for row in delivery["artifacts"] if isinstance(row, dict)}
                        existing_row = next((row for row in delivery["artifacts"]
                                             if isinstance(row, dict) and row.get("artifact_id") == artifact_id), None)
                        if existing_row is not None:
                            # A prior model-authored registration may have
                            # used the run label as revision. Replace only
                            # that metadata with the producer receipt's
                            # source revision; the ref itself remains bounded
                            # to the already verified output.
                            existing_row["revision"] = revision
                            existing_row["status"] = "recorded"
                        else:
                            delivery["artifacts"].append({
                                "artifact_id": artifact_id, "kind": kind,
                                "label": artifact_id, "ref": ref,
                                "revision": revision, "status": "recorded",
                            })
            for item in existing.values():
                artifact_id = item.get("artifact_id", "")
                current = versions.get((artifact_id, item["check_id"]), versions.get(artifact_id))
                if current and item.get("revision") != current and item.get("status") == "passed":
                    item["status"] = "stale"
            if source_revision:
                for item in existing.values():
                    if item.get("revision") and item.get("revision") != source_revision and item.get("status") == "passed":
                        item["status"] = "stale"
            if len(existing) > 128:
                raise DeliveryError("too_many_delivery_checks", 400)
            delivery["checks"] = list(existing.values())
        if limitations is not None:
            delivery["limitations"] = [_text(x, 256) for x in limitations[:32] if _text(x, 256)]
        if recovery is not None:
            delivery["recovery"] = _recovery(recovery)
        completion = _completion(delivery)
        same_verdict = all(delivery.get("completion", {}).get(key) == completion[key]
                           for key in ("allowed", "status", "reasons"))
        persisted = con.execute("SELECT delivery_json FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()[0]
        if persisted and same_verdict and json.dumps(delivery, sort_keys=True) == before_update:
            return delivery
        delivery["delivery_revision"] = int(delivery.get("delivery_revision") or 0) + 1
        delivery["updated_at"] = time.time()
        delivery["completion"] = completion
        payload = json.dumps(delivery, ensure_ascii=False, separators=(",", ":"))
        changed = con.execute("UPDATE agent_commands SET delivery_json=?,revision=revision+1,updated_at=? WHERE command_id=? AND revision=?", (payload, time.time(), command_id, command_revision)).rowcount
        if changed != 1:
            raise DeliveryError("delivery_revision_conflict", 409,
                                delivery=_load(con, command_id, paths)[0])
        kind = "delivery.check" if checks is not None or receipts or source_versions else "delivery.artifact" if artifacts is not None else "delivery.recovery" if recovery is not None else "delivery.stage"
        _event(con, command_id, kind, {"delivery": delivery, "delivery_revision": delivery["delivery_revision"]})
        con.commit()
    return delivery


def finalize_delivery(paths, *, command_id: str, session_id: str, expected_revision: int, source_versions: Mapping[str | tuple[str, str], str] | None = None, producer_receipts: list[dict] | None = None, workflow_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return update_delivery(paths, command_id=command_id, session_id=session_id, expected_revision=expected_revision, source_versions=source_versions, producer_receipts=producer_receipts, workflow_contract=workflow_contract)


def record_delivery_verification(paths, *, command_id: str, receipt: dict) -> dict[str, Any] | None:
    """Host-only bridge. Never registered as a model tool or fed tool content."""
    with closing(connect(paths.db)) as con:
        try:
            delivery, _, session_id, _ = _load(con, command_id, paths)
        except DeliveryError as exc:
            if exc.code in {"command_not_found", "delivery_not_started"}:
                return None
            raise
    return update_delivery(paths, command_id=command_id, session_id=session_id,
                           expected_revision=delivery["delivery_revision"], producer_receipt=receipt)


def read_delivery(paths, *, command_id: str, session_id: str) -> dict[str, Any]:
    with closing(connect(paths.db)) as con:
        delivery, _, owner_session, _ = _load(con, command_id, paths)
    if owner_session != session_id:
        raise DeliveryError("command_not_found", 404)
    return delivery


def completion_is_current(paths, delivery: dict) -> bool:
    """Recheck producer source bindings before a later Goal claims completion."""
    from copy import deepcopy
    from .core.config import Config
    from .strategies.tool_verification import delivery_source_versions
    current=deepcopy(delivery)
    versions=delivery_source_versions(Config(paths=paths),current)
    for check in current.get("checks",[]):
        artifact_id = check.get("artifact_id", "")
        revision = versions.get((artifact_id, check["check_id"]), versions.get(artifact_id))
        if revision is not None and check.get("revision") != revision:
            check["status"]="stale"
    return bool(_completion(current)["allowed"])


def public_delivery(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


__all__ = ["PROTOCOL", "DeliveryError", "new_delivery", "start_delivery", "update_delivery", "finalize_delivery", "public_delivery"]
