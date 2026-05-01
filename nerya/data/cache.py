"""In-memory TTL cache used by market_data_skill."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataCache:
    ttl_seconds: float = 5.0
    _store: dict[str, tuple[float, Any]] = field(default_factory=dict)

    def get(self, key: str):
        hit = self._store.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > self.ttl_seconds:
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)
