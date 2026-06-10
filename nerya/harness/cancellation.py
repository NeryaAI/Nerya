"""Cooperative cancellation tokens for ``AgentKernel.run_turn`` / ``ToolRunner``.

The runtime' agent loop has interrupt + redirect semantics so an operator can
abort a long inspection/build job without waiting for the planner to
realise it should stop. The Nerya harness lacked any cancellation
contract, so a long-running tool blocked the whole turn.

This module defines a tiny :class:`CancelToken` that any caller (HTTP
handler, orchestrator, dashboard) can pass into ``run_turn`` and that
the kernel checks between iterations. ``ToolRunner`` callers also pass
it through ``call(...)`` and check it before each retry.

The token is intentionally process-local — for distributed cancellation
we would back it with a journal flag (see TODO at the bottom).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CancelToken:
    """Cooperative cancellation flag.

    The token is *passive*: a worker periodically calls
    :meth:`raise_if_cancelled` (or checks :attr:`is_set`) and aborts on
    its own. We do not interrupt threads or send signals.

    Attributes:
        reason: Operator-supplied cancellation reason, surfaced into the
            ``stopped_reason`` field of the turn result so the LLM /
            dashboard can explain *why* the loop ended.
        deadline_s: Optional unix timestamp after which the token is
            considered cancelled even if nobody flipped it explicitly.
    """

    reason: str = ""
    deadline_s: Optional[float] = None
    _flag: threading.Event = field(default_factory=threading.Event, repr=False)

    # ------------------------------------------------------------------
    # mutators
    # ------------------------------------------------------------------

    def cancel(self, reason: str = "") -> None:
        if reason and not self.reason:
            self.reason = reason
        self._flag.set()

    def reset(self) -> None:
        self._flag.clear()
        self.reason = ""
        self.deadline_s = None

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------

    @property
    def is_set(self) -> bool:
        if self._flag.is_set():
            return True
        if self.deadline_s is not None and time.time() >= float(self.deadline_s):
            self._flag.set()
            if not self.reason:
                self.reason = "deadline_exceeded"
            return True
        return False

    def raise_if_cancelled(self) -> None:
        if self.is_set:
            raise CancelledError(self.reason or "cancelled")


class CancelledError(RuntimeError):
    """Raised by ``CancelToken.raise_if_cancelled`` when a cancel was requested."""


def maybe(token: Optional[CancelToken]) -> CancelToken:
    """Return ``token`` or a fresh no-op token if ``None``.

    Lets call sites unconditionally call ``cancel.raise_if_cancelled()``
    without sprinkling ``if token is not None`` everywhere.
    """

    return token if token is not None else CancelToken()


# Process-wide registry of live tokens keyed by session/turn id, so the
# dashboard's POST /agent/interrupt can flip the right token without
# holding a Python reference.
_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, CancelToken] = {}


def register_token(key: str, token: CancelToken) -> None:
    if not key:
        return
    with _REGISTRY_LOCK:
        _REGISTRY[key] = token


def unregister_token(key: str) -> None:
    if not key:
        return
    with _REGISTRY_LOCK:
        _REGISTRY.pop(key, None)


def signal_cancel(key: str, *, reason: str = "operator_interrupt") -> bool:
    """Flip the registered token (if any). Returns whether a token was flipped."""

    if not key:
        return False
    with _REGISTRY_LOCK:
        token = _REGISTRY.get(key)
    if token is None:
        return False
    token.cancel(reason)
    return True


# ---------------------------------------------------------------------------
# Mid-turn steering (Codex TurnSteer-style operator redirect)
# ---------------------------------------------------------------------------


_STEER_MAX_PENDING = 16
_STEER_MAX_CHARS = 4_000


@dataclass
class SteerInbox:
    """Thread-safe queue of operator messages for a *running* turn.

    Cancellation kills a turn and throws away the work in flight;
    steering redirects it. The agent loop drains this inbox at the top
    of every iteration and appends each message to the live transcript
    as a pinned user message, so the model course-corrects on the next
    model round without losing any tool evidence already earned.

    Like :class:`CancelToken`, the inbox is passive and process-local:
    the HTTP layer pushes via :func:`signal_steer`, the loop polls.
    Bounded (``16`` pending messages of up to ``4000`` chars) so a
    misbehaving caller cannot balloon the transcript.
    """

    _messages: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def push(self, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        with self._lock:
            if len(self._messages) >= _STEER_MAX_PENDING:
                return False
            self._messages.append(clean[:_STEER_MAX_CHARS])
        return True

    def drain(self) -> list[str]:
        with self._lock:
            out = list(self._messages)
            self._messages.clear()
        return out

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._messages)


_STEER_REGISTRY_LOCK = threading.Lock()
_STEER_REGISTRY: dict[str, SteerInbox] = {}


def register_steer_inbox(key: str, inbox: SteerInbox) -> None:
    if not key:
        return
    with _STEER_REGISTRY_LOCK:
        _STEER_REGISTRY[key] = inbox


def unregister_steer_inbox(key: str) -> None:
    if not key:
        return
    with _STEER_REGISTRY_LOCK:
        _STEER_REGISTRY.pop(key, None)


def signal_steer(key: str, message: str) -> bool:
    """Queue an operator message for the live turn (if any).

    Returns whether a registered inbox accepted the message — ``False``
    means no turn is currently running under that key (or the inbox is
    full) and the caller should fall back to a normal new-turn message.
    """

    if not key:
        return False
    with _STEER_REGISTRY_LOCK:
        inbox = _STEER_REGISTRY.get(key)
    if inbox is None:
        return False
    return inbox.push(message)


__all__ = [
    "CancelToken",
    "CancelledError",
    "maybe",
    "register_token",
    "unregister_token",
    "signal_cancel",
    "SteerInbox",
    "register_steer_inbox",
    "unregister_steer_inbox",
    "signal_steer",
]
