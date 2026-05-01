"""Rate-limit state parsed from provider response headers.

Ported from The runtime' `agent/rate_limit_tracker.py` — simplified to the
fields Nerya actually needs (per-minute + per-hour requests/tokens).

Recognised header conventions:
    x-ratelimit-limit-requests, x-ratelimit-limit-requests-1h
    x-ratelimit-remaining-requests, ...-remaining-requests-1h
    x-ratelimit-reset-requests, ...-reset-requests-1h
    x-ratelimit-limit-tokens, ...-remaining-tokens, ...-reset-tokens
    (and ``-1h`` variants for all tokens fields)

Anthropic/OpenAI/OpenRouter/Nous/Groq all use subsets of this scheme.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Mapping


@dataclass
class RateLimitBucket:
    limit: int = 0
    remaining: int = 0
    reset_seconds: float = 0.0
    captured_at: float = 0.0

    @property
    def used(self) -> int:
        return max(0, self.limit - self.remaining)

    @property
    def usage_pct(self) -> float:
        if self.limit <= 0:
            return 0.0
        return (self.used / self.limit) * 100.0

    @property
    def remaining_seconds_now(self) -> float:
        if self.captured_at == 0:
            return 0.0
        elapsed = time.time() - self.captured_at
        return max(0.0, self.reset_seconds - elapsed)


@dataclass
class RateLimitState:
    requests_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    requests_hour: RateLimitBucket = field(default_factory=RateLimitBucket)
    tokens_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    tokens_hour: RateLimitBucket = field(default_factory=RateLimitBucket)
    captured_at: float = 0.0
    provider: str = ""

    @property
    def has_data(self) -> bool:
        return self.captured_at > 0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_rate_limit_headers(
    headers: Mapping[str, str],
    provider: str = "",
) -> RateLimitState | None:
    if not headers:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    if not any(k.startswith("x-ratelimit-") for k in lowered):
        return None

    now = time.time()

    def bucket(resource: str, suffix: str = "") -> RateLimitBucket:
        tag = f"{resource}{suffix}"
        return RateLimitBucket(
            limit=_safe_int(lowered.get(f"x-ratelimit-limit-{tag}")),
            remaining=_safe_int(lowered.get(f"x-ratelimit-remaining-{tag}")),
            reset_seconds=_safe_float(lowered.get(f"x-ratelimit-reset-{tag}")),
            captured_at=now,
        )

    state = RateLimitState(
        requests_min=bucket("requests"),
        requests_hour=bucket("requests", "-1h"),
        tokens_min=bucket("tokens"),
        tokens_hour=bucket("tokens", "-1h"),
        captured_at=now,
        provider=provider,
    )
    if all(b.limit == 0 for b in (
        state.requests_min, state.requests_hour,
        state.tokens_min, state.tokens_hour)):
        return None
    return state


class RateLimitStore:
    """In-process store keyed by ``(provider, api_key_fingerprint)``."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._store: dict[tuple[str, str], RateLimitState] = {}

    def update(self, provider: str, key_fp: str, state: RateLimitState | None) -> None:
        if state is None:
            return
        with self._lock:
            self._store[(provider, key_fp)] = state

    def get(self, provider: str, key_fp: str) -> RateLimitState | None:
        with self._lock:
            return self._store.get((provider, key_fp))

    def should_defer(self, provider: str, key_fp: str) -> float:
        """Return seconds to sleep if we're about to blow the rate limit.

        Threshold: if less than 2 requests remain in the minute bucket, wait
        until the window resets. Returns 0 if safe to proceed.
        """
        state = self.get(provider, key_fp)
        if state is None or not state.has_data:
            return 0.0
        if state.requests_min.limit > 0 and state.requests_min.remaining <= 1:
            return max(0.0, state.requests_min.remaining_seconds_now)
        return 0.0


_GLOBAL_STORE = RateLimitStore()


def global_store() -> RateLimitStore:
    return _GLOBAL_STORE


__all__ = [
    "RateLimitBucket", "RateLimitState",
    "parse_rate_limit_headers",
    "RateLimitStore", "global_store",
]
