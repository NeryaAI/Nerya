"""Workspace-level scheduled reflection control.

The per-strategy tuning schedules already live in ``triggers/schedules.yml``.
This module owns the single workspace-wide "Dream" reflection schedule so the
dashboard can configure it without mixing it into strategy trading cadence.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import TriggerValidationError
from ..core.paths import WorkspacePaths
from ..triggers.schedule import ScheduleEntry, load_schedules, save_schedules


PERIODIC_REFLECTION_SCHEDULE_ID = "workspace_reflection_dream"
PERIODIC_REFLECTION_KIND = "evolution.reflect"
PERIODIC_REFLECTION_TARGET = "skill:evolution.reflect"
DEFAULT_REFLECTION_TIME = "03:00"
DEFAULT_REFLECTION_TIMEZONE = "Asia/Shanghai"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def get_periodic_reflection(paths: WorkspacePaths) -> dict[str, Any]:
    entry = _find_entry(paths)
    if entry is None:
        return _default_snapshot()
    return _entry_snapshot(entry, configured=True)


def configure_periodic_reflection(
    paths: WorkspacePaths,
    *,
    enabled: bool,
    time: str | None = None,
    cron: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    tz = (timezone or DEFAULT_REFLECTION_TIMEZONE).strip() or DEFAULT_REFLECTION_TIMEZONE
    cron_value = str(cron or "").strip()
    time_value = str(time or "").strip()
    if not cron_value:
        cron_value = _cron_from_time(time_value or DEFAULT_REFLECTION_TIME)
    else:
        # Validate and keep the explicit cron expression.
        ScheduleEntry(
            id="__validate_reflection_cron__",
            kind=PERIODIC_REFLECTION_KIND,
            cron=cron_value,
            enabled=False,
            target=PERIODIC_REFLECTION_TARGET,
            timezone=tz,
        )
    entry = ScheduleEntry(
        id=PERIODIC_REFLECTION_SCHEDULE_ID,
        kind=PERIODIC_REFLECTION_KIND,
        cron=cron_value,
        enabled=bool(enabled),
        target=PERIODIC_REFLECTION_TARGET,
        payload={
            "reason": "dream_reflection_schedule",
            "mode": "dream",
            "tuning": False,
            "trading": False,
        },
        timezone=tz,
    )
    entries = [
        e for e in load_schedules(paths)
        if e.id != PERIODIC_REFLECTION_SCHEDULE_ID
    ]
    entries.append(entry)
    save_schedules(paths, entries)
    return {"ok": True, "schedule": _entry_snapshot(entry, configured=True)}


def ensure_periodic_reflection(paths: WorkspacePaths) -> dict[str, Any]:
    entry = _find_entry(paths)
    if entry is not None:
        return _entry_snapshot(entry, configured=True)
    return configure_periodic_reflection(
        paths,
        enabled=False,
        time=DEFAULT_REFLECTION_TIME,
        timezone=DEFAULT_REFLECTION_TIMEZONE,
    )["schedule"]


def _find_entry(paths: WorkspacePaths) -> ScheduleEntry | None:
    for entry in load_schedules(paths):
        if entry.id == PERIODIC_REFLECTION_SCHEDULE_ID:
            return entry
    return None


def _cron_from_time(value: str) -> str:
    match = _TIME_RE.match(value)
    if not match:
        raise TriggerValidationError(
            "periodic reflection time must use HH:MM in 24-hour format"
        )
    hour, minute = match.groups()
    return f"{int(minute)} {int(hour)} * * *"


def _time_from_cron(value: str | None) -> str | None:
    if not value:
        return None
    parts = str(value).split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        return None
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError:
        return None
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return None
    return f"{hour:02d}:{minute:02d}"


def _default_snapshot() -> dict[str, Any]:
    cron = _cron_from_time(DEFAULT_REFLECTION_TIME)
    return {
        "id": PERIODIC_REFLECTION_SCHEDULE_ID,
        "kind": PERIODIC_REFLECTION_KIND,
        "target": PERIODIC_REFLECTION_TARGET,
        "enabled": False,
        "configured": False,
        "cron": cron,
        "time": DEFAULT_REFLECTION_TIME,
        "timezone": DEFAULT_REFLECTION_TIMEZONE,
    }


def _entry_snapshot(entry: ScheduleEntry, *, configured: bool) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "target": entry.target,
        "enabled": bool(entry.enabled),
        "configured": configured,
        "cron": entry.cron,
        "time": _time_from_cron(entry.cron),
        "timezone": entry.timezone or DEFAULT_REFLECTION_TIMEZONE,
        "payload": dict(entry.payload or {}),
    }
