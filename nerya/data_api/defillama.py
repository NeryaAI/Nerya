"""Bounded public DeFi deposit-yield reads; no wallet, credentials or writes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ..connectors.http import HttpTransport, UrllibHttp
from .types import DataActionSpec, DataApiContext, DataApiError


ORIGIN = "https://yields.llama.fi"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_POOLS = 100_000
MAX_HISTORY = 20_000
MAX_WINDOW_DAYS = 366
TIMEOUT_SECONDS = 15.0
_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}
_APY_FIELDS = {
    "apy": "deposit_apy_percent",
    "apyBase": "deposit_base_apy_percent",
    "apyReward": "deposit_reward_apy_percent",
}


def defillama_specs(http: HttpTransport | None = None) -> list[DataActionSpec]:
    provider = DefiLlamaYieldProvider(http)
    common = {
        "provider": "defillama",
        "tags": ("defi", "yield", "lending", "supply", "deposit", "apy", "aave", "usdc"),
        "docs_url": "https://github.com/DefiLlama/yield-server#readme",
        "output_kind": "table",
    }
    return [
        DataActionSpec(
            **common,
            action="yield_pools",
            title="DeFi deposit yield pools",
            description=(
                "Discover stable pool UUIDs and current deposit APY percent for DeFi "
                "protocols (including Aave USDC). Optional exact project, chain, symbol "
                "and underlying_token filters are combined with AND. Supply/fees APY "
                "is distinct from borrow APY; current rates are not historical returns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **{key: {"type": "string", "minLength": 1, "maxLength": 256,
                             "description": "Exact, case-sensitive match to source value."}
                       for key in ("project", "chain", "symbol", "underlying_token")},
                    "limit": dict(_LIMIT_SCHEMA),
                },
                "additionalProperties": False,
            },
            handler=provider.yield_pools,
        ),
        DataActionSpec(
            **common,
            action="yield_history",
            replay_input=True,
            title="DeFi deposit APY history",
            description=(
                "Read historical deposit APY observations for one discovered pool UUID, "
                "with exact current pool identity, source hashes and UTC coverage/gaps. "
                "Maximum window 366 days; default last 30 days ending now. APY percent "
                "is an annualized rate observation, not realized historical return. "
                "No filling or inferred completeness; nulls remain null."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pool_id": {"type": "string", "pattern": _UUID_PATTERN,
                                "description": "Exact UUID returned by yield_pools."},
                    **{key: {"type": "string", "maxLength": 40,
                             "description": "Inclusive UTC RFC3339 timestamp (Z/+00:00) or YYYY-MM-DD at UTC midnight."}
                       for key in ("start", "end")},
                    "limit": dict(_LIMIT_SCHEMA),
                },
                "required": ["pool_id"],
                "additionalProperties": False,
            },
            handler=provider.yield_history,
        ),
    ]


class DefiLlamaYieldProvider:
    def __init__(self, http: HttpTransport | None = None) -> None:
        self.http = http if http is not None else UrllibHttp(
            max_response_bytes=MAX_RESPONSE_BYTES,
            allowed_origins=(ORIGIN,),
            strict_json=True,
        )

    def _get(self, path: str, max_rows: int) -> tuple[list[Any], dict[str, Any]]:
        # Only internal constant paths or a validated UUID can reach the transport.
        url = ORIGIN + path
        try:
            status, doc = self.http.request("GET", url, headers={"Accept": "application/json"},
                                            timeout=TIMEOUT_SECONDS)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _error("Invalid or oversized DefiLlama JSON response") from exc
        except Exception as exc:
            raise DataApiError("DefiLlama transport failed", kind="provider_error",
                               detail={"source_url": url}, retryable=True) from exc
        if status != 200:
            raise DataApiError("DefiLlama HTTP request failed", kind="provider_error",
                               detail={"source_url": url, "http_status": status},
                               retryable=status == 429 or status >= 500)
        if not isinstance(doc, dict) or doc.get("status") != "success" or not isinstance(doc.get("data"), list):
            raise _error("DefiLlama response must contain status=success and a data array")
        if len(doc["data"]) > max_rows:
            raise _error("DefiLlama response exceeds row limit")
        digest = _hash(doc, max_bytes=MAX_RESPONSE_BYTES)
        return doc["data"], {
            "url": url, "retrieved_at": _iso(datetime.now(timezone.utc)),
            "sha256": digest, "hash_basis": "canonical_parsed_json_utf8_sorted_keys",
            "source_row_count": len(doc["data"]),
        }

    def _pools(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw, source = self._get("/pools", MAX_POOLS)
        by_id: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise _error("Pool must be an object")
            pool_id = _uuid(item.get("pool"), kind="provider_error")
            tokens = item.get("underlyingTokens")
            if tokens is not None and (not isinstance(tokens, list) or len(tokens) > 100
                                       or any(not _text(token) for token in tokens)):
                raise _error("Invalid pool underlyingTokens")
            row = {"pool_id": pool_id}
            for key in ("project", "chain", "symbol"):
                if not _text(item.get(key)):
                    raise _error(f"Invalid pool {key}")
                row[key] = item[key]
            row["underlying_tokens"] = tokens
            row["pool_meta"] = item.get("poolMeta")
            if row["pool_meta"] is not None and (not isinstance(row["pool_meta"], str) or len(row["pool_meta"]) > 1024):
                raise _error("Invalid pool poolMeta")
            row.update(_rates(item))
            if pool_id in by_id and by_id[pool_id] != row:
                raise _error("Conflicting duplicate pool identity", pool_id=pool_id)
            by_id[pool_id] = row
        return [by_id[key] for key in sorted(by_id)], source

    def yield_pools(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        _args(args, {"project", "chain", "symbol", "underlying_token", "limit"})
        filters = {key: value for key, value in args.items() if key != "limit"}
        if any(not _text(value) for value in filters.values()):
            raise _error("Filters must be nonempty strings of at most 256 characters", kind="schema_validation")
        limit = _limit(args)
        pools, source = self._pools()
        selected = [pool for pool in pools if all(
            value in (pool["underlying_tokens"] or []) if key == "underlying_token"
            else pool[key] == value for key, value in filters.items()
        )]
        return _result(selected, limit, {
            **_semantics(), "observation_kind": "current_snapshot",
            "source_timestamp": None, "freshness": "upstream_snapshot_time_unavailable",
            "filters": filters, "sources": [source], "pool_count": len(pools),
        })

    def yield_history(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        _args(args, {"pool_id", "start", "end", "limit"})
        pool_id = _uuid(args.get("pool_id"), kind="schema_validation")
        limit = _limit(args)
        end = _timestamp(args["end"], kind="schema_validation") if "end" in args else datetime.now(timezone.utc)
        try:
            start = (_timestamp(args["start"], kind="schema_validation") if "start" in args
                     else end - timedelta(days=30))
        except OverflowError as exc:
            raise _error("Invalid history window", kind="schema_validation") from exc
        if not timedelta(0) <= end - start <= timedelta(days=MAX_WINDOW_DAYS):
            raise _error("History requires start <= end and a window <= 366 days", kind="schema_validation")
        pools, pool_source = self._pools()
        pool = next((row for row in pools if row["pool_id"] == pool_id), None)
        if pool is None:
            raise _error("Pool UUID is absent from the current catalog; exact identity cannot be verified",
                         kind="not_found", pool_id=pool_id)
        raw, source = self._get("/chart/" + pool_id, MAX_HISTORY)
        by_time: dict[datetime, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise _error("History observation must be an object")
            stamp = _timestamp(item.get("timestamp"))
            for key in ("pool", "pool_id"):
                if key in item and _uuid(item[key], kind="provider_error") != pool_id:
                    raise _error("History pool identity conflicts with requested UUID")
            row = {"pool_id": pool_id, "timestamp": _iso(stamp), **_rates(item)}
            if stamp in by_time and by_time[stamp] != row:
                raise _error("Conflicting duplicate history timestamp", timestamp=_iso(stamp))
            by_time[stamp] = row
        stamps = sorted(by_time)
        selected = [by_time[stamp] for stamp in stamps if start <= stamp <= end]
        before = next((by_time[t] for t in reversed(stamps) if t < start), None)
        after = next((by_time[t] for t in stamps if t > end), None)
        identity = {key: pool[key] for key in (
            "pool_id", "project", "chain", "symbol", "underlying_tokens", "pool_meta"
        )}
        return _result(selected, limit, {
            **_semantics(), "observation_kind": "historical_rate_observations",
            "pool": identity, "pool_identity_scope": "current_catalog_metadata",
            "sources": [pool_source, source],
            "boundary_observations": {"before": before, "after": after},
            "requested_window": {"start": _iso(start), "end": _iso(end), "inclusive": True},
            "source_observed_range": {"start": _iso(stamps[0]) if stamps else None,
                                      "end": _iso(stamps[-1]) if stamps else None},
            "duplicate_timestamps_removed": len(raw) - len(by_time),
            "coverage": _coverage(selected, start, end),
        })


def _error(message: str, *, kind: str = "provider_error", **detail: Any) -> DataApiError:
    return DataApiError(message, kind=kind, detail=detail, retryable=False)


def _args(args: dict[str, Any], allowed: set[str]) -> None:
    if set(args) - allowed:
        raise _error("Unknown DefiLlama arguments; endpoint overrides are not supported", kind="schema_validation")


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip())


def _uuid(value: Any, *, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(_UUID_PATTERN, value):
        raise _error("pool_id must be a canonical hyphenated UUID", kind=kind)
    return str(UUID(value))


def _timestamp(value: Any, *, kind: str = "provider_error") -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise _error("Timestamp must be an ISO date or UTC timestamp", kind=kind)
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            stamp = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        else:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,6})?(?:Z|[+]00:00)", value):
                raise ValueError("UTC required")
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc)
    except ValueError as exc:
        raise _error("Invalid UTC timestamp", kind=kind) from exc


def _iso(stamp: datetime) -> str:
    return stamp.isoformat().replace("+00:00", "Z")


def _limit(args: dict[str, Any]) -> int:
    value = args.get("limit", 50)
    if type(value) is not int or not 1 <= value <= 500:
        raise _error("limit must be an integer between 1 and 500", kind="schema_validation")
    return value


def _rates(item: dict[str, Any]) -> dict[str, Any]:
    row = {}
    # These fields describe deposits. Never substitute apyBaseBorrow or divide by 100.
    for source, target in {**_APY_FIELDS, "tvlUsd": "tvl_usd"}.items():
        value = item.get(source)
        if value is not None:
            try:
                valid = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid or (source == "tvlUsd" and value < 0):
                raise _error(f"Invalid finite numeric field: {source}")
        row[target] = value
    return row


def _hash(value: Any, *, max_bytes: int | None = None) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise _error("Invalid JSON data") from exc
    if max_bytes is not None and len(raw) > max_bytes:
        raise _error("DefiLlama response exceeds byte limit")
    return hashlib.sha256(raw).hexdigest()


def _semantics() -> dict[str, Any]:
    return {
        "provider": "defillama", "mode": "public_read_only", "mock": False,
        "apy_unit": "percent_per_year", "apy_fields": _APY_FIELDS,
        "rate_semantics": "apyBase is deposit supply/fees; apyReward is deposit rewards; apy is total deposit APY. Borrow APY is excluded.",
        "return_semantics": "Annualized rate observations, not realized holding-period returns; no accrual, compounding or missing values inferred.",
    }


def _result(rows: list[dict[str, Any]], limit: int, envelope: dict[str, Any]) -> dict[str, Any]:
    # compact_data_result preserves only _envelope and source_url beside table rows.
    return {
        "rows": rows[:limit], "source_url": envelope["sources"][-1]["url"],
        "_envelope": {
            **envelope, "matched_row_count": len(rows), "returned_row_count": min(limit, len(rows)),
            "provider_limit": limit, "provider_truncated": len(rows) > limit,
            "rows_sha256": _hash(rows), "rows_hash_scope": "all_matching_normalized_rows_before_limits",
            "returned_rows_sha256": _hash(rows[:limit]),
            "returned_rows_hash_scope": "provider_rows_before_data_api_limit_and_column_projection",
        },
    }


def _coverage(rows: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    observed_days = {row["timestamp"][:10] for row in rows}
    days = [(start.date() + timedelta(days=offset)).isoformat()
            for offset in range((end.date() - start.date()).days + 1)]
    stamps = [_timestamp(row["timestamp"]) for row in rows]
    return {
        "scope": "all_matching_observations_before_limits",
        "observed_range": {"start": rows[0]["timestamp"] if rows else None,
                           "end": rows[-1]["timestamp"] if rows else None},
        "observations": len(rows), "observed_utc_days": len(observed_days),
        "requested_utc_days": len(days),
        "missing_utc_days": [day for day in days if day not in observed_days],
        "gaps_over_24h": [{"start": _iso(a), "end": _iso(b), "seconds": (b - a).total_seconds()}
                          for a, b in zip(stamps, stamps[1:]) if b - a > timedelta(days=1)],
        "leading_gap_seconds": (stamps[0] - start).total_seconds() if stamps else None,
        "trailing_gap_seconds": (end - stamps[-1]).total_seconds() if stamps else None,
        "null_counts": {field: sum(row[field] is None for row in rows) for field in _APY_FIELDS.values()},
        "completeness": "not_asserted",
        "note": "UTC day presence does not prove full-day coverage, daily cadence or realized yield. No resampling or filling performed.",
    }
