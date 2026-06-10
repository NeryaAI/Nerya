"""Scheduled agent session runner (compatibility, ).

When a :class:`ScheduleEntry` declares ``session_kind == "agent"`` it is
not just a trigger emitter — it asks the cron loop to spawn a fresh,
single-turn agent session each time the cadence fires, with a pinned
skill whitelist (``attached_skills``) and an optional delivery fan-out
(``delivery_targets``).

This module owns that branch. It never fires the trigger router; it
goes straight to :class:`AgentKernel` so the scheduled session behaves
exactly like a user-initiated ``/agent/run_turn`` call with the extra
guardrails the schedule encodes.

Flow per tick
-------------
1. Build a synthetic trigger dict keyed by ``source='scheduled_session'``
   so the planner / router / journaling all tag it consistently.
2. Mint a fresh ``session_id`` of the form ``sched:<schedule_id>:<ts>``
   so each cadence firing is a clean context window (no accidental
   carry-over from the last run unless the caller also wires a
   persistent session manually).
3. Call :meth:`AgentKernel.run_turn` with ``attached_skills=`` taken
   verbatim from the schedule entry. The kernel enforces the
   strategy-level / global skill deny-list on top.
4. If ``session_ttl_seconds`` is set we wrap the call in a best-effort
   soft-deadline thread-wait; an exceeded deadline is logged but we
   don't hard-kill the turn because there's no safe interruption point
   in the kernel. The deadline is advisory.
5. Journal the full outcome under
   ``journals/scheduled_session.jsonl`` (one row per tick, success or
   failure) so operators can replay exactly which turn each cadence
   firing produced.
6. Hand the :class:`AgentTurnResult` to the injected ``delivery_fn``
   (default: :mod:`nerya.messaging.scheduled_delivery`) for fan-out to
   ``delivery_targets``. Delivery failures are logged but never swallow
   the agent turn result.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from .schedule import ScheduleEntry


_SESSION_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_session_fragment(value: str) -> str:
    text = _SESSION_ID_SAFE_RE.sub("_", str(value or "").strip())
    text = text.strip("._-")
    return text or "schedule"


@dataclass
class ScheduledSessionResult:
    """What :class:`ScheduledSessionRunner.run_once` reports per tick."""

    schedule_id: str
    session_id: str
    ok: bool
    turn_id: str | None = None
    trigger_event_id: str | None = None
    decision: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str | None = None
    error: dict[str, Any] | None = None
    delivery: list[dict[str, Any]] = field(default_factory=list)
    ttl_exceeded: bool = False
    wall_ms: int = 0

    def asdict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "session_id": self.session_id,
            "ok": self.ok,
            "turn_id": self.turn_id,
            "trigger_event_id": self.trigger_event_id,
            "decision": self.decision,
            "actions": list(self.actions),
            "stopped_reason": self.stopped_reason,
            "error": self.error,
            "delivery": list(self.delivery),
            "ttl_exceeded": self.ttl_exceeded,
            "wall_ms": self.wall_ms,
        }


@dataclass
class ScheduledSessionRunner:
    """Drive ``session_kind='agent'`` entries of :class:`ScheduleEntry`.

    The runner is intentionally stateless — one instance can service
    any number of due entries per cron tick.
    """

    config: Config
    # Callable that returns a booted :class:`AgentKernel`. We keep it as
    # a factory rather than an instance so tests can inject a stub.
    kernel_factory: Any
    # Callable that accepts ``(config, entry, result)`` and fans the
    # agent result out to the schedule's delivery_targets. Left open so
    # :mod:`nerya.messaging.scheduled_delivery` can be wired at boot
    # without breaking the runtime-ownership ADR (triggers must not
    # import messaging directly).
    delivery_fn: Any = None

    # ------------------------------------------------------------- run
    def run_many(
        self,
        entry: ScheduleEntry,
        *,
        now_ts: float | None = None,
    ) -> list[ScheduledSessionResult]:
        """Execute one turn in every session selected by ``entry``.

        ``ephemeral`` preserves the original behaviour: every firing gets a
        fresh session. ``reuse`` keeps all firings in one stable session, and
        ``fanout`` runs the same prompt once per configured ``session_ids``
        entry.
        """

        now_ts = time.time() if now_ts is None else float(now_ts)
        session_ids = self._session_ids_for(entry, now_ts)
        return [
            self.run_once(entry, now_ts=now_ts, session_id=session_id)
            for session_id in session_ids
        ]

    def run_once(self, entry: ScheduleEntry,
                 *, now_ts: float | None = None,
                 session_id: str | None = None) -> ScheduledSessionResult:
        """Execute one scheduled agent session for ``entry``.

        Must only be called when ``entry.session_kind == 'agent'`` —
        the caller (cron tick) is responsible for that branch.
        """
        now_ts = time.time() if now_ts is None else float(now_ts)
        session_id = session_id or self._session_ids_for(entry, now_ts)[0]
        trigger_event_id = f"sched_evt_{_safe_session_fragment(entry.id)}_{int(now_ts)}"
        t0 = time.monotonic()
        trigger = {
            "id": trigger_event_id,
            "event_id": trigger_event_id,
            "source": "scheduled_session",
            "kind": entry.kind,
            "target": entry.target,
            "strategy_id": entry.strategy_id,
            "payload": self._build_payload(entry, now_ts, session_id),
        }

        result = ScheduledSessionResult(
            schedule_id=entry.id,
            session_id=session_id,
            ok=False,
            trigger_event_id=trigger_event_id,
        )

        try:
            kernel = self.kernel_factory(self.config)
        except Exception as exc:
            result.error = {
                "code": "kernel_boot_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            self._journal(entry, result, now_ts)
            return result

        turn_result: Any = None
        turn_error: BaseException | None = None

        def _invoke() -> None:
            nonlocal turn_result, turn_error
            try:
                turn_result = kernel.run_turn(
                    trigger=trigger,
                    strategy_id=entry.strategy_id,
                    session_id=session_id,
                    attached_skills=list(entry.attached_skills or []) or None,
                )
            except BaseException as exc:  # noqa: BLE001
                turn_error = exc

        if entry.session_ttl_seconds and entry.session_ttl_seconds > 0:
            # Soft deadline. We cannot interrupt a running LLM call
            # mid-flight safely, so this is advisory: if the turn
            # overshoots we record ttl_exceeded=True and still return
            # whatever came back when the thread finally joined.
            th = threading.Thread(target=_invoke, name=f"sched-{entry.id}",
                                  daemon=True)
            th.start()
            th.join(timeout=float(entry.session_ttl_seconds))
            if th.is_alive():
                result.ttl_exceeded = True
                # Give the turn a generous additional grace window to
                # finish cleanly before we give up on it. This is still
                # bounded so a wedged turn can't hold the cron loop.
                th.join(timeout=min(30.0, float(entry.session_ttl_seconds)))
                if th.is_alive():
                    result.error = {
                        "code": "session_ttl_exceeded",
                        "message": (
                            f"scheduled session exceeded "
                            f"{entry.session_ttl_seconds}s TTL and did not "
                            f"return within the grace window; result dropped"
                        ),
                    }
                    result.wall_ms = int((time.monotonic() - t0) * 1000)
                    self._journal(entry, result, now_ts)
                    return result
        else:
            _invoke()

        result.wall_ms = int((time.monotonic() - t0) * 1000)

        if turn_error is not None:
            result.error = {
                "code": "run_turn_failed",
                "message": f"{type(turn_error).__name__}: {turn_error}",
                "trace": traceback.format_exception(
                    type(turn_error), turn_error,
                    turn_error.__traceback__,
                )[-6:],
            }
            self._journal(entry, result, now_ts)
            return result

        if turn_result is None:
            result.error = {
                "code": "run_turn_empty",
                "message": "kernel.run_turn returned None",
            }
            self._journal(entry, result, now_ts)
            return result

        result.ok = True
        result.turn_id = getattr(turn_result, "turn_id", None)
        result.decision = getattr(turn_result, "decision", None)
        result.actions = list(getattr(turn_result, "actions", []) or [])
        result.stopped_reason = getattr(turn_result, "stopped_reason", None)

        # Delivery fan-out. Failures are logged per-target but never
        # turn an otherwise-successful turn into a failure.
        if entry.delivery_targets and self.delivery_fn is not None:
            try:
                delivery_report = self.delivery_fn(
                    self.config, entry, turn_result,
                ) or []
                result.delivery = list(delivery_report)
            except Exception as exc:  # pragma: no cover - defensive
                result.delivery = [{
                    "ok": False,
                    "kind": "delivery_dispatch_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }]

        self._journal(entry, result, now_ts)
        return result

    # -------------------------------------------------------- helpers
    @staticmethod
    def _build_payload(entry: ScheduleEntry, now_ts: float,
                       session_id: str) -> dict[str, Any]:
        """Build the per-tick payload the kernel sees.

        The schedule's own payload wins on key collisions (operators can
        override anything) but we always stamp the schedule identity so
        downstream skills can reconstruct provenance without parsing the
        trigger id.
        """
        payload: dict[str, Any] = dict(entry.payload or {})
        payload.setdefault("schedule_id", entry.id)
        payload.setdefault("scheduled_at", now_iso())
        payload.setdefault("scheduled_ts", float(now_ts))
        payload.setdefault("session_id", session_id)
        payload.setdefault("session_kind", "agent")
        if entry.attached_skills:
            payload.setdefault("attached_skills", list(entry.attached_skills))
        return payload

    @staticmethod
    def _session_ids_for(entry: ScheduleEntry, now_ts: float) -> list[str]:
        if entry.session_mode == "fanout":
            return list(entry.session_ids or [])
        if entry.session_mode == "reuse":
            return [
                entry.session_id
                or f"sched_{_safe_session_fragment(entry.id)}"
            ]
        return [f"sched_{_safe_session_fragment(entry.id)}_{int(now_ts)}"]

    def _journal(self, entry: ScheduleEntry,
                 result: ScheduledSessionResult, now_ts: float) -> None:
        row = {
            "kind": "scheduled_session.tick",
            "ts": now_iso(),
            "ts_epoch": float(now_ts),
            "schedule_id": entry.id,
            "session_id": result.session_id,
            "session_kind": entry.session_kind,
            "target": entry.target,
            "strategy_id": entry.strategy_id,
            "attached_skills": list(entry.attached_skills or []),
            "delivery_targets": [
                {"kind": t.get("kind")} for t in (entry.delivery_targets or [])
            ],
            "ok": result.ok,
            "turn_id": result.turn_id,
            "trigger_event_id": result.trigger_event_id,
            "ttl_exceeded": result.ttl_exceeded,
            "wall_ms": result.wall_ms,
            "stopped_reason": result.stopped_reason,
            "error": result.error,
            "delivery": result.delivery,
        }
        try:
            jsonl.append(
                self.config.paths.journal("scheduled_session"), row,
            )
        except Exception:  # pragma: no cover - best-effort journaling
            pass


__all__ = [
    "ScheduledSessionRunner",
    "ScheduledSessionResult",
]
