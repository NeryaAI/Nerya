"""Jittered exponential backoff for provider retries.

Ported from Hermes' `agent/retry_utils.py` — jitter decorrelates
concurrent retries so multiple sessions hitting the same provider don't
all retry at the same instant.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

_jitter_counter = 0
_jitter_lock = threading.Lock()

# HTTP statuses that should be retried
_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff in seconds.

    ``attempt`` is 1-based. Returns ``min(base * 2^(attempt-1), max) + jitter``.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)
    return delay + jitter


def is_retryable_status(status: int) -> bool:
    return int(status) in _TRANSIENT_STATUSES


def retry_call(
    fn: Callable[[], tuple[int, Any]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_after_parser: Callable[[Any], float | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, Any]:
    """Retry ``fn`` while it returns a retryable HTTP status.

    ``fn`` must return ``(status, body)``. When present, ``retry_after_parser``
    can inspect the body/headers dict and return an explicit delay (seconds);
    falls back to jittered backoff.
    """
    last: tuple[int, Any] = (0, None)
    for attempt in range(1, max_attempts + 1):
        status, body = fn()
        last = (status, body)
        if not is_retryable_status(status):
            return status, body
        if attempt >= max_attempts:
            return status, body
        delay = None
        if retry_after_parser is not None:
            try:
                delay = retry_after_parser(body)
            except Exception:
                delay = None
        if delay is None:
            delay = jittered_backoff(attempt, base_delay=base_delay,
                                       max_delay=max_delay)
        sleep(delay)
    return last


__all__ = [
    "jittered_backoff",
    "is_retryable_status",
    "retry_call",
]
