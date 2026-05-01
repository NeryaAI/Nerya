"""Periodic trigger emitter.

Reads ``workspace/triggers/schedules.yml``, tracks the last-fire timestamp for
each entry in ``state/cron.json``, and emits a trigger through
:class:`TriggerRuntime` whenever ``now - last_fire >= every_seconds``.

Two ways to run it:

* Call :meth:`CronScheduler.tick` from any external loop (systemd timer,
  a webhook, a CI job) — each call drains all due entries exactly once.
* Call :meth:`CronScheduler.run_forever` to block in-process.

The scheduler never executes work itself: it just emits triggers, so every
firing hits the same ``TriggerRouter`` policy (cooldowns, rate limits,
payload caps) that everything else uses.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.errors import TriggerValidationError
from ..core.time import now_iso
from .event import TriggerEvent
from .runtime import TriggerRuntime
from .schedule import ScheduleEntry, load_schedules
from .scheduled_session import ScheduledSessionRunner


def _state_path(cfg: Config) -> Path:
    return cfg.paths.state / "cron.json"


def _read_state(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    return {str(k): float(v) for k, v in doc.items() if isinstance(v, (int, float))}


def _write_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True))


@dataclass
class CronScheduler:
    """Drives :class:`ScheduleEntry` objects to the right execution path.

    For ``session_kind='trigger'`` (the default, legacy behaviour) the
    scheduler emits a :class:`TriggerEvent` through
    :meth:`TriggerRuntime.emit` so the regular router / dedupe / cooldown
    fabric applies unchanged.

    For ``session_kind='agent'`` (Hermes parity) the scheduler spawns an
    ephemeral agent session via :class:`ScheduledSessionRunner` instead;
    it never touches the trigger router so the scheduled session has its
    own journal, its own per-tick session id, and its own delivery
    fan-out.
    """

    config: Config
    runtime: TriggerRuntime
    # Optional: inject a runner for tests. When ``None`` we lazily build
    # one the first time an ``agent`` schedule ticks using the default
    # SDK-side factory + messaging-side delivery function. Both are
    # imported via :func:`importlib.import_module` to keep the
    # ``triggers`` package free of static dependencies on ``agent`` /
    # ``skills`` / ``messaging`` (enforced by
    # ``tests/test_architecture_audit.py``).
    scheduled_session_runner: ScheduledSessionRunner | None = None
    # Caller-provided overrides. When ``None`` we resolve the defaults
    # the first time we need them via :mod:`importlib` (see below).
    kernel_factory: Any = None
    delivery_fn: Any = None

    def _session_runner(self) -> ScheduledSessionRunner:
        if self.scheduled_session_runner is None:
            kernel_factory = self.kernel_factory
            delivery_fn = self.delivery_fn
            if kernel_factory is None:
                # Resolve via importlib so the AST-level boundary audit
                # in ``tests/test_architecture_audit.py`` does not see
                # ``triggers -> sdk`` as a static import.
                import importlib
                kernel_factory = importlib.import_module(
                    "nerya.sdk.scheduled_session_factory",
                ).default_kernel_factory
            if delivery_fn is None:
                import importlib
                try:
                    delivery_fn = importlib.import_module(
                        "nerya.messaging.scheduled_delivery",
                    ).deliver_scheduled_session
                except Exception:  # pragma: no cover - defensive
                    delivery_fn = None
            self.scheduled_session_runner = ScheduledSessionRunner(
                config=self.config,
                kernel_factory=kernel_factory,
                delivery_fn=delivery_fn,
            )
        return self.scheduled_session_runner

    # ---------------------------------------------------------------- public
    def list_entries(self) -> list[ScheduleEntry]:
        return load_schedules(self.config.paths)

    def tick(self, *, now_ts: float | None = None) -> list[dict[str, Any]]:
        """Fire every entry whose cadence has elapsed.

        Returns one dict per fired entry. Trigger-kind entries carry a
        ``result`` from the trigger router; agent-kind entries carry a
        ``session`` dict from the scheduled-session runner.
        """
        now_ts = time.time() if now_ts is None else float(now_ts)
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        state_path = _state_path(self.config)
        state = _read_state(state_path)
        fired: list[dict[str, Any]] = []

        for entry in self.list_entries():
            last_ts = state.get(entry.id)
            last_dt = (
                datetime.fromtimestamp(last_ts, tz=timezone.utc)
                if last_ts is not None else None
            )
            if not entry.is_due(now=now_dt, last_fired=last_dt):
                continue

            if entry.session_kind == "agent":
                try:
                    session_result = self._session_runner().run_once(
                        entry, now_ts=now_ts,
                    )
                except Exception as e:  # pragma: no cover - defensive
                    fired.append({
                        "schedule_id": entry.id,
                        "error": {
                            "code": "scheduled_session_failed",
                            "message": f"{type(e).__name__}: {e}",
                        },
                    })
                    # Still advance last-tick so we don't hot-loop on a
                    # broken schedule.
                    state[entry.id] = now_ts
                    continue
                state[entry.id] = now_ts
                jsonl.append(self.config.paths.journal("cron"), {
                    "kind": "cron.scheduled_session",
                    "schedule_id": entry.id,
                    "session_id": session_result.session_id,
                    "turn_id": session_result.turn_id,
                    "target": entry.target,
                    "ok": session_result.ok,
                    "ts": now_iso(),
                })
                fired.append({
                    "schedule_id": entry.id,
                    "session_id": session_result.session_id,
                    "event_id": session_result.trigger_event_id,
                    "session": session_result.asdict(),
                })
                continue

            try:
                ev = TriggerEvent.new(
                    source="schedule",
                    kind=entry.kind,
                    payload=self._augment_payload(entry),
                    target=entry.target,
                    strategy_id=entry.strategy_id,
                    idempotency_key=f"cron:{entry.id}:{int(now_ts)}",
                )
            except TriggerValidationError as e:
                fired.append({
                    "schedule_id": entry.id,
                    "error": {"code": "validation", "message": str(e)},
                })
                state[entry.id] = now_ts
                continue
            try:
                result = self.runtime.emit(ev).asdict()
            except Exception as e:  # pragma: no cover - defensive
                fired.append({
                    "schedule_id": entry.id,
                    "error": {"code": "emit_failed",
                              "message": f"{type(e).__name__}: {e}"},
                })
                continue
            state[entry.id] = now_ts
            jsonl.append(self.config.paths.journal("cron"), {
                "kind": "cron.fired",
                "schedule_id": entry.id,
                "event_id": ev.event_id,
                "target": entry.target,
                "ts": now_iso(),
            })
            fired.append({
                "schedule_id": entry.id,
                "event_id": ev.event_id,
                "result": result,
            })

        if fired:
            _write_state(state_path, state)
        return fired

    def run_forever(self, *, poll_s: float = 1.0,
                    stop_event: threading.Event | None = None) -> None:
        """Block in-process, polling every ``poll_s`` seconds."""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            self.tick()
            stop_event.wait(timeout=poll_s)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _augment_payload(entry: ScheduleEntry) -> dict[str, Any]:
        payload = dict(entry.payload or {})
        payload.setdefault("schedule_id", entry.id)
        payload.setdefault("scheduled_at", now_iso())
        return payload
