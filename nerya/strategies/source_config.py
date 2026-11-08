"""Data-source dimensions. Declarative input configuration, not strategy generation."""
from __future__ import annotations
import json
import re
from typing import Any


def dimensions(source: dict[str, Any], plural: str, singular: str, fallback: list[str]) -> list[str]:
    if plural in source:
        values = source[plural]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"data source {plural} must be a non-empty list of strings")
        values = [v.strip() for v in values]
        if len(values) != len(set(values)):
            raise ValueError(f"data source {plural} must not contain duplicates")
        if singular in source and (not isinstance(source[singular], str) or values != [source[singular].strip()]):
            raise ValueError(f"Use {plural} or {singular}, not conflicting values")
        return values
    if singular in source:
        value = source[singular]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"data source {singular} must be a non-empty string")
        return [value.strip()]
    return list(dict.fromkeys(fallback))


def validate_source(source: dict[str, Any]) -> None:
    provider = source.get("provider", "runtime.market")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("data source provider must be a non-empty string")
    if provider == "data_api":
        allowed = {"id", "provider", "provider_id", "action", "args", "limit"}
        if set(source) - allowed:
            raise ValueError("data_api source accepts only id/provider/provider_id/action/args/limit")
        name = source.get("id")
        if not isinstance(name, str) or not name.strip() or len(name) > 193:
            raise ValueError("data_api source requires a non-empty id of at most 193 characters")
        for key in ("provider_id", "action"):
            value = source.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value):
                raise ValueError(f"data_api source requires an explicit {key} identifier")
        if not isinstance(source.get("args"), dict):
            raise ValueError("data_api source args must be an object matching the action schema")
        try:
            encoded = json.dumps(source["args"], ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("data_api source args must be finite JSON") from exc
        if len(encoded.encode("utf-8")) > 32_000:
            raise ValueError("data_api source args exceed 32 KB")
        limit = source.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("data_api source limit must be an integer between 1 and 500")
        return
    if provider in {"runtime.market", "runtime.news"} and any(k in source for k in ("provider_id", "action", "args")):
        raise ValueError("data_api source fields require explicit provider=data_api")
    dimensions(source, "markets", "market", [])
    frames = dimensions(source, "timeframes", "timeframe", [])
    limit = source.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("data source limit must be a positive integer")
    capability = source.get("capability", "candles")
    if provider == "runtime.market" and capability == "ticker" and frames:
        raise ValueError("A ticker is a snapshot; remove timeframe/timeframes")
    if provider == "runtime.news" and any(k in source for k in ("markets", "market", "timeframes", "timeframe")):
        raise ValueError("runtime.news uses sources, not market or candle timeframes")


def is_matrix(source: dict[str, Any]) -> bool:
    # Explicit plural configuration always keeps its stable envelope, even
    # when reduced to one combination. Legacy scalar sources stay unchanged.
    return "markets" in source or "timeframes" in source
