"""Read-only schedule validation using the same calendar as the scheduler.

No workspace, kernel, delivery or exchange access is required by this module.
Wall-clock inputs are resolved here rather than in the browser's local zone.
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import math
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schedule import ScheduleEntry
from .schedule_clock import next_due


class PreviewError(ValueError):
    def __init__(self, code: str, *, choices: list[dict[str, Any]] | None = None):
        super().__init__(code)
        self.code = code
        self.choices = choices or []


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_wall_time(value: str, zone_name: str, *, fold: int | None = None,
                      existing_instant: str | None = None) -> str:
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise PreviewError("invalid_timezone") from exc
    try:
        local = datetime.fromisoformat(value)
        if local.tzinfo is not None or len(value) not in (16, 19):
            raise ValueError("expected local date and time")
    except (ValueError, TypeError) as exc:
        raise PreviewError("invalid_local_time") from exc
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise PreviewError("invalid_time_fold")
    choices: dict[float, dict[str, Any]] = {}
    for candidate_fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=candidate_fold)
        stamp = aware.timestamp()
        # UTC round-trip rejects a wall time skipped by a daylight-saving jump.
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local:
            choices.setdefault(stamp, {"fold": candidate_fold, "utc": _utc(stamp), "local": aware.isoformat()})
    if not choices:
        raise PreviewError("nonexistent_local_time")
    options = list(choices.values())
    if len(options) == 1:
        return options[0]["utc"]
    if fold is not None:
        return next(option["utc"] for option in options if option["fold"] == fold)
    # Editing an unchanged historical instant must preserve which repeated hour it used.
    if existing_instant:
        try:
            instant = datetime.fromisoformat(existing_instant.replace("Z", "+00:00"))
            if instant.tzinfo is not None and instant.timestamp() in choices:
                return choices[instant.timestamp()]["utc"]
        except (ValueError, TypeError, AttributeError):
            pass
    raise PreviewError("ambiguous_local_time", choices=options)


def preview_schedule(payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Validate a definition and return up to five future occurrences, without saving."""
    raw = payload.get("schedule", payload)
    if not isinstance(raw, dict):
        raise PreviewError("invalid_schedule")
    names = {field.name for field in fields(ScheduleEntry)}
    data = {key: value for key, value in raw.items() if key in names}
    data.setdefault("id", "schedule_preview")
    data.setdefault("kind", "agent.task")
    data["enabled"] = True  # Preview also works for disabled drafts; no writes occur.
    data["archived"] = False
    zone_name = data.get("timezone") or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise PreviewError("invalid_timezone") from exc
    if "wall_time" in raw:
        data["run_at"] = resolve_wall_time(raw["wall_time"], zone_name,
            fold=raw.get("fold"), existing_instant=data.get("run_at"))
    entry = ScheduleEntry(**data)
    count = payload.get("count", 3)
    if type(count) is not int or not 1 <= count <= 5:
        raise PreviewError("invalid_preview_count")
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise PreviewError("invalid_preview_time")
    start = datetime.fromisoformat(entry.starts_at.replace("Z", "+00:00")).timestamp() if entry.starts_at else None
    end = datetime.fromisoformat(entry.ends_at.replace("Z", "+00:00")).timestamp() if entry.ends_at else None
    cursor = max(current, start - 0.001) if start is not None else current
    if entry.every_seconds is not None and entry.anchor_at is None:
        entry.anchor_at = current
    occurrences = []
    for _ in range(count):
        stamp = next_due(entry, cursor)
        if stamp is None or end is not None and stamp >= end:
            break
        occurrences.append({"utc": _utc(stamp), "local": datetime.fromtimestamp(stamp, zone).isoformat()})
        cursor = stamp
    return {
        "ok": True, "read_only": True,
        "schedule": {"cron": entry.cron, "every_seconds": entry.every_seconds,
                     "run_at": entry.run_at, "timezone": zone_name},
        "occurrences": occurrences,
        "interval_unanchored": bool(entry.every_seconds and raw.get("anchor_at") is None),
        "overlap_policy": entry.overlap_policy,
    }
