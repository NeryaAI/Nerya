"""Small, durable key/value store backed by atomic JSON.

Used for runtime flags (kill switch, live trading override), small
counters (LLM daily spend), dedupe sets (intent hashes) and similar."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from ..core.atomic_write import atomic_write_text


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True, default=str))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            data = self._read()
            data.update(kwargs)
            self._write(data)

    def bump(self, key: str, amount: float = 1.0) -> float:
        with self._lock:
            data = self._read()
            data[key] = float(data.get(key, 0.0)) + amount
            self._write(data)
            return data[key]

    def add_to_set(self, key: str, value: str) -> bool:
        """Returns True if the value was new, False if it was already there."""
        with self._lock:
            data = self._read()
            bag = data.get(key, [])
            if not isinstance(bag, list):
                bag = []
            if value in bag:
                return False
            bag.append(value)
            data[key] = bag
            self._write(data)
            return True

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._read())
