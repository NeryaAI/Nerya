"""Prepare bounded public data before isolated custom replay.

The candidate declares routes, never returned rows, provenance or pass flags.
Only installed producer opt-ins can execute without workspace credentials.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from .....core.errors import TradingError
from .....data_api import build_data_api_registry
from .....data_api.types import DataApiError
from .....harness.cancellation import raise_if_cancelled

_MAX_BYTES = 4_000_000
_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_manifest(root: Path) -> dict | None:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open("replay_inputs.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32_000:
            raise TradingError("replay input manifest must be a bounded regular file")
        raw = stream.read(32_001)
    if len(raw) > 32_000:
        raise TradingError("replay input manifest exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"version", "datasets"} or value["version"] != 1:
        raise TradingError("replay_inputs.json requires version=1 and datasets")
    rows = value["datasets"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise TradingError("replay inputs require 1 to 4 datasets")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not {"id", "provider", "action", "args"} <= set(row)
                or set(row) - {"id", "provider", "action", "args", "producer_ref"}
                or not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
                or row["id"] in seen or not isinstance(row["args"], dict)
                or any(not isinstance(row[k], str) or not _ID.fullmatch(row[k]) for k in ("provider", "action"))):
            raise TradingError("invalid or duplicate replay dataset declaration")
        seen.add(row["id"])
    return value


def prepare_inputs(root: Path, *, registry=None, cancel_token=None, config=None) -> dict:
    manifest = read_manifest(root)
    if manifest is None:
        return {"files": {}, "receipts": [], "manifest": None}
    registry = registry or build_data_api_registry()
    files, receipts, total = {}, [], 0
    for declaration in manifest["datasets"]:
        raise_if_cancelled(cancel_token)
        try:
            cached = None
            if ('producer_ref' not in declaration and config is not None
                    and config.get('runtime.command_id')):
                from .....data_api.replay_snapshot import find_snapshot
                cached = find_snapshot(config, registry, declaration['provider'],
                                       declaration['action'], declaration['args'])
            producer_ref = declaration.get('producer_ref')
            if cached is not None:
                producer_ref, output = cached
            elif 'producer_ref' in declaration:
                registry.admit_replay_input(declaration['provider'], declaration['action'], declaration['args'])
                if config is None:
                    raise DataApiError('Explicit replay snapshot requires command context', retryable=False)
                from .....data_api.replay_snapshot import read
                output = read(config, producer_ref,
                              registry.resolve_provider(declaration['provider']),
                              registry._action_name(declaration['provider'], declaration['action']),
                              declaration['args'])
            else:
                output = registry.call_replay_input(
                    declaration["provider"], declaration["action"], declaration["args"],
                    cancel_token=cancel_token,
                )
        except DataApiError as exc:
            error = TradingError("replay input preparation failed: " + exc.message)
            # Preserve bounded producer diagnostics, not provider bodies/headers.
            detail = {key: value for key, value in exc.detail.items() if (
                key in {'provider', 'action', 'reason', 'failure_stage', 'rpc_provider', 'rpc_method'}
                and isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}', value))
                or (key == 'request_count' and type(value) is int and 0 <= value <= 100_000)
                or (key == 'http_status' and type(value) is int and 100 <= value <= 599)
                or (key == 'rpc_error_code' and type(value) is int and -(2**31) <= value < 2**31)
                or (key in {'coverage_complete', 'safe_to_retry'} and type(value) is bool)}
            error.receipt = detail
            raise error from exc
        raise_if_cancelled(cancel_token)
        if not isinstance(output, dict) or not isinstance(output.get("rows"), list):
            raise TradingError("replay input producer must return a bounded table")
        rows = output["rows"]
        envelope = output.get("_envelope")
        if not rows or len(rows) > 10_000 or not isinstance(envelope, dict):
            raise TradingError("replay input has no rows or provenance envelope")
        sources = envelope.get("sources")
        if (not isinstance(sources, list) or not sources
                or any(not isinstance(source, dict) or not isinstance(source.get("sha256"), str)
                       or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                       for source in sources)):
            raise TradingError("replay input lacks source hashes")
        # Preserve only the provider's public dataset and audited envelope.
        data = {"rows": rows, "_envelope": envelope}
        blob = json_bytes(data)
        total += len(blob)
        if total > _MAX_BYTES:
            raise TradingError("replay input byte limit exceeded")
        digest = hashlib.sha256(blob).hexdigest()
        name = declaration["id"]
        files[name + ".json"] = blob
        coverage = envelope.get("coverage") if isinstance(envelope.get("coverage"), dict) else {}
        gaps = coverage.get("missing_utc_days") or coverage.get("gaps_over_24h")
        nulls = coverage.get("null_counts") or {}
        partial = bool(envelope.get("provider_truncated") or gaps)
        partial |= coverage.get('requested_window_complete') is False
        partial |= any(isinstance(coverage.get(k), (int, float)) and math.isfinite(coverage[k])
                       and coverage[k] > 0 for k in ("leading_gap_seconds", "trailing_gap_seconds"))
        # Missing completeness is unknown, never success inferred from row count.
        complete = False if partial else (True if coverage.get("completeness") == "complete"
                                          and not any(nulls.values()) else None)
        receipts.append({"id": name, "provider": declaration["provider"], "action": declaration["action"],
            **({'producer_ref': producer_ref} if producer_ref is not None else {}),
            "sha256": digest, "rows": len(rows), "sources": sources,
            "requested_window": envelope.get("requested_window"), "coverage": coverage,
            "requested_window_complete": complete, "snapshot_file": "prepared_inputs/" + name + ".json",
            "observation_kind": envelope.get("observation_kind"),
            "data_kind": "unverified" if envelope.get("mock") is not False else "historical_observations",
            })
    if read_manifest(root) != manifest:
        raise TradingError("replay input declaration changed during preparation")
    return {"files": files, "receipts": receipts, "manifest": manifest}
