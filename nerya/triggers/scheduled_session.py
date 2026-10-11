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
2. Select the configured ephemeral/reuse/fanout session and mint a
   fixed turn_id before invoking the kernel.
3. Call :meth:`AgentKernel.run_turn` with ``attached_skills=`` taken
   verbatim from the schedule entry. The kernel enforces the
   strategy-level / global skill deny-list on top.
4. At TTL request cooperative cancellation; after grace report an
   unconfirmed running turn. The worker still owns terminal writeback.
5. Journal lifecycle receipts under ``journals/scheduled_session.jsonl``
   keyed by the fixed turn_id. The latest row is the observed state,
   including late results; no deadline implies a confirmed stop.
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
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.ids import turn_id as new_turn_id
from ..core.redaction import redact_display_dict
from ..core.time import now_iso
from ..harness.cancellation import CancelToken, CancelledError
from .execution_receipt import turn_execution_status
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
    execution_status: str = "running"
    delivery_status: str = "not_requested"
    cancellation_requested: bool = False
    late_outcome: bool = False
    final_text: str = ""
    run_id: str | None = None
    command_id: str | None = None

    def asdict(self) -> dict[str, Any]:
        return redact_display_dict({
            "receipt_id": self.turn_id,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "source": "scheduled_session",
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
            "execution_status": self.execution_status,
            "delivery_status": self.delivery_status,
            "cancellation_requested": self.cancellation_requested,
            "late_outcome": self.late_outcome,
            "final_text": self.final_text,
        })


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
            turn_id=new_turn_id(),
            delivery_status="pending" if entry.delivery_targets else "not_requested",
        )
        token = CancelToken()
        result_lock = threading.Lock()
        self._journal(entry, result, now_ts)

        try:
            kernel_config = self.config
            if entry.execution.get("assistant_id"):
                from ..agent.assistants import AssistantStore, assistant_config
                profile = AssistantStore(self.config.paths).bind(session_id, entry.execution["assistant_id"], source="scheduled_agent")
                kernel_config = assistant_config(self.config, profile, session_id=session_id)
            kernel = self.kernel_factory(kernel_config)
        except Exception as exc:
            result.execution_status = "failed"
            if entry.delivery_targets:
                result.delivery_status = "not_sent"
            result.error = {
                "code": "kernel_boot_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            self._journal(entry, result, now_ts)
            return result

        def _invoke() -> None:
            turn_result: Any = None
            turn_error: BaseException | None = None
            try:
                turn_result = kernel.run_turn(
                    trigger=trigger,
                    strategy_id=entry.strategy_id,
                    session_id=session_id,
                    turn_id=result.turn_id,
                    cancel_token=token,
                    attached_skills=list(entry.attached_skills or []) or None,
                )
            except BaseException as exc:  # noqa: BLE001
                turn_error = exc
            # The worker owns finalization even after run_once returns. Serialize
            # it with the deadline receipt so a late result cannot be overwritten.
            with result_lock:
                result.late_outcome = result.ttl_exceeded
                result.wall_ms = int((time.monotonic() - t0) * 1000)
                result.error = None
                if turn_error is not None:
                    result.execution_status = "cancelled" if isinstance(turn_error, CancelledError) else "failed"
                    result.error = {
                        "code": "run_turn_failed",
                        "message": f"{type(turn_error).__name__}: {turn_error}",
                        "trace": traceback.format_exception(type(turn_error), turn_error, turn_error.__traceback__)[-6:],
                    }
                elif turn_result is None:
                    result.execution_status = "failed"
                    result.error = {"code": "run_turn_empty", "message": "kernel.run_turn returned None"}
                else:
                    result.decision = getattr(turn_result, "decision", None)
                    result.actions = list(getattr(turn_result, "actions", []) or [])
                    result.stopped_reason = getattr(turn_result, "stopped_reason", None)
                    result.final_text = getattr(turn_result, "final_text", "") or ""
                    result.execution_status = turn_execution_status(result.stopped_reason)
                    result.ok = result.execution_status not in {"failed", "cancelled"}
                if result.error and entry.delivery_targets:
                    result.delivery_status = "not_sent"
                elif entry.delivery_targets and self.delivery_fn is None:
                    result.delivery_status = "unavailable"
                self._journal(entry, result, now_ts)

            # Persist execution before delivery: a slow notifier must never make
            # an already finished turn look alive, nor erase its final outcome.
            if turn_result is not None and entry.delivery_targets and self.delivery_fn is not None:
                try:
                    delivery = list(self.delivery_fn(self.config, entry, turn_result) or [])
                except Exception as exc:
                    delivery = [{"ok": False, "kind": "delivery_dispatch_failed",
                                 "error": f"{type(exc).__name__}: {exc}"}]
                with result_lock:
                    result.delivery = delivery
                    result.delivery_status = ("delivered" if len(delivery) == len(entry.delivery_targets)
                                              and all(row.get("ok") is True for row in delivery) else "failed")
                    self._journal(entry, result, now_ts)

        if entry.session_ttl_seconds and entry.session_ttl_seconds > 0:
            th = threading.Thread(target=_invoke, name=f"sched-{entry.id}",
                                  daemon=True)
            th.start()
            th.join(timeout=float(entry.session_ttl_seconds))
            if th.is_alive():
                with result_lock:
                    if result.execution_status == "running":
                        result.ttl_exceeded = True
                        result.cancellation_requested = True
                        result.execution_status = "stopping"
                        token.cancel("session_ttl_exceeded")
                        self._journal(entry, result, now_ts)
                th.join(timeout=min(30.0, float(entry.session_ttl_seconds)))
                with result_lock:
                    if result.execution_status == "stopping":
                        result.execution_status = "running_unconfirmed"
                        result.error = {
                            "code": "session_ttl_exceeded",
                            "message": "Cancellation requested; execution has not returned. Check this turn's receipt before taking further action.",
                        }
                        result.wall_ms = int((time.monotonic() - t0) * 1000)
                        self._journal(entry, result, now_ts)
        else:
            _invoke()
        with result_lock:
            return deepcopy(result)

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
            **result.asdict(),
            "kind": "scheduled_session.tick",
            "ts": now_iso(),
            "ts_epoch": float(now_ts),
            "session_kind": entry.session_kind,
            "target": entry.target,
            "strategy_id": entry.strategy_id,
            "attached_skills": list(entry.attached_skills or []),
            "delivery_targets": [
                {key: t[key] for key in ("kind", "channel", "platform") if key in t}
                for t in (entry.delivery_targets or [])
            ],
        }
        try:
            jsonl.append(
                self.config.paths.journal("scheduled_session"), redact_display_dict(row),
            )
        except Exception:  # pragma: no cover - best-effort journaling
            pass


__all__ = [
    "ScheduledSessionRunner",
    "ScheduledSessionResult",
]
