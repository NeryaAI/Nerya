"""Track scheduled scripts inside ``workspace/triggers/schedules.yml``.

This file is read by whatever cron/agent loop lives in the host — we
only manage the list here; Nerya itself does not run a scheduler
daemon.

History & shape
---------------
The module used to only accept ``every_seconds``-style intervals and a
synthetic ``script_tick:<id>`` kind. Strategy authors that wanted a
5-minute cron or a weekday-only window had to drop down to the trigger
schedule yaml by hand.

This revision lifts the full trigger-plane schedule surface
(:class:`nerya.triggers.schedule.ScheduleEntry`) to the script layer:

* ``every_seconds`` **or** ``cron`` (not both).
* ``starts_at`` / ``ends_at`` windowing.
* ``enabled`` kill-switch.
* arbitrary ``target`` — defaults to ``main`` for legacy script ticks,
  but callers can schedule a prompt+subagent tick by passing
  ``target="subagent:<name>"`` and a ``kind``/``payload`` of their
  choice. The scheduler is now the single entrypoint for
  "fire-something-on-a-clock" regardless of whether the firing ends up
  running a script or asking a subagent/prompt to run.
"""

from __future__ import annotations

from typing import Any

from ..core import yaml_io
from ..core.errors import TriggerValidationError
from ..core.paths import WorkspacePaths


def _validate_cadence(
    *, every_seconds: int | None, cron: str | None, entry_id: str,
) -> None:
    """Enforce XOR(every_seconds, cron) + minimal cron shape check.

    We deliberately don't import the full cron parser from
    ``nerya.triggers.schedule`` — the ``scripts`` package must not
    depend on ``triggers`` (see docs/runtime-ownership.md). The trigger
    plane re-validates every row when the scheduler loop loads them.
    """
    if (every_seconds is None) == (cron is None):
        raise TriggerValidationError(
            f"schedule {entry_id!r} must set exactly one of "
            f"every_seconds or cron"
        )
    if cron is not None:
        parts = str(cron).split()
        if len(parts) != 5:
            raise TriggerValidationError(
                f"schedule {entry_id!r} cron must have 5 fields, "
                f"got {len(parts)}: {cron!r}"
            )
    if every_seconds is not None and int(every_seconds) < 1:
        raise TriggerValidationError(
            f"schedule {entry_id!r} every_seconds must be >= 1"
        )


def schedule(
    paths: WorkspacePaths, *,
    script_id: str,
    every_seconds: int | None = None,
    cron: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    enabled: bool = True,
    kind: str | None = None,
    strategy_id: str | None = None,
    target: str = "main",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert a schedule entry for ``script_id``.

    Exactly one of ``every_seconds`` or ``cron`` must be provided.
    All other fields are optional.
    """
    entry_id = f"script:{script_id}"
    _validate_cadence(
        every_seconds=every_seconds, cron=cron, entry_id=entry_id,
    )

    doc = yaml_io.load(paths.triggers_schedules_file,
                       default={"schedules": []}) or {}
    entries = list(doc.get("schedules") or [])
    entries = [e for e in entries if e.get("id") != entry_id]

    merged_payload = dict(payload or {})
    merged_payload.setdefault("script_id", script_id)

    row: dict[str, Any] = {
        "id": entry_id,
        "kind": kind or f"script_tick:{script_id}",
        "target": target,
        "strategy_id": strategy_id,
        "payload": merged_payload,
        "enabled": bool(enabled),
    }
    if every_seconds is not None:
        row["every_seconds"] = int(every_seconds)
    if cron is not None:
        row["cron"] = str(cron).strip()
    if starts_at is not None:
        row["starts_at"] = starts_at
    if ends_at is not None:
        row["ends_at"] = ends_at

    entries.append(row)
    doc["schedules"] = entries
    yaml_io.dump(paths.triggers_schedules_file, doc)
    return {
        "script_id": script_id, "scheduled": True,
        "every_seconds": every_seconds, "cron": cron,
        "starts_at": starts_at, "ends_at": ends_at,
        "target": target, "enabled": bool(enabled),
    }


def schedule_prompt(
    paths: WorkspacePaths, *,
    schedule_id: str,
    subagent: str,
    kind: str,
    every_seconds: int | None = None,
    cron: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    enabled: bool = True,
    strategy_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Schedule a *prompt+subagent* tick.

    This is a thin helper around the full trigger-plane schedule yaml
    so strategy authors can wire "every 15 minutes, have the
    ``risk_watcher`` subagent review the book" without needing to know
    the on-disk schedule schema.
    """
    if not subagent:
        raise TriggerValidationError("schedule_prompt requires a subagent name")
    entry_id = f"prompt:{schedule_id}"
    _validate_cadence(
        every_seconds=every_seconds, cron=cron, entry_id=entry_id,
    )

    doc = yaml_io.load(paths.triggers_schedules_file,
                       default={"schedules": []}) or {}
    entries = list(doc.get("schedules") or [])
    entries = [e for e in entries if e.get("id") != entry_id]

    row: dict[str, Any] = {
        "id": entry_id,
        "kind": kind,
        "target": f"subagent:{subagent}",
        "strategy_id": strategy_id,
        "payload": dict(payload or {}),
        "enabled": bool(enabled),
    }
    if every_seconds is not None:
        row["every_seconds"] = int(every_seconds)
    if cron is not None:
        row["cron"] = str(cron).strip()
    if starts_at is not None:
        row["starts_at"] = starts_at
    if ends_at is not None:
        row["ends_at"] = ends_at

    entries.append(row)
    doc["schedules"] = entries
    yaml_io.dump(paths.triggers_schedules_file, doc)
    return {
        "schedule_id": schedule_id, "subagent": subagent,
        "scheduled": True,
        "every_seconds": every_seconds, "cron": cron,
        "starts_at": starts_at, "ends_at": ends_at,
        "enabled": bool(enabled),
    }


__all__ = ["schedule", "schedule_prompt"]
