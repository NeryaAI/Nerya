"""Small, durable key/value store backed by atomic JSON.

Used for runtime flags (kill switch, live trading override), small
counters (LLM daily spend), dedupe sets (intent hashes) and similar.

Concurrency: instances are shared per resolved path at module level,
so one process has exactly one :class:`StateStore` (and therefore one
lock) per file — ``compare_and_set`` excludes every thread in this
process, not just threads sharing a caller-built instance. This is
NOT a cross-process atomic CAS on the JSON file; cross-process
serialisation of strategy ticks is the runner's per-strategy file
lock's job.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import RLock
from typing import Any

from ..core.atomic_write import atomic_write_text

_LOG = logging.getLogger(__name__)

# Process-wide registry: resolved path -> shared instance.
_INSTANCES: dict[str, "StateStore"] = {}
_REGISTRY_LOCK = RLock()


class StateStore:
    def __new__(cls, path: Path):
        key = str(Path(path).resolve())
        with _REGISTRY_LOCK:
            instance = _INSTANCES.get(key)
            if instance is None:
                instance = super().__new__(cls)
                _INSTANCES[key] = instance
            return instance

    def __init__(self, path: Path):
        # __init__ re-runs on every construction of a shared instance;
        # keep it idempotent so the existing lock is never replaced
        # while another thread might be holding it.
        with _REGISTRY_LOCK:
            if getattr(self, "_initialized", False):
                return
            self.path = Path(path)
            self._lock = RLock()
            self._initialized = True

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            self._quarantine_corrupt_file(f"invalid JSON ({exc})")
            return {}
        if not isinstance(raw, dict):
            # A non-dict root (list/str/number) would crash every ``.get``
            # caller with AttributeError — treat it as corrupt too.
            self._quarantine_corrupt_file(f"non-dict JSON root ({type(raw).__name__})")
            return {}
        return raw

    def _quarantine_corrupt_file(self, reason: str) -> None:
        """Rename an unreadable state file aside instead of overwriting it.

        Returning ``{}`` on decode failure used to make the very next
        write persist the empty doc — one corrupted ``state.json``
        silently wiped everything (compat pending stops, ``_compat_pos``),
        re-arming orders that had already fired. The corrupt file is
        kept next to its old location (``<name>.corrupt-<ts>``) so it
        stays inspectable, and the store starts empty explicitly.
        """

        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}"
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.rename(target)
            _LOG.warning(
                "state store %s was unreadable (%s); quarantined to %s",
                self.path,
                reason,
                target.name,
            )
        except OSError:
            _LOG.warning(
                "state store %s is unreadable (%s) and could not be quarantined",
                self.path,
                reason,
            )

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
