"""Trigger SDK wrapper."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..triggers.event import TriggerEvent
from ..triggers.cron import CronScheduler, _read_state, _state_path, _write_state
from ..triggers.routes import load_routes
from ..triggers.runtime import TriggerRuntime
from ..triggers.schedule import ScheduleEntry, load_schedules, save_schedules


@dataclass
class TriggerAPI:
    config: Config
    runtime: TriggerRuntime

    def emit(self, *, source: str, kind: str, payload: dict[str, Any] | None = None,
             target: str = "main", strategy_id: str | None = None,
             idempotency_key: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        event = TriggerEvent.new(
            source=source, kind=kind, payload=payload or {},
            target=target, strategy_id=strategy_id,
            idempotency_key=idempotency_key, dry_run=dry_run,
        )
        return self.runtime.emit(event).asdict()

    def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["dry_run"] = True
        return self.emit(**kwargs)

    def list_routes(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id, "match": r.match, "target": r.target,
                "strategy_id": r.strategy_id,
                "cooldown_seconds": r.cooldown_seconds,
                "max_per_minute": r.max_per_minute,
                "max_payload_bytes": r.max_payload_bytes,
                "paused": bool(r.extra.get("paused", False)),
                "enabled": bool(r.extra.get("enabled", True)),
                "description": r.extra.get("description"),
            }
            for r in load_routes(self.config.paths, include_inactive=True)
        ]

    # ---------------------------------------------- route CRUD
    def add_route(self, **payload: Any) -> dict[str, Any]:
        return self._skills_kernel().runtime.call(
            "trigger", "add_route", payload=payload,
            caller="sdk:trigger_api",
        )

    def update_route(self, **payload: Any) -> dict[str, Any]:
        return self._skills_kernel().runtime.call(
            "trigger", "update_route", payload=payload,
            caller="sdk:trigger_api",
        )

    def pause_route(self, *, id: str, paused: bool = True) -> dict[str, Any]:
        return self._skills_kernel().runtime.call(
            "trigger", "pause_route",
            payload={"id": id, "paused": paused},
            caller="sdk:trigger_api",
        )

    def remove_route(self, *, id: str) -> dict[str, Any]:
        return self._skills_kernel().runtime.call(
            "trigger", "remove_route", payload={"id": id},
            caller="sdk:trigger_api",
        )

    def apply_routes(self, *, add: list[dict[str, Any]] | None = None,
                     update: list[dict[str, Any]] | None = None,
                     remove: list[Any] | None = None) -> dict[str, Any]:
        return self._skills_kernel().runtime.call(
            "trigger", "apply_routes",
            payload={
                "add": add or [],
                "update": update or [],
                "remove": remove or [],
            },
            caller="sdk:trigger_api",
        )

    # -------------------------------------------------- schedule control plane
    def _skills_kernel(self):
        """Lazily boot / cache a :class:`SkillKernel` for schedule ops."""
        cached = getattr(self, "_kernel_cache", None)
        if cached is not None:
            return cached
        from ..skills.kernel import SkillKernel
        self._kernel_cache = SkillKernel.boot(self.config)
        return self._kernel_cache

    def add_schedule(self, *, id: str, kind: str,
                     every_seconds: int | None = None,
                     cron: str | None = None,
                     starts_at: str | None = None,
                     ends_at: str | None = None,
                     enabled: bool | None = None,
                     target: str = "main",
                     strategy_id: str | None = None,
                     payload: dict[str, Any] | None = None,
                     timezone: str | None = None,
                     session_kind: str | None = None,
                     attached_skills: list[str] | None = None,
                     delivery_targets: list[dict[str, Any]] | None = None,
                     session_ttl_seconds: int | None = None,
                     ) -> dict[str, Any]:
        """Register or update a schedule entry.

        Exactly one of ``every_seconds`` or ``cron`` must be supplied.
        ``starts_at`` / ``ends_at`` accept ISO-8601 strings. ``enabled``
        defaults to ``True`` at the scheduler level but can be set
        explicitly here.

        compatibility extensions (all optional):

        * ``session_kind`` = ``"trigger"`` (default, emits a TriggerEvent)
          or ``"agent"`` (spawns an ephemeral agent session per tick).
        * ``attached_skills`` — list of skill ids the scheduled agent
          session is allowed to call. Only valid when
          ``session_kind == "agent"``.
        * ``delivery_targets`` — where to route the session output;
          e.g. ``[{"kind": "messages", "channel": "ops"}]`` or
          ``[{"kind": "webhook", "url": "https://..."}]``.
        * ``session_ttl_seconds`` — wallclock ceiling on the spawned
          agent session.
        """
        kwargs: dict[str, Any] = {"id": id, "kind": kind,
                                  "target": target,
                                  "payload": payload or {}}
        if strategy_id is not None:
            kwargs["strategy_id"] = strategy_id
        if every_seconds is not None:
            kwargs["every_seconds"] = every_seconds
        if cron is not None:
            kwargs["cron"] = cron
        if starts_at is not None:
            kwargs["starts_at"] = starts_at
        if ends_at is not None:
            kwargs["ends_at"] = ends_at
        if enabled is not None:
            kwargs["enabled"] = enabled
        if timezone is not None:
            kwargs["timezone"] = timezone
        if session_kind is not None:
            kwargs["session_kind"] = session_kind
        if attached_skills is not None:
            kwargs["attached_skills"] = list(attached_skills)
        if delivery_targets is not None:
            kwargs["delivery_targets"] = [dict(t) for t in delivery_targets]
        if session_ttl_seconds is not None:
            kwargs["session_ttl_seconds"] = session_ttl_seconds
        entries = [e for e in load_schedules(self.config.paths) if e.id != id]
        entries.append(ScheduleEntry(**kwargs))
        save_schedules(self.config.paths, entries)
        return {"ok": True, "schedule": self._entry_to_dict(entries[-1])}

    def add_schedule_from_text(self, *, text: str,
                               defaults: dict[str, Any] | None = None,
                               ) -> dict[str, Any]:
        """Natural-language schedule creation.

        Delegates to the ``trigger.add_schedule_from_text`` skill action,
        which asks the *light* LLM tier to translate ``text`` into a
        strict schedule payload and then calls ``add_schedule``.
        """
        payload: dict[str, Any] = {"text": text}
        if defaults is not None:
            # Don't coerce here — the skill action owns shape validation so
            # bad input surfaces as SkillActionError, not ValueError.
            payload["defaults"] = defaults
        return self._skills_kernel().runtime.call(
            "trigger", "add_schedule_from_text", payload=payload,
            caller="sdk:trigger_api",
        )

    def list_schedules(self) -> list[dict[str, Any]]:
        return [self._entry_to_dict(e) for e in load_schedules(self.config.paths)]

    def remove_schedule(self, *, id: str) -> dict[str, Any]:
        before = load_schedules(self.config.paths)
        after = [e for e in before if e.id != id]
        save_schedules(self.config.paths, after)
        return {"ok": True, "id": id, "removed": len(before) - len(after)}

    def enable_schedule(self, *, id: str, enabled: bool = True) -> dict[str, Any]:
        return self.update_schedule(id=id, enabled=enabled)

    def pause_schedule(self, *, id: str) -> dict[str, Any]:
        """Alias: pause is just ``enable_schedule(enabled=False)``."""
        return self.enable_schedule(id=id, enabled=False)

    def resume_schedule(self, *, id: str) -> dict[str, Any]:
        """Alias: resume is just ``enable_schedule(enabled=True)``."""
        return self.enable_schedule(id=id, enabled=True)

    def update_schedule(self, *, id: str, **fields: Any) -> dict[str, Any]:
        """Patch individual fields of an existing schedule in place."""
        payload: dict[str, Any] = {"id": id}
        for key in (
            "kind", "every_seconds", "cron", "starts_at", "ends_at",
            "enabled", "target", "strategy_id", "payload",
            "timezone",
            # compatibility extension:
            "session_kind", "attached_skills", "delivery_targets",
            "session_ttl_seconds",
        ):
            if key in fields:
                payload[key] = fields[key]
        entries = load_schedules(self.config.paths)
        updated: list[ScheduleEntry] = []
        updated_entry: ScheduleEntry | None = None
        found = False
        for entry in entries:
            if entry.id != id:
                updated.append(entry)
                continue
            found = True
            row = self._entry_to_dict(entry)
            row.update({k: v for k, v in payload.items() if k != "id"})
            updated_entry = ScheduleEntry(**row)
            updated.append(updated_entry)
        if not found:
            return {"ok": False, "error": "schedule_not_found", "id": id}
        save_schedules(self.config.paths, updated)
        return {"ok": True, "id": id, "schedule": self._entry_to_dict(updated_entry)}

    def run_schedule_now(self, *, id: str,
                         reason: str = "operator_manual_run"
                         ) -> dict[str, Any]:
        """Fire the schedule immediately; updates last-tick state."""
        entry = next((e for e in load_schedules(self.config.paths) if e.id == id), None)
        if entry is None:
            return {"ok": False, "error": "schedule_not_found", "id": id}
        now_ts = time.time()
        if entry.session_kind == "agent":
            from ..sdk.scheduled_session_factory import default_kernel_factory
            try:
                from ..messaging.scheduled_delivery import deliver_scheduled_session
            except Exception:
                deliver_scheduled_session = None
            from ..triggers.scheduled_session import ScheduledSessionRunner
            result = ScheduledSessionRunner(
                config=self.config,
                kernel_factory=default_kernel_factory,
                delivery_fn=deliver_scheduled_session,
            ).run_once(entry, now_ts=now_ts)
            self._mark_schedule_fired(id, now_ts)
            return {"ok": result.ok, "schedule_id": id, "session": result.asdict()}
        payload = dict(entry.payload or {})
        payload.setdefault("schedule_id", entry.id)
        payload.setdefault("manual_reason", reason)
        event = TriggerEvent.new(
            source="schedule",
            kind=entry.kind,
            payload=payload,
            target=entry.target,
            strategy_id=entry.strategy_id,
            idempotency_key=f"manual:{entry.id}:{int(now_ts * 1000)}",
        )
        result = self.runtime.emit(event)
        self._mark_schedule_fired(id, now_ts)
        return {
            "ok": True,
            "schedule_id": id,
            "event_id": event.event_id,
            "result": result.asdict(),
        }

    def schedule_status(self, *, id: str | None = None) -> dict[str, Any]:
        """Return computed status for one or all schedules."""
        state = _read_state(_state_path(self.config))
        entries = load_schedules(self.config.paths)
        rows = []
        for entry in entries:
            if id is not None and entry.id != id:
                continue
            row = self._entry_to_dict(entry)
            row["last_fired_ts"] = state.get(entry.id)
            rows.append(row)
        return {"ok": True, "schedules": rows}

    def tick_schedules(self) -> dict[str, Any]:
        """Force one scheduler pass and return the list of fired entries."""
        fired = CronScheduler(config=self.config, runtime=self.runtime).tick()
        return {"ok": True, "fired": fired}

    def _mark_schedule_fired(self, id: str, ts: float) -> None:
        path = _state_path(self.config)
        state = _read_state(path)
        state[id] = float(ts)
        _write_state(path, state)

    @staticmethod
    def _entry_to_dict(entry: ScheduleEntry) -> dict[str, Any]:
        out = {
            "id": entry.id,
            "kind": entry.kind,
            "every_seconds": entry.every_seconds,
            "cron": entry.cron,
            "starts_at": entry.starts_at,
            "ends_at": entry.ends_at,
            "enabled": entry.enabled,
            "target": entry.target,
            "strategy_id": entry.strategy_id,
            "payload": dict(entry.payload or {}),
            "timezone": entry.timezone,
            "session_kind": entry.session_kind,
            "attached_skills": list(entry.attached_skills or []),
            "delivery_targets": [dict(t) for t in (entry.delivery_targets or [])],
            "session_ttl_seconds": entry.session_ttl_seconds,
        }
        return {k: v for k, v in out.items() if v is not None}

    # -------------------------------------------------------- explain
    def explain(self, *, source: str, kind: str,
                payload: dict[str, Any] | None = None,
                target: str = "main", strategy_id: str | None = None,
                ) -> dict[str, Any]:
        """Static explain: which route would the router pick, and why?

        This never fires the event nor touches dedupe/cooldown stores.
        """
        event = TriggerEvent.new(
            source=source, kind=kind, payload=payload or {},
            target=target, strategy_id=strategy_id,
        )
        return self.runtime.explain(event)

    def replay(self, *, event_id: str,
               reason: str = "operator_replay") -> dict[str, Any] | None:
        """Replay a historical event by id (scans the trigger journal).

        Returns the new :class:`RouterResult` as a dict, or ``None`` if
        the event could not be located.
        """
        record = self._find_event(event_id)
        if record is None:
            return None
        ev = TriggerEvent(
            event_id=record.get("event_id") or event_id,
            source=str(record.get("source", "script")),
            kind=str(record.get("event_kind") or record.get("kind") or "replay"),
            payload=record.get("payload") or {},
            target=str(record.get("target") or "main"),
            strategy_id=record.get("strategy_id"),
            idempotency_key=record.get("idempotency_key"),
            dry_run=False,
        )
        return self.runtime.replay(ev, reason=reason).asdict()

    def _find_event(self, event_id: str) -> dict[str, Any] | None:
        """Locate a historical event in the trigger journal by id."""
        journal = self.config.paths.journal("triggers")
        if not journal.exists():
            return None
        import json as _json
        for line in journal.read_text(encoding="utf-8").splitlines()[::-1]:
            try:
                row = _json.loads(line)
            except Exception:
                continue
            if row.get("event_id") != event_id:
                continue
            # ``trigger.routed`` entries carry the richest shape.
            if row.get("kind") in {"trigger.routed", "trigger.replay",
                                    "trigger.dedup", "trigger.rate_limited",
                                    "trigger.cooldown"}:
                return row
        return None

    def wait_for_result(self, event_id: str, *, timeout_s: float = 5.0,
                        poll_s: float = 0.1) -> dict[str, Any] | None:
        """Look up the routing outcome for a previously emitted event by
        scanning `journals/triggers.jsonl` tail. Returns None on timeout.

        The synchronous skill-target path already returns its outcome from
        `emit()`. This helper is for async targets where the agent loop picks
        the event up later.
        """
        journal = self.config.paths.journal("triggers")
        deadline = time.time() + timeout_s
        last_size = 0
        while time.time() < deadline:
            try:
                raw = journal.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = ""
            if len(raw) != last_size:
                last_size = len(raw)
                import json as _json
                for line in raw.splitlines()[::-1][:200]:
                    try:
                        row = _json.loads(line)
                    except Exception:
                        continue
                    if row.get("event_id") == event_id:
                        return row
            time.sleep(poll_s)
        return None
