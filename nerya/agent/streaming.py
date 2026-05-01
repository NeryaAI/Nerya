"""Streaming event bus for dashboard / gateway / TUI delivery.

The kernel emits structured events as a turn progresses; subscribers
(Dashboard SSE, Gateway WebSocket, CLI TUI) receive them in order
without polling the journal. Without this layer every UI either had
to tail the JSONL files or wait for the turn to complete, which made
long Hermes-style coding loops feel laggy.

Plan 03 P0 §3 wired the basic in-memory bus. Plan 12 (Context,
Streaming, And State Architecture) raises the bar with a *resume
contract*:

- every event carries a monotonic ``seq`` and a stable ``event_id``;
- subscribers can ask for "everything I missed" via
  :meth:`StreamingEventBus.events_since` /
  ``GET /agent/stream/events?after_seq=N``;
- the in-memory ring is large enough (default 500 events) to cover
  routine reconnects, dropped websockets, and dashboard reloads.

Wire:

- ``StreamingEventBus.publish(kind, **payload)`` is called from
  :class:`nerya.agent.kernel.AgentKernel` and the tool runner whenever
  a notable event happens.
- Subscribers register a callback (``subscribe(callback)``) and get
  every subsequent event. They can drop the callback by calling the
  returned ``unsubscribe`` function.
- The bus is thread-safe (Lock-guarded) so HTTP request handlers can
  safely stream from a background turn.

Events are intentionally generic dicts with a ``kind`` discriminator
so the schema can grow without churning every subscriber. The kernel
publishes:

- ``message.delta`` — partial assistant content
- ``tool.start`` — tool/skill call started (mirrors skill.call.start)
- ``tool.progress`` — per-tool progress bump (e.g. "12/100 rows")
- ``tool.complete`` — tool finished, success or failure
- ``approval.request`` — operator approval requested
- ``turn.step`` — one :class:`~nerya.agent.transcript_blocks.BlockEnvelope`
  emitted by the workspace-native loop (text / thinking / tool_use /
  tool_result)
- ``turn.complete`` — turn finished

Plan refs:
``docs/plans/2026-04-25-nerya-hermes-capability-gap-audit/03-agent-loop-context-session.md``
P0 §3,
``docs/plans/2026-04-25-nerya-hermes-capability-gap-audit/12-context-streaming-state.md``
"Streaming Contract".
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


_EventCb = Callable[[dict[str, Any]], None]


@dataclass
class StreamingEventBus:
    """Process-local pub/sub.

    ``_max_replay`` controls the in-memory ring buffer the bus retains
    so that late subscribers and reconnecting clients can replay
    missed events. The default is intentionally generous (500): a
    typical turn emits a few dozen events, so the ring covers a few
    consecutive turns even under pressure. Tests build their own bus
    with smaller rings to keep assertions tight.
    """

    _subscribers: dict[str, _EventCb] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_events: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0
    _max_replay: int = 500

    def subscribe(self, callback: _EventCb) -> Callable[[], None]:
        """Register ``callback``. Returns an unsubscribe function."""

        sid = uuid.uuid4().hex
        with self._lock:
            self._subscribers[sid] = callback
            replay = list(self._last_events)
        for ev in replay:
            try:
                callback(ev)
            except Exception:
                pass

        def _drop() -> None:
            with self._lock:
                self._subscribers.pop(sid, None)

        return _drop

    def publish(self, kind: str, **payload: Any) -> dict[str, Any]:
        """Send ``kind`` to every subscriber. Returns the published event.

        The returned event is the same dict every subscriber receives
        and that ``recent()`` will return — including the freshly
        assigned ``seq`` and ``event_id``. Callers that journal the
        event to disk should use the returned value so the persisted
        copy carries the same identifiers.
        """

        with self._lock:
            self._seq += 1
            seq = self._seq
        # ``payload`` may legitimately set ``ts`` / ``event_id`` (e.g.
        # when replaying a persisted event) so we honour those values
        # but always overwrite ``seq`` with the bus-monotonic counter
        # to keep ordering invariants in the ring.
        event: dict[str, Any] = {
            "kind": kind,
            "seq": seq,
            "event_id": payload.pop("event_id", None) or uuid.uuid4().hex,
            "ts": payload.pop("ts", None) or time.time(),
            **payload,
        }
        with self._lock:
            self._last_events.append(event)
            if len(self._last_events) > self._max_replay:
                self._last_events = self._last_events[-self._max_replay:]
            subs = list(self._subscribers.values())
        for cb in subs:
            try:
                cb(event)
            except Exception:
                # never let one bad subscriber break the others
                pass
        return event

    # ---- replay / cursor API -----------------------------------------

    def replay_buffer(self) -> list[dict[str, Any]]:
        """Best-effort recent events for late subscribers.

        Returns a copy of the in-memory ring so callers cannot mutate
        the bus state.
        """

        with self._lock:
            return list(self._last_events)

    def recent(self, *, after_seq: int | None = None) -> list[dict[str, Any]]:
        """Return events newer than ``after_seq`` (or the whole ring).

        Plan 05 P0 §1 / Plan 12 streaming contract: a polling client
        sends the highest ``seq`` it already saw and gets back only
        the strictly newer events. ``after_seq=None`` (the default)
        returns the entire ring, preserving the original Plan 03
        behaviour.
        """

        with self._lock:
            buf = list(self._last_events)
        if after_seq is None:
            return buf
        return [ev for ev in buf if int(ev.get("seq") or 0) > int(after_seq)]

    def events_since(self, after_seq: int) -> list[dict[str, Any]]:
        """Alias for :meth:`recent` with a required cursor argument.

        Used by HTTP handlers that want to make the cursor intent
        explicit at the call site.
        """

        return self.recent(after_seq=after_seq)

    def latest_seq(self) -> int:
        """Return the highest ``seq`` ever assigned by this bus.

        Clients can call this to obtain a cursor before subscribing,
        so that the first ``after_seq`` poll returns only events that
        arrived *after* the subscription started.
        """

        with self._lock:
            return self._seq

    def cursor_after(self, events: Iterable[dict[str, Any]]) -> int:
        """Return the largest ``seq`` in ``events`` (or ``latest_seq``).

        Convenience for callers that want to advance their cursor
        based on the events they just received without re-walking
        the ring.
        """

        max_seq = 0
        for ev in events:
            try:
                s = int(ev.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if s > max_seq:
                max_seq = s
        if max_seq == 0:
            return self.latest_seq()
        return max_seq

    def next_seq(self) -> int:
        """Reserve and return the next ``seq`` value.

        Useful when a caller needs to journal an event to disk *before*
        publishing it (so the on-disk row carries the same seq the
        live subscribers will see). The reserved seq is consumed —
        callers must subsequently publish via
        ``publish(..., seq=<reserved>)`` style helpers if they want
        the values to match. Today no caller relies on this; the helper
        is exposed so the upcoming context-manifest writer can use it.
        """

        with self._lock:
            self._seq += 1
            return self._seq

    def clear(self) -> None:
        """Drop every subscriber and reset the buffer/seq.

        Tests rely on this to start from a clean slate. The seq
        counter resets to zero so deterministic assertions work.
        """

        with self._lock:
            self._subscribers.clear()
            self._last_events.clear()
            self._seq = 0


_default_bus = StreamingEventBus()


def get_default_bus() -> StreamingEventBus:
    """Return the process-wide bus used by the kernel + HTTP handlers."""

    return _default_bus


__all__ = ["StreamingEventBus", "get_default_bus"]
