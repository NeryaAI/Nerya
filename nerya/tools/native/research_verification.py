"""Local, command-bound research capture/readback receipts; never replay evidence."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import urlsplit

from ...agent.file_state import compute_file_hash
from ...core.atomic_write import atomic_write_bytes
from ...data_api.types import DataApiError
from ...db.sqlite import connect_readonly
from ...task_delivery import DeliveryError, delivery_command_chain
from ..result_contracts import parse_compacted_kept_jsonish, parse_json_text
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult
from ..verification import attach_verification, read_verification_receipt, verification_receipt
from .paths import resolve_workspace_path

RESEARCH_VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "report_path": {"type": "string", "minLength": 1,
            "description": "Saved report under artifacts/conversations/<current-session>/; requires active research_brief delivery."},
        "evidence_call_ids": {"type": "array", "minItems": 1, "maxItems": 8,
            "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 160},
            "description": "Prefer omitting: automatically locate 1..8 successful native data_api calls from this command by report-cited source URLs. Optional exact IDs; web_fetch/MCP unsupported."},
    }, "required": ["report_path"],
}
# Only installed public adapters whose _envelope.sources is host provenance.
_ORIGINS = {
    "aave": {"api.v3.aave.com", "yields.llama.fi"},
    "defillama": {"yields.llama.fi"},
}
_TOKEN = r"[A-Za-z0-9_-]{1,160}"
_MAX = 1_048_576


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _path(config, ref):
    root = config.paths.root.resolve()
    raw = Path(ref)
    raw = raw if raw.is_absolute() else root / raw
    parts = raw.relative_to(root).parts
    if ".." in parts or any(root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)):
        raise ValueError("unsafe research path")
    return resolve_workspace_path(str(raw), root=root)


def _read(config, ref):
    path = _path(config, ref)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("research file must be regular")
        blob = stream.read(_MAX + 1)
    if len(blob) > _MAX:
        raise ValueError("research file exceeds 1 MiB")
    return blob


def _result(event):
    value = event.get("result")
    return (parse_compacted_kept_jsonish(value) or parse_json_text(value)) if isinstance(value, str) else value


def _events(con, command, action, call_id=None):
    rows = con.execute(
        "SELECT payload_json FROM agent_command_events WHERE command_id=? AND "
        "json_extract(payload_json,'$.kind')='tool.complete' AND "
        "json_extract(payload_json,'$.action')=? AND "
        "(? IS NULL OR json_extract(payload_json,'$.call_id')=?) ORDER BY id DESC LIMIT 64",
        (command["command_id"], action, call_id, call_id))
    for row in rows:
        event = json.loads(row[0])
        if (event.get("session_id") == command["session_id"]
                and event.get("turn_id") == command["turn_id"]
                and event.get("skill_id", "native") == "native"
                and event.get("ok") is True and not event.get("error")
                and event.get("semantic_success") is not False):
            yield event


def _bundle(config, con, command, report_ref, ids):
    if not report_ref.startswith(f"artifacts/conversations/{command['session_id']}/"):
        raise ValueError("report must belong to this conversation artifact directory")
    report = _read(config, report_ref)
    text = report.decode("utf-8")
    if ids is None:
        ids = [e.get("call_id") for e in _events(con, command, "data_api")
               if isinstance(e.get("payload"), dict) and e["payload"].get("op") == "call"
               and isinstance(r := _result(e), dict) and r.get("provider") in _ORIGINS
               and isinstance(envelope := r.get("_envelope"), dict)
               and isinstance(envelope.get("sources"), list)
               and any(isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"] in text
                       for s in envelope.get("sources", []))]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
            or any(not isinstance(i, str) or not 1 <= len(i) <= 160 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("require 1..8 unique exact data_api call IDs")
    report_hash = hashlib.sha256(report).hexdigest()
    writes = [e.get("payload") for e in _events(con, command, "write_file")]
    if not any(isinstance(w, dict) and isinstance(w.get("path"), str)
               and isinstance(w.get("contents"), str)
               and resolve_workspace_path(w["path"], root=config.paths.root) == _path(config, report_ref)
               and compute_file_hash(w["contents"]) == report_hash for w in writes):
        raise ValueError("report lacks a matching same-command write_file content hash")
    evidence, fresh, cited = [], True, bool(text.strip())
    for call_id in sorted(ids):
        events = list(_events(con, command, "data_api", call_id))
        if len(events) != 1:
            raise ValueError("missing or ambiguous successful native data_api event")
        request = events[0].get("payload")
        if not isinstance(request, dict) or request.get("op") != "call":
            raise ValueError("require data_api acquisition operation")
        result = _result(events[0])
        if not isinstance(result, dict):
            raise ValueError("source provenance unavailable")
        provider, action = result.get("provider"), result.get("action")
        from ...data_api import build_data_api_registry
        if (not isinstance(provider, str) or provider not in _ORIGINS or not isinstance(action, str)
                or request.get("provider") != provider or request.get("action") != action):
            raise ValueError("unsupported public data_api producer")
        build_data_api_registry().schema(provider, action)
        envelope = result.get("_envelope")
        if not isinstance(envelope, dict) or envelope.get("mock") is not False:
            raise ValueError("missing non-mock producer envelope")
        sources = envelope.get("sources")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
            raise ValueError("missing bounded source captures")
        captures = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("invalid source capture")
            url, stamp, digest = (source.get(k) for k in ("url", "retrieved_at", "sha256"))
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (not parsed or len(url) > 2048 or parsed.scheme != "https"
                    or parsed.hostname not in _ORIGINS[provider] or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError("invalid public source URL/hash")
            try:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                end = time.time() if command["state"] == "running" else command["updated_at"]
                fresh &= dt.tzinfo is not None and command["created_at"] <= dt.timestamp() <= end
            except (AttributeError, TypeError, ValueError, OverflowError):
                fresh = False
            captures.append(dict(url=url, retrieved_at=stamp if isinstance(stamp, str) else None, sha256=digest))
            cited &= url in text or bool(re.search(r"(?<![\w-])" + re.escape(call_id) + r"(?![\w-])", text))
        evidence.append(dict(call_id=call_id, provider=provider, action=action, sources=captures))
    manifest = dict(version=1, command_id=command["command_id"], session_id=command["session_id"],
        turn_id=command["turn_id"], report_path=report_ref, report_sha256=report_hash, evidence=evidence)
    return manifest, hashlib.sha256(report + _encoded(manifest)).hexdigest(), cited, fresh


def research_verify_handler(call: ToolCall, *, config) -> ToolResult:
    try:
        args = call.arguments or {}
        with closing(connect_readonly(config.paths.db)) as con:
            con.execute("BEGIN")
            command = con.execute("SELECT * FROM agent_commands WHERE command_id=?",
                                  (config.get("runtime.command_id"),)).fetchone()
            if (not command or command["state"] != "running"
                    or command["session_id"] != config.get("runtime.command_session_id")
                    or command["turn_id"] != config.get("runtime.command_turn_id")
                    or not all(re.fullmatch(_TOKEN, command[k]) for k in ("command_id", "session_id"))):
                raise ValueError("active command/session/turn binding unavailable")
            delivery = json.loads(command["delivery_json"] or "null")
            if (not isinstance(delivery, dict) or delivery.get("command_id") != command["command_id"]
                    or delivery.get("workflow_id") != "research_brief"):
                raise ValueError("start research_brief delivery before verification")
            ref = _path(config, args["report_path"]).relative_to(config.paths.root.resolve()).as_posix()
            manifest, revision, cited, fresh = _bundle(config, con, command, ref, args.get("evidence_call_ids"))
        manifest_ref = f"artifacts/conversations/{command['session_id']}/research/evidence-{revision[:12]}.json"
        target, blob = _path(config, manifest_ref), _encoded(manifest)
        if target.exists():
            if _read(config, manifest_ref) != blob:
                raise ValueError("existing research manifest changed")
        else:
            atomic_write_bytes(target, blob)
        readback = _read(config, ref)
        if hashlib.sha256(readback).hexdigest() != manifest["report_sha256"]:
            raise ValueError("report changed during verification")
        artifact_id = "research:" + command["command_id"]
        report_id = "research-report:" + command["command_id"]
        receipt = verification_receipt(artifact_id=artifact_id, revision=revision,
            scope="Declared source captures and saved report only", checks=[
                dict(check_id="source_coverage", status="passed" if cited else "failed", evidence_ref=artifact_id,
                     label="Declared source captures",
                     message="Declared captures cited by URL or exact call reference; not exhaustive question coverage."),
                dict(check_id="freshness", status="passed" if fresh else "failed", evidence_ref=artifact_id,
                     label="Producer retrieval times",
                     message="Producer retrieval times within command; not current market freshness."),
                dict(check_id="report_readback", status="passed" if readback.strip() else "failed",
                     label="Saved research report",
                     evidence_ref=report_id, message="Saved bytes match same-command write and bundle hash.")],
            unverified=["claim_accuracy", "exhaustive_source_coverage", "point_in_time_replay",
                        "trading_performance", "current_market_freshness"])
        from ...task_delivery import record_delivery_verification
        delivery = record_delivery_verification(config.paths, command_id=command["command_id"], receipt=receipt)
        result = ToolResult.from_json(tool_use_id=call.id, name=call.name, data={
            **({"delivery_revision": delivery["delivery_revision"],
                "delivery_completion": delivery["completion"]} if delivery else {}),
            "manifest_path": manifest_ref,
            "report_path": ref,
            "artifacts": [dict(artifact_id=kind, ref=path, revision=revision)
                          for kind, path in (("evidence", artifact_id), ("report", report_id))]})
        result.semantic_success = cited and fresh and bool(readback.strip())
        return attach_verification(result, receipt)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, DataApiError, DeliveryError):
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
            kind=ToolErrorKind.EXECUTION_ERROR, retryable=False,
            message="Research evidence missing/failed: start research_brief delivery; require 1..8 matching current-command public data_api captures (cite source URLs when omitting evidence_call_ids) and an exact same-command saved report write in this conversation's artifacts."))


def research_source_versions(config, delivery) -> dict[str, str]:
    """Read-only invalidation from durable producer receipts, files and events."""
    versions = {}
    for check in delivery.get("checks", []):
        artifact = check.get("artifact_id", "")
        if not check.get("producer_owned") or not re.fullmatch("research:" + _TOKEN, artifact) or artifact in versions:
            continue
        versions[artifact] = "research_stale"
        try:
            with closing(connect_readonly(config.paths.db)) as con:
                con.execute("BEGIN")
                command = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (artifact[9:],)).fetchone()
                current_id = delivery.get("command_id", command["command_id"] if command else "")
                chain = delivery_command_chain(config.paths, con, current_id)
                if (not command or command["command_id"] not in {c["command_id"] for c in chain}
                        or command["session_id"] != chain[0]["session_id"]
                        or (delivery.get("session_id") and delivery["session_id"] != command["session_id"])):
                    continue
                receipt = next((r for e in _events(con, command, "research_verify")
                    if (r := read_verification_receipt(e.get("verification"))) and r["artifact_id"] == artifact), None)
                if not receipt:
                    continue
                refs = {c["check_id"]: c["evidence_ref"] for c in receipt["checks"]}
                manifest_ref = f"artifacts/conversations/{command['session_id']}/research/evidence-{receipt['revision'][:12]}.json"
                if (refs.get("source_coverage") != artifact or refs.get("freshness") != artifact
                        or refs.get("report_readback") != "research-report:" + command["command_id"]):
                    continue
                saved = json.loads(_read(config, manifest_ref))
                manifest, revision, cited, fresh = _bundle(config, con, command, saved["report_path"],
                                                          [e["call_id"] for e in saved["evidence"]])
                if manifest == saved and revision == receipt["revision"]:
                    versions[artifact] = revision
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, DataApiError, DeliveryError):
            pass
    return versions
