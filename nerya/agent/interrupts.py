"""Cooperative interrupts (cancel / pause / resume) for long agent turns.

- Interrupts should always succeed: a cancel at any point flushes the
  current step, stops the model stream, drains the journal, and yields
  control back to the operator.

Why
---
Today the agent runs a turn end-to-end with no first-class way for an
operator to bail out mid-flight. A 4-minute team.run_team locks the
gateway; a runaway loop can only be killed by SIGTERM.

This module provides a *cooperative* cancellation primitive: every
long-running step (LLM stream pump, ToolRunner.call, terminal exec,
team.run_team, …) periodically checks ``InterruptToken.poll()`` and
unwinds cleanly when an interrupt fires. The token also supports
*pause* / *resume* so the dashboard can show "agent paused — click to
resume" without losing turn state.

Design
------
- One :class:`InterruptToken` per turn, owned by the kernel.
- API server exposes ``POST /agent/turns/{turn_id}/interrupt`` (and
  ``/pause`` / ``/resume``) that flips the token state.
- Long loops check ``token.poll()`` at every iteration and either:
    * proceed when state is ``running``
    * sleep+wait when state is ``paused`` (with a wake event)
    * raise :class:`Interrupted` when state is ``cancelled``
- The kernel translates :class:`Interrupted` into a clean turn close
  with ``stopped_reason="interrupted"`` and a synthetic
  ``send_message`` (always budget-immune) summarising what was done.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


__all__ = [
    "InterruptToken",
    "Interrupted",
    "TurnControlRegistry",
    "default_registry",
]


class Interrupted(Exception):
    """Raised when an interruptible loop observes a cancellation."""

    def __init__(self, *, reason: str = "operator_cancel", origin: str = "") -> None:
        self.reason = reason
        self.origin = origin
        super().__init__(f"interrupted: {reason}" + (f" ({origin})" if origin else ""))


@dataclass
class InterruptToken:
    """Per-turn cooperative cancel/pause primitive.

    States:

    - ``running``    — normal execution; ``poll()`` returns immediately.
    - ``paused``     — ``poll()`` blocks on an internal Event until
                       resumed or cancelled.
    - ``cancelled``  — ``poll()`` raises :class:`Interrupted`.

    Once cancelled the token cannot be reset; the kernel constructs
    a fresh token for the next turn.
    """

    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _state: str = "running"
    _reason: str = ""
    _origin: str = ""
    _wake: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def origin(self) -> str:
        with self._lock:
            return self._origin

    def cancel(self, *, reason: str = "operator_cancel", origin: str = "") -> None:
        with self._lock:
            self._state = "cancelled"
            self._reason = reason or self._reason or "operator_cancel"
            self._origin = origin or self._origin
            self._wake.set()

    def pause(self, *, reason: str = "operator_pause", origin: str = "") -> None:
        with self._lock:
            if self._state == "cancelled":
                return  # cancellation is sticky
            self._state = "paused"
            self._reason = reason
            self._origin = origin
            self._wake.clear()

    def resume(self) -> None:
        with self._lock:
            if self._state == "cancelled":
                return
            self._state = "running"
            self._reason = ""
            self._origin = ""
            self._wake.set()

    def is_running(self) -> bool:
        return self.state == "running"

    def is_cancelled(self) -> bool:
        return self.state == "cancelled"

    def is_paused(self) -> bool:
        return self.state == "paused"

    # ---- the hot path -------------------------------------------------------

    def poll(self, *, timeout_s: float | None = None) -> None:
        """Check the token. Raises :class:`Interrupted` if cancelled.

        ``timeout_s`` controls how long to wait when paused: ``None``
        blocks until resumed/cancelled, a positive number waits up to
        that many seconds. Long-running loops should call ``poll`` at
        every meaningful boundary (per LLM token chunk, per tool call,
        per shell-output read).
        """

        with self._lock:
            state = self._state
        if state == "running":
            return
        if state == "cancelled":
            raise Interrupted(reason=self._reason or "operator_cancel",
                              origin=self._origin)
        # paused — block until resumed/cancelled
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        while True:
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._wake.wait(timeout=wait)
            with self._lock:
                state = self._state
            if state == "cancelled":
                raise Interrupted(reason=self._reason or "operator_cancel",
                                  origin=self._origin)
            if state == "running":
                return
            if deadline is not None and time.monotonic() >= deadline:
                return  # caller decides what to do after timeout

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                "turn_id": self.turn_id,
                "state": self._state,
                "reason": self._reason,
                "origin": self._origin,
            }


class TurnControlRegistry:
    """Process-wide map of ``turn_id -> InterruptToken``.

    The HTTP control endpoints look up the token by turn id, the kernel
    registers a fresh token at the start of every turn, and removes it
    on completion.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, InterruptToken] = {}
        self._lock = threading.Lock()

    def register(self, token: InterruptToken) -> None:
        with self._lock:
            self._tokens[token.turn_id] = token

    def get(self, turn_id: str) -> Optional[InterruptToken]:
        with self._lock:
            return self._tokens.get(turn_id)

    def discard(self, turn_id: str) -> None:
        with self._lock:
            self._tokens.pop(turn_id, None)

    def list_active(self) -> list[dict[str, str]]:
        with self._lock:
            return [t.snapshot() for t in self._tokens.values()]


_default_registry = TurnControlRegistry()


def default_registry() -> TurnControlRegistry:
    """Return the process-wide default registry used by HTTP handlers."""

    return _default_registry
