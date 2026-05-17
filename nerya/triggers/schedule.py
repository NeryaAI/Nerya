"""Read scheduled trigger sources from workspace/triggers/schedules.yml. extends the schedule schema beyond ``every_seconds``:

* ``cron``: a POSIX-style 5-field cron expression (minute hour dom month dow).
* ``starts_at`` / ``ends_at``: ISO timestamps gating when the schedule is
  live. A schedule is only due if ``starts_at <= now < ends_at``.
* ``enabled``: boolean flag operators can flip without deleting the row.

``every_seconds`` and ``cron`` are mutually exclusive — exactly one must
be supplied. The old ``every_seconds``-only shape remains the default so
existing workspaces keep working untouched.

compatibility extension (2026-04-24)
------------------------------------
Four optional fields on ``ScheduleEntry`` let a schedule behave as a
"scheduled agent session" rather than a plain trigger emitter:

* ``session_kind``: ``"trigger"`` (default, legacy) or ``"agent"``. An
  ``agent`` schedule, when due, spawns a fresh session and runs one
  agent turn with the configured skills attached.
* ``attached_skills``: skill names the scheduled session is allowed to
  call. Empty list = no restriction beyond the strategy's own policy.
* ``delivery_targets``: list of routed outputs (``messages`` channel or
  webhook URL) applied after the agent turn completes.
* ``session_ttl_seconds``: optional wallclock ceiling for the scheduled
  agent session; ``None`` means single-turn-and-close.

All four are backwards compatible: legacy YAML without these keys keeps
parsing and behaves identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core import yaml_io
from ..core.errors import TriggerValidationError
from ..core.paths import WorkspacePaths


_VALID_SESSION_KINDS = frozenset({"trigger", "agent"})
_VALID_DELIVERY_KINDS = frozenset({"messages", "webhook"})


@dataclass
class ScheduleEntry:
    id: str
    kind: str
    every_seconds: int | None = None
    cron: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool = True
    target: str = "main"
    strategy_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timezone: str | None = None
    # ---- compatibility cron/session extension (all optional) -----------
    session_kind: str = "trigger"
    attached_skills: list[str] = field(default_factory=list)
    delivery_targets: list[dict[str, Any]] = field(default_factory=list)
    session_ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.every_seconds is None and not self.cron:
            raise TriggerValidationError(
                f"schedule {self.id!r} must set either every_seconds or cron"
            )
        if self.every_seconds is not None and self.cron:
            raise TriggerValidationError(
                f"schedule {self.id!r} cannot set both every_seconds and cron"
            )
        if self.cron:
            _parse_cron(self.cron)  # raises if malformed
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise TriggerValidationError(
                    f"schedule {self.id!r} timezone is unknown: "
                    f"{self.timezone!r}"
                ) from exc
        if self.session_kind not in _VALID_SESSION_KINDS:
            raise TriggerValidationError(
                f"schedule {self.id!r} session_kind must be one of "
                f"{sorted(_VALID_SESSION_KINDS)!r}, got {self.session_kind!r}"
            )
        if self.attached_skills and self.session_kind != "agent":
            raise TriggerValidationError(
                f"schedule {self.id!r} attached_skills only apply to "
                f"session_kind='agent'"
            )
        for tgt in self.delivery_targets:
            if not isinstance(tgt, dict) or "kind" not in tgt:
                raise TriggerValidationError(
                    f"schedule {self.id!r} delivery_targets must each be a "
                    f"dict with a 'kind' field"
                )
            if tgt["kind"] not in _VALID_DELIVERY_KINDS:
                raise TriggerValidationError(
                    f"schedule {self.id!r} delivery_targets[].kind must be "
                    f"one of {sorted(_VALID_DELIVERY_KINDS)!r}, got "
                    f"{tgt['kind']!r}"
                )
        if self.session_ttl_seconds is not None and self.session_ttl_seconds < 0:
            raise TriggerValidationError(
                f"schedule {self.id!r} session_ttl_seconds must be >= 0"
            )

    # ---------------------------------------------------------- windowing
    def in_window(self, now: datetime) -> bool:
        """Return True iff the schedule is live at ``now``."""
        if not self.enabled:
            return False
        if self.starts_at:
            try:
                ts = datetime.fromisoformat(self.starts_at.replace("Z", "+00:00"))
                if now < ts:
                    return False
            except Exception:
                pass
        if self.ends_at:
            try:
                ts = datetime.fromisoformat(self.ends_at.replace("Z", "+00:00"))
                if now >= ts:
                    return False
            except Exception:
                pass
        return True

    # ---------------------------------------------------------- due-check
    def is_due(self, *, now: datetime, last_fired: datetime | None) -> bool:
        """Decide whether the schedule should fire at ``now``.

        * ``every_seconds``: due iff ``now - last_fired >= every_seconds``
          (always due on first run).
        * ``cron``: due iff the cron expression matches the current minute
          and the entry hasn't already fired in this minute.
        """
        if not self.in_window(now):
            return False
        if self.every_seconds is not None:
            if last_fired is None:
                return True
            elapsed = (now - last_fired).total_seconds()
            return elapsed >= max(1, self.every_seconds)
        assert self.cron is not None
        match_now = self._cron_time(now)
        if not _cron_matches(self.cron, match_now):
            return False
        # Avoid firing twice within the same minute for cron schedules.
        if last_fired is not None:
            minute_now = match_now.replace(second=0, microsecond=0)
            minute_last = self._cron_time(last_fired).replace(second=0, microsecond=0)
            if minute_last >= minute_now:
                return False
        return True

    def _cron_time(self, value: datetime) -> datetime:
        if not self.timezone:
            return value
        return value.astimezone(ZoneInfo(self.timezone))


def load_schedules(paths: WorkspacePaths) -> list[ScheduleEntry]:
    doc = yaml_io.load(paths.triggers_schedules_file, default={}) or {}
    out: list[ScheduleEntry] = []
    for row in doc.get("schedules") or []:
        kwargs: dict[str, Any] = {
            "id": row["id"],
            "kind": row.get("kind") or row["id"],
            "target": row.get("target") or "main",
            "strategy_id": row.get("strategy_id"),
            "payload": row.get("payload") or {},
            "timezone": row.get("timezone"),
            "starts_at": row.get("starts_at"),
            "ends_at": row.get("ends_at"),
            "enabled": bool(row.get("enabled", True)),
            # compatibility extension (backwards compatible defaults).
            "session_kind": str(row.get("session_kind") or "trigger"),
            "attached_skills": list(row.get("attached_skills") or []),
            "delivery_targets": list(row.get("delivery_targets") or []),
        }
        ttl = row.get("session_ttl_seconds")
        if ttl is not None:
            kwargs["session_ttl_seconds"] = int(ttl)
        has_cron = bool(row.get("cron"))
        has_every = "every_seconds" in row and row["every_seconds"] is not None
        if has_cron and has_every:
            raise TriggerValidationError(
                f"schedule {row.get('id')!r} cannot set both "
                f"every_seconds and cron"
            )
        if has_cron:
            kwargs["cron"] = str(row["cron"]).strip()
        else:
            # Default interval mirrors the old behaviour (60s) only when
            # the caller gave nothing at all.
            kwargs["every_seconds"] = int(row.get("every_seconds") or 60)
        out.append(ScheduleEntry(**kwargs))
    return out


def save_schedules(paths: WorkspacePaths, entries: list[ScheduleEntry]) -> None:
    """Round-trip ``entries`` back to ``schedules.yml``.

    Only non-default fields are serialised so legacy workspaces stay
    clean when we rewrite them. This is the canonical inverse of
    :func:`load_schedules`.
    """

    rows: list[dict[str, Any]] = []
    for e in entries:
        row: dict[str, Any] = {
            "id": e.id,
            "kind": e.kind,
            "target": e.target,
            "enabled": e.enabled,
        }
        if e.every_seconds is not None:
            row["every_seconds"] = int(e.every_seconds)
        if e.cron:
            row["cron"] = e.cron
        if e.starts_at:
            row["starts_at"] = e.starts_at
        if e.ends_at:
            row["ends_at"] = e.ends_at
        if e.strategy_id:
            row["strategy_id"] = e.strategy_id
        if e.payload:
            row["payload"] = dict(e.payload)
        if e.timezone:
            row["timezone"] = e.timezone
        if e.session_kind and e.session_kind != "trigger":
            row["session_kind"] = e.session_kind
        if e.attached_skills:
            row["attached_skills"] = list(e.attached_skills)
        if e.delivery_targets:
            row["delivery_targets"] = [dict(t) for t in e.delivery_targets]
        if e.session_ttl_seconds is not None:
            row["session_ttl_seconds"] = int(e.session_ttl_seconds)
        rows.append(row)
    paths.triggers_schedules_file.parent.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(paths.triggers_schedules_file, {"schedules": rows})


# ================================================================== cron
# A deliberately tiny POSIX-cron-subset parser / matcher. We do *not*
# pull in a heavy dependency; the language we support is:
#
#   * minute:       0-59 or ``*`` or comma-separated list or ``*/N`` step
#   * hour:         0-23, same grammar
#   * day-of-month: 1-31, same grammar
#   * month:        1-12, same grammar
#   * day-of-week:  0-6 (Sun=0), same grammar
#
# That covers 95% of operator use-cases (``"*/5 * * * *"``, ``"0 9 * * 1-5"``)
# without dragging in crontab-quirk semantics we'd only have to un-learn.

_CRON_RANGES = {
    0: (0, 59),
    1: (0, 23),
    2: (1, 31),
    3: (1, 12),
    4: (0, 6),
}


def _parse_field(field_text: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the set of integers it matches."""
    values: set[int] = set()
    for part in field_text.split(","):
        part = part.strip()
        if not part:
            raise TriggerValidationError(f"empty cron field part in {field_text!r}")
        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise TriggerValidationError(f"bad cron step {step!r}")
            part = base or "*"
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise TriggerValidationError(
                f"cron value out of range {part!r} (expected {lo}-{hi})"
            )
        for v in range(start, end + 1, step):
            values.add(v)
    return values


def _parse_cron(expr: str) -> list[set[int]]:
    """Parse a 5-field cron expression into matcher sets."""
    parts = expr.split()
    if len(parts) != 5:
        raise TriggerValidationError(
            f"cron expression must have 5 fields, got {len(parts)}: {expr!r}"
        )
    out: list[set[int]] = []
    for i, p in enumerate(parts):
        lo, hi = _CRON_RANGES[i]
        out.append(_parse_field(p, lo, hi))
    return out


def _cron_matches(expr: str, now: datetime) -> bool:
    """Return True iff the cron expression fires on ``now`` (to-the-minute)."""
    sets = _parse_cron(expr)
    # Compare against the caller-provided wall clock. CronScheduler passes UTC
    # by default; ScheduleEntry.timezone passes a localized clock.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (
        now.minute in sets[0]
        and now.hour in sets[1]
        and now.day in sets[2]
        and now.month in sets[3]
        and now.weekday() % 7 in _convert_weekday(sets[4])
    )


def _convert_weekday(values: set[int]) -> set[int]:
    """Map cron's Sun=0..Sat=6 to Python's Mon=0..Sun=6."""
    # Sun=0 -> Python 6; Mon=1 -> Python 0; ...; Sat=6 -> Python 5.
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping[v] for v in values}
