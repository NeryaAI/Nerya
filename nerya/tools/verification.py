"""Small producer-owned verification receipts, separate from tool success.

Receipts describe checks of one immutable source version. They are not trading
authority, a profitability claim, or proof that every part of a task is complete.
Only a host producer attaches them to ToolResult metadata; JSON in tool content
(including MCP results, source documents and model output) is never promoted.
"""
from __future__ import annotations

from typing import Any
import re

from .types import ToolResult
from ..core.redaction import redact_public_record

VERIFICATION_PROTOCOL = "nerya.verification.v1"
CHECK_STATUSES = frozenset({"passed", "failed", "missing", "pending", "stale"})


def verification_receipt(*, artifact_id: str, revision: str, scope: str,
                         checks: list[dict[str, Any]], unverified: list[str] | None = None) -> dict[str, Any]:
    receipt = {
        "protocol": VERIFICATION_PROTOCOL, "artifact_id": artifact_id,
        "revision": revision, "scope": scope, "checks": checks,
        "unverified": unverified or [],
    }
    validated = read_verification_receipt(receipt)
    if validated is None:
        raise ValueError("Invalid host verification receipt")
    return validated


def read_verification_receipt(value: Any) -> dict[str, Any] | None:
    """Validate and bound the host metadata channel, never infer from prose."""
    if not isinstance(value, dict) or value.get("protocol") != VERIFICATION_PROTOCOL:
        return None
    if any(not isinstance(value.get(key), str) or not value[key].strip()
           or len(value[key]) > 1024 for key in ("artifact_id", "revision", "scope")):
        return None
    rows = value.get("checks")
    unverified = value.get("unverified", [])
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        return None
    if (not isinstance(unverified, list) or len(unverified) > 32
            or any(not isinstance(item, str) or len(item) > 1024 for item in unverified)):
        return None
    checks, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        check_id, status = row.get("check_id"), row.get("status")
        if (not isinstance(check_id, str) or not check_id or len(check_id) > 128
                or check_id in seen or not isinstance(status, str) or status not in CHECK_STATUSES
                or type(row.get("required", True)) is not bool):
            return None
        check = {"check_id": check_id, "status": status, "required": row.get("required", True)}
        for key in ("label", "message", "evidence_ref"):
            text = row.get(key, "")
            if not isinstance(text, str) or len(text) > 2048:
                return None
            check[key] = text
        checks.append(check)
        seen.add(check_id)
    return {"protocol": VERIFICATION_PROTOCOL, **{key: value[key] for key in ("artifact_id", "revision", "scope")},
            "checks": checks, "unverified": list(unverified)}


def attach_verification(result: ToolResult, receipt: dict[str, Any]) -> ToolResult:
    """Keep machine evidence out of inference paths and available to the UI."""
    validated = public_verification_receipt(receipt)
    if validated is None:
        raise ValueError("Invalid host verification receipt")
    result.metadata["verification"] = validated
    # Retain an existing domain protocol; this receipt is an independent channel.
    if not result.result_protocol:
        result.result_protocol = VERIFICATION_PROTOCOL
    for part in result.content:
        if part.type == "json" and isinstance(part.data, dict):
            part.data["verification"] = validated
            break
    return result


def public_verification_receipt(value: Any) -> dict[str, Any] | None:
    """Expose only redacted receipt text; a producer's source hash is public."""
    receipt = read_verification_receipt(value)
    if not receipt:
        return None
    public = redact_public_record(receipt, identity_fields=frozenset({"revision"}))
    for original, projected in zip(receipt["checks"], public["checks"]):
        if original["check_id"] not in {"request_scope_match", "candidate_scope_match"}:
            continue
        ref = original["evidence_ref"]
        match = re.fullmatch(r"request_scope:([A-Za-z0-9_-]{8,160}):([a-f0-9]{64})(?::([a-f0-9]{64}):([A-Za-z0-9_./-]+/metrics[.]json))?", ref)
        if not match:
            continue
        path = match[4]
        if path and (path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))):
            continue
        # Hashes are public identity, not credentials. Retain redaction of the
        # command/path portions instead of granting this entire string a bypass.
        safe = redact_public_record({"command":match[1], "path":path or ""}, identity_fields=frozenset())
        projected["evidence_ref"] = "request_scope:" + safe["command"] + ":" + match[2]
        if path:
            projected["evidence_ref"] += ":" + match[3] + ":" + safe["path"]
    return public
