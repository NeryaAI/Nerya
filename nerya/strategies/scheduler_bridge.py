"""Compile strategy schedules into the existing trigger-schedule schema.

The runtime intentionally does **not** ship a second scheduler. Every
strategy that wants to trade on a cron is rendered down to a row in
``workspace/triggers/schedules.yml`` so the operator's existing
trigger control plane (``/triggers/schedules/*`` API + dashboard) can
inspect, pause, replay, and dry-run strategy ticks the same way it
handles every other schedule.

Two schedules per strategy
--------------------------
The bridge installs *up to* two rows per strategy:

* ``strategy_<id>_tick``       — fires the trading tick. ``strategy_id``
  is set so existing journal queries by strategy keep working.
* ``strategy_<id>_tuning``     — fires the self-evolution tuning loop. Only installed when ``manifest.tuning.enabled`` is true.

The two schedules are independent: pausing the trading tick does not
pause tuning, and vice versa. That separation matters because we
often want to keep tuning research running on a strategy that's
been paused for live trading.

Tick payloads
-------------
Each row's ``payload`` carries the canonical fields the runner needs
to dispatch a tick:

```yaml
payload:
  strategy_id: btc_scalper
  reason: cron
  mode: paper        # may be overridden at run-time
  trading: true      # vs tuning=false
```

Most trading ticks use ``target=skill:strategy.run_tick``. Strategies
that declare an Agent task runtime, or older generated ``*team*``
strategies with multiple research roles, use
``target=skill:strategy.agent_task`` so the scheduler enters
AgentKernel and can call native tools such as ``team_run`` before any
trade intent is submitted. Tuning still uses
``target=skill:strategy.run_tuning``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..core.errors import TradingError
from ..core.paths import WorkspacePaths
from ..triggers.schedule import ScheduleEntry, load_schedules, save_schedules
from .agent_task_mode import AGENT_TASK_TARGET, agent_task_requested
from .package import StrategyManifest, StrategyPackage, StrategySchedule, load_package


# ---------------------------------------------------------------------------
# Schedule id helpers
# ---------------------------------------------------------------------------


TRADING_KIND = "strategy.tick"
TRADING_TARGET = "skill:strategy.run_tick"
TUNING_KIND = "strategy.tuning"
TUNING_TARGET = "skill:strategy.run_tuning"


def trading_schedule_id(strategy_id: str) -> str:
    return f"strategy_{strategy_id}_tick"


def tuning_schedule_id(strategy_id: str) -> str:
    return f"strategy_{strategy_id}_tuning"


def is_strategy_schedule(entry: ScheduleEntry) -> bool:
    """Return True iff ``entry`` was installed by this bridge."""

    return entry.kind in {TRADING_KIND, TUNING_KIND}


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------


def _schedule_kwargs(
    schedule: StrategySchedule,
) -> dict[str, Any]:
    if schedule.type == "cron":
        if not schedule.cron:
            raise TradingError("cron schedule missing cron expression")
        return {"cron": schedule.cron}
    return {"every_seconds": int(schedule.every_seconds or 60)}


def _common_kwargs(schedule: StrategySchedule) -> dict[str, Any]:
    out: dict[str, Any] = {"enabled": bool(schedule.enabled)}
    if schedule.starts_at:
        out["starts_at"] = schedule.starts_at
    if schedule.ends_at:
        out["ends_at"] = schedule.ends_at
    return out


def compile_trading_schedule(package: StrategyPackage) -> ScheduleEntry:
    """Render the trading-tick schedule row for ``package``."""

    manifest = package.manifest
    sid = manifest.strategy_id
    use_agent_task = agent_task_requested(manifest)
    payload: dict[str, Any] = {
        "strategy_id": sid,
        "reason": "cron",
        "mode": manifest.mode,
        "trading": True,
        "tuning": False,
    }
    if use_agent_task:
        payload["agent_task"] = True
    return ScheduleEntry(
        id=trading_schedule_id(sid),
        kind=TRADING_KIND,
        target=AGENT_TASK_TARGET if use_agent_task else TRADING_TARGET,
        strategy_id=sid,
        payload=payload,
        **_schedule_kwargs(manifest.schedule),
        **_common_kwargs(manifest.schedule),
    )


def compile_tuning_schedule(package: StrategyPackage) -> Optional[ScheduleEntry]:
    """Render the tuning schedule row for ``package`` (when enabled)."""

    manifest = package.manifest
    if not manifest.tuning.enabled or manifest.tuning.schedule is None:
        return None
    sid = manifest.strategy_id
    payload: dict[str, Any] = {
        "strategy_id": sid,
        "reason": "tuning_cron",
        "mode": manifest.mode,
        "trading": False,
        "tuning": True,
    }
    return ScheduleEntry(
        id=tuning_schedule_id(sid),
        kind=TUNING_KIND,
        target=TUNING_TARGET,
        strategy_id=sid,
        payload=payload,
        **_schedule_kwargs(manifest.tuning.schedule),
        **_common_kwargs(manifest.tuning.schedule),
    )


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


@dataclass
class StrategyScheduleApplyResult:
    """Outcome of :func:`apply_strategy_schedules` for one package."""

    strategy_id: str
    trading_id: str
    tuning_id: Optional[str] = None
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def apply_strategy_schedules(
    paths: WorkspacePaths,
    package: StrategyPackage,
) -> StrategyScheduleApplyResult:
    """Install / update / prune schedule rows for ``package``.

    Reads ``schedules.yml``, replaces any existing rows for this
    strategy, and writes the file back. Idempotent: re-running
    against an unchanged package leaves the file content the same.
    """

    sid = package.strategy_id
    trading_entry = compile_trading_schedule(package)
    tuning_entry = compile_tuning_schedule(package)

    existing = list(load_schedules(paths))
    out: list[ScheduleEntry] = []
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []

    seen_trading = False
    seen_tuning = False

    expected_ids = {trading_entry.id}
    if tuning_entry is not None:
        expected_ids.add(tuning_entry.id)

    for entry in existing:
        if entry.strategy_id == sid and is_strategy_schedule(entry):
            if entry.id == trading_entry.id:
                if not _entries_equal(entry, trading_entry):
                    updated.append(entry.id)
                seen_trading = True
                out.append(trading_entry)
            elif tuning_entry is not None and entry.id == tuning_entry.id:
                if not _entries_equal(entry, tuning_entry):
                    updated.append(entry.id)
                seen_tuning = True
                out.append(tuning_entry)
            elif entry.id not in expected_ids:
                # Old strategy schedule that no longer matches the manifest
                # — drop it so the active state matches the package.
                removed.append(entry.id)
                continue
            else:  # pragma: no cover — defensive branch
                out.append(entry)
        else:
            out.append(entry)

    if not seen_trading:
        out.append(trading_entry)
        added.append(trading_entry.id)
    if tuning_entry is not None and not seen_tuning:
        out.append(tuning_entry)
        added.append(tuning_entry.id)

    save_schedules(paths, out)
    return StrategyScheduleApplyResult(
        strategy_id=sid,
        trading_id=trading_entry.id,
        tuning_id=tuning_entry.id if tuning_entry else None,
        added=added,
        updated=updated,
        removed=removed,
    )


def remove_strategy_schedules(
    paths: WorkspacePaths,
    strategy_id: str,
) -> list[str]:
    """Drop every bridge-managed schedule for ``strategy_id``."""

    existing = list(load_schedules(paths))
    out: list[ScheduleEntry] = []
    removed: list[str] = []
    for entry in existing:
        if entry.strategy_id == strategy_id and is_strategy_schedule(entry):
            removed.append(entry.id)
            continue
        out.append(entry)
    if removed:
        save_schedules(paths, out)
    return removed


def reconcile_all(paths: WorkspacePaths) -> list[StrategyScheduleApplyResult]:
    """Walk every strategy package and re-install its schedule rows.

    Useful after a bulk import or a manifest schema migration; the
    operator-facing endpoint binds this to ``POST /strategies/schedule/sync``.
    """

    out: list[StrategyScheduleApplyResult] = []
    if not paths.strategies.exists():
        return out
    for child in sorted(p for p in paths.strategies.iterdir() if p.is_dir()):
        if not (child / "strategy.yml").exists():
            continue
        try:
            package = load_package(paths, child.name)
        except TradingError:
            continue
        out.append(apply_strategy_schedules(paths, package))
    return out


# ---------------------------------------------------------------------------
# Pause / resume convenience helpers
# ---------------------------------------------------------------------------


def set_strategy_schedule_enabled(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    trading: Optional[bool] = None,
    tuning: Optional[bool] = None,
) -> dict[str, Optional[bool]]:
    """Toggle the ``enabled`` flag on the trading / tuning schedule.

    Returns a dict with the post-change state for each row (None when
    the row doesn't exist).
    """

    existing = list(load_schedules(paths))
    state: dict[str, Optional[bool]] = {"trading": None, "tuning": None}
    rebuilt: list[ScheduleEntry] = []
    changed = False
    trading_id = trading_schedule_id(strategy_id)
    tuning_id = tuning_schedule_id(strategy_id)
    for entry in existing:
        if entry.id == trading_id:
            state["trading"] = bool(entry.enabled)
            if trading is not None and bool(entry.enabled) != bool(trading):
                entry.enabled = bool(trading)
                state["trading"] = bool(trading)
                changed = True
        elif entry.id == tuning_id:
            state["tuning"] = bool(entry.enabled)
            if tuning is not None and bool(entry.enabled) != bool(tuning):
                entry.enabled = bool(tuning)
                state["tuning"] = bool(tuning)
                changed = True
        rebuilt.append(entry)
    if changed:
        save_schedules(paths, rebuilt)
    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entries_equal(a: ScheduleEntry, b: ScheduleEntry) -> bool:
    """Compare two schedule entries on the fields the bridge manages."""

    fields = (
        "id", "kind", "target", "strategy_id", "every_seconds", "cron",
        "starts_at", "ends_at", "enabled",
    )
    for f in fields:
        if getattr(a, f) != getattr(b, f):
            return False
    return dict(a.payload or {}) == dict(b.payload or {})


__all__ = [
    "StrategyScheduleApplyResult",
    "AGENT_TASK_TARGET",
    "TRADING_KIND",
    "TRADING_TARGET",
    "TUNING_KIND",
    "TUNING_TARGET",
    "apply_strategy_schedules",
    "compile_trading_schedule",
    "compile_tuning_schedule",
    "is_strategy_schedule",
    "reconcile_all",
    "remove_strategy_schedules",
    "set_strategy_schedule_enabled",
    "trading_schedule_id",
    "tuning_schedule_id",
]
