"""Clock abstraction. Everything that stamps events uses this, so tests
can inject a deterministic clock."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

_clock: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc)


def now() -> datetime:
    return _clock()


def now_iso() -> str:
    return now().isoformat()


def set_clock(fn: Callable[[], datetime]) -> None:
    """Override the global clock (for tests)."""
    global _clock
    _clock = fn


def reset_clock() -> None:
    global _clock
    _clock = lambda: datetime.now(tz=timezone.utc)
