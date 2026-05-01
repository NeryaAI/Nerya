"""Very small in-memory rate limiter keyed by channel."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    max_per_minute: int = 30
    _windows: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, channel: str) -> bool:
        now = time.time()
        dq = self._windows.setdefault(channel, deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= self.max_per_minute:
            return False
        dq.append(now)
        return True
