"""Shared post-apply observation weighting helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


POST_APPLY_HEALTHY_STATUSES = frozenset(
    {"healthy", "passed", "ok", "stable", "improved"}
)
POST_APPLY_NEGATIVE_STATUSES = frozenset(
    {"regressed", "failed", "degraded", "rollback_recommended"}
)
OBSERVATION_HALF_LIFE_DAYS = 7.0
OBSERVATION_SOURCE_WEIGHT_CAP = 3.0


def summarize_observation_weights(
    rows: list[dict[str, Any]],
    *,
    default_status: str = "observing",
    half_life_days: float = OBSERVATION_HALF_LIFE_DAYS,
    source_weight_cap: float = OBSERVATION_SOURCE_WEIGHT_CAP,
) -> dict[str, Any]:
    """Return raw and weighted post-apply observation aggregates."""

    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    uncapped_weighted_by_status: dict[str, float] = {}
    source_status_weights: dict[str, dict[str, float]] = {}
    anchor = latest_observation_time(rows)
    for row in rows:
        status = _normal_status(row, default=default_status)
        source = str(row.get("source") or "unknown").lower()
        by_status[status] = by_status.get(status, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        weight = observation_weight(row, anchor=anchor, half_life_days=half_life_days)
        uncapped_weighted_by_status[status] = (
            uncapped_weighted_by_status.get(status, 0.0) + weight
        )
        source_status_weights.setdefault(source, {})
        source_status_weights[source][status] = (
            source_status_weights[source].get(status, 0.0) + weight
        )
    weighted_by_status, weighted_by_source = cap_observation_weights_by_source(
        source_status_weights,
        source_weight_cap=source_weight_cap,
    )
    weighted_negative = sum(
        weighted_by_status.get(status, 0.0)
        for status in POST_APPLY_NEGATIVE_STATUSES
    )
    weighted_healthy = sum(
        weighted_by_status.get(status, 0.0)
        for status in POST_APPLY_HEALTHY_STATUSES
    )
    weighted_observing = (
        weighted_by_status.get("observing", 0.0)
        + weighted_by_status.get("pending", 0.0)
    )
    return {
        "by_status": by_status,
        "by_source": by_source,
        "decay": {
            "half_life_days": half_life_days,
            "source_weight_cap": source_weight_cap,
            "anchor_observed_at": anchor.isoformat() if anchor else None,
        },
        "weighted_by_status": _round_map(weighted_by_status),
        "uncapped_weighted_by_status": _round_map(uncapped_weighted_by_status),
        "weighted_by_source": _round_map(weighted_by_source),
        "weighted_negative_count": round(weighted_negative, 4),
        "weighted_healthy_count": round(weighted_healthy, 4),
        "weighted_observing_count": round(weighted_observing, 4),
        "dominant_sources": [
            {
                "source": source,
                "raw_count": by_source.get(source, 0),
                "weight": round(weight, 4),
            }
            for source, weight in sorted(
                weighted_by_source.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:6]
        ],
    }


def latest_observation_time(rows: list[dict[str, Any]]) -> datetime | None:
    parsed = [parse_observed_at(row) for row in rows]
    parsed = [dt for dt in parsed if dt is not None]
    return max(parsed) if parsed else None


def observation_weight(
    row: dict[str, Any],
    *,
    anchor: datetime | None,
    half_life_days: float = OBSERVATION_HALF_LIFE_DAYS,
) -> float:
    if anchor is None:
        return 1.0
    observed = parse_observed_at(row)
    if observed is None:
        return 0.5
    age_days = max(0.0, (anchor - observed).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def parse_observed_at(row: dict[str, Any]) -> datetime | None:
    raw = row.get("observed_at") or row.get("ts")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cap_observation_weights_by_source(
    source_status_weights: dict[str, dict[str, float]],
    *,
    source_weight_cap: float = OBSERVATION_SOURCE_WEIGHT_CAP,
) -> tuple[dict[str, float], dict[str, float]]:
    weighted_by_status: dict[str, float] = {}
    weighted_by_source: dict[str, float] = {}
    for source, statuses in source_status_weights.items():
        source_total = sum(max(0.0, weight) for weight in statuses.values())
        if source_total <= 0:
            continue
        capped_total = min(source_weight_cap, source_total)
        scale = capped_total / source_total
        weighted_by_source[source] = capped_total
        for status, weight in statuses.items():
            weighted_by_status[status] = (
                weighted_by_status.get(status, 0.0) + max(0.0, weight) * scale
            )
    return weighted_by_status, weighted_by_source


def _normal_status(row: dict[str, Any], *, default: str) -> str:
    return str(row.get("status") or row.get("outcome") or default).lower()


def _round_map(values: dict[str, float]) -> dict[str, float]:
    return {
        key: round(value, 4)
        for key, value in sorted(values.items())
    }


__all__ = [
    "OBSERVATION_HALF_LIFE_DAYS",
    "OBSERVATION_SOURCE_WEIGHT_CAP",
    "POST_APPLY_HEALTHY_STATUSES",
    "POST_APPLY_NEGATIVE_STATUSES",
    "cap_observation_weights_by_source",
    "latest_observation_time",
    "observation_weight",
    "parse_observed_at",
    "summarize_observation_weights",
]
