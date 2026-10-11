"""Jittered exponential backoff for provider retries.

Ported from The runtime' `agent/retry_utils.py` — jitter decorrelates
concurrent retries so multiple sessions hitting the same provider don't
all retry at the same instant.
"""

from __future__ import annotations

import math
import random
import errno
import socket
import ssl
import threading
import time
import urllib.error
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

_jitter_counter = 0
_jitter_lock = threading.Lock()

# HTTP statuses that should be retried. ``529`` is a non-standard provider
# overload/peak-busy status used by some OpenAI-compatible endpoints.
_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524, 529})


def is_retryable_stream_transport_error(error: BaseException) -> bool:
    """Retry temporary transport failure only; never mask TLS or local config errors.

    ZCode distinguishes network/timeout from invalid configuration and TLS.
    This predicate is used only before a model stream has emitted a payload;
    the caller must independently enforce the output-commit boundary.
    """
    if isinstance(error, urllib.error.HTTPError):
        # HTTP responses must be classified by status and Retry-After instead.
        return False
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        if error.reason is not error:
            return is_retryable_stream_transport_error(error.reason)
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if not isinstance(error, OSError):
        return False
    if isinstance(error, (PermissionError, FileNotFoundError, IsADirectoryError)):
        return False
    if error.errno in {errno.EACCES, errno.EPERM, errno.ENOENT, errno.EINVAL, errno.EBADF}:
        return False
    return True


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


# These business codes require operator action, not another RPM retry.
_QUOTA_CODES = frozenset({
    "insufficient_quota", "credit_balance_exhausted", "quota_exhausted",
    "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "billing_hard_limit_reached",
})


def is_quota_error(doc: Mapping[str, Any] | None) -> bool:
    data = doc if isinstance(doc, Mapping) else {}
    raw_error = data.get("error")
    error = raw_error if isinstance(raw_error, Mapping) else {}
    if any(str(value).lower() in _QUOTA_CODES for value in (
        data.get("code"), error.get("code"), error.get("type"),
    )):
        return True
    # Some compatible providers return only a billing message with HTTP 429.
    # Do not match generic quota/RPM/token limits: those can recover after a wait.
    message = " ".join(str(value or "") for value in (
        data.get("message"), error.get("message"),
        raw_error if isinstance(raw_error, str) else "",
    )).lower()
    return any(phrase in message for phrase in (
        "insufficient balance", "insufficient credit", "credit balance is too low",
        "no resource package", "billing hard limit", "余额不足", "账户欠费",
    ))


def provider_retryable(
    status: int, doc: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> bool:
    normalized = {str(k).lower(): str(v).strip().lower() for k, v in (headers or {}).items()}
    if normalized.get("x-should-retry") in {"false", "0"} or is_quota_error(doc):
        return False
    return is_retryable_status(status)


def parse_retry_after(
    headers: Mapping[str, str] | None, *, now: float | None = None,
) -> float | None:
    """Preserve provider cooldowns (seconds, HTTP-date, or millisecond header)."""
    normalized = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    for key, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = normalized.get(key)
        if not raw:
            continue
        try:
            value = float(raw) * scale
        except ValueError:
            if key != "retry-after":
                continue
            try:
                value = max(0.0, parsedate_to_datetime(raw).timestamp() - (time.time() if now is None else now))
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


def retry_delay(
    attempt: int, *, base_delay: float = 2.0, max_delay: float = 60.0,
    headers: Mapping[str, str] | None = None, jitter: bool = True,
) -> float:
    """One retry curve, like ZCode: 2s -> 60s, equal jitter, provider wait first.

    The local cap must not shorten a valid Retry-After. The caller owns its
    overall deadline and must stop rather than issue an early retry.
    """
    explicit = parse_retry_after(headers)
    if explicit is not None:
        return explicit
    delay = min(max(0.0, max_delay), max(0.0, base_delay) * 2 ** min(62, max(0, attempt - 1)))
    return delay * random.uniform(0.5, 1.0) if jitter and delay else delay


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
    "is_retryable_stream_transport_error",
    "retry_call",
    "retry_delay",
    "parse_retry_after",
    "provider_retryable",
    "is_quota_error",
]
