"""Data-source dimensions. Declarative input configuration, not strategy generation."""
from __future__ import annotations
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
    dimensions(source, "markets", "market", [])
    frames = dimensions(source, "timeframes", "timeframe", [])
    limit = source.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("data source limit must be a positive integer")
    provider = source.get("provider", "runtime.market")
    capability = source.get("capability", "candles")
    if provider == "runtime.market" and capability == "ticker" and frames:
        raise ValueError("A ticker is a snapshot; remove timeframe/timeframes")
    if provider == "runtime.news" and any(k in source for k in ("markets", "market", "timeframes", "timeframe")):
        raise ValueError("runtime.news uses sources, not market or candle timeframes")


def is_matrix(source: dict[str, Any]) -> bool:
    # Explicit plural configuration always keeps its stable envelope, even
    # when reduced to one combination. Legacy scalar sources stay unchanged.
    return "markets" in source or "timeframes" in source
