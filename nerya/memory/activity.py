"""Append-only activity log for the memory subsystem.

Every memory write (or skipped write — dedupe, disabled rule) and every
search emits one JSONL record under
``<workspace>/state/memory/activity.jsonl``. The dashboard tails this
file to render the live "what is Nerya remembering / searching"
streams.

We deliberately keep the format flat:

.. code-block:: json

    {
      "ts":         "2026-05-06T17:01:23Z",
      "kind":       "write_ok",      // write_ok | write_skipped | search
      "category":   "learning",      // for writes
      "key":        "trading.preferred_horizon",
      "title":      "swing horizon (3-5d)",
      "preview":    "operator prefers 3-5 day swing horizons over scalping",
      "hash":       "1f3c…",
      "skip_reason":"",              // for write_skipped
      "query":      "",              // for searches
      "result_count": 0,             // for searches
      "latency_ms": 0,
      "source":     "chat:session_id",
      "actor_id":   "default"
    }

The log is rotated when it exceeds ``max_bytes`` (default 4 MiB):
the file is renamed to ``activity.jsonl.1`` and a fresh one starts.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..core.atomic_write import atomic_write_bytes
from ..core.config import Config


__all__ = [
    "MemoryActivityEvent",
    "MemoryActivityLog",
    "activity_log_path",
]


_LOCK = threading.RLock()


def activity_log_path(config: Config) -> Path:
    return config.paths.state / "memory" / "activity.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _preview(text: str, *, limit: int = 240) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


@dataclass
class MemoryActivityEvent:
    ts: str = field(default_factory=_now)
    kind: str = "write_ok"
    category: str = ""
    key: str = ""
    title: str = ""
    preview: str = ""
    hash: str = ""
    skip_reason: str = ""
    query: str = ""
    result_count: int = 0
    latency_ms: int = 0
    source: str = ""
    actor_id: str = "default"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def write_ok(
        cls,
        *,
        category: str,
        key: str,
        title: str,
        preview: str,
        hash: str,
        source: str = "",
        actor_id: str = "default",
        extra: dict[str, Any] | None = None,
    ) -> "MemoryActivityEvent":
        return cls(
            kind="write_ok",
            category=category,
            key=key,
            title=title,
            preview=preview,
            hash=hash,
            source=source,
            actor_id=actor_id,
            extra=dict(extra or {}),
        )

    @classmethod
    def write_skipped(
        cls,
        *,
        category: str,
        skip_reason: str,
        title: str = "",
        preview: str = "",
        hash: str = "",
        source: str = "",
        actor_id: str = "default",
        extra: dict[str, Any] | None = None,
    ) -> "MemoryActivityEvent":
        return cls(
            kind="write_skipped",
            category=category,
            skip_reason=skip_reason,
            title=title,
            preview=preview,
            hash=hash,
            source=source,
            actor_id=actor_id,
            extra=dict(extra or {}),
        )

    @classmethod
    def search(
        cls,
        *,
        query: str,
        result_count: int,
        latency_ms: int = 0,
        source: str = "",
        actor_id: str = "default",
        extra: dict[str, Any] | None = None,
    ) -> "MemoryActivityEvent":
        return cls(
            kind="search",
            query=query,
            result_count=int(result_count),
            latency_ms=int(latency_ms),
            source=source,
            actor_id=actor_id,
            extra=dict(extra or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class MemoryActivityLog:
    config: Config
    max_bytes: int = 4 * 1024 * 1024
    keep_rotations: int = 2

    @property
    def path(self) -> Path:
        return activity_log_path(self.config)

    def append(self, event: MemoryActivityEvent) -> None:
        record = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
            except OSError:
                size = 0
            if size and size + len(record.encode("utf-8")) > self.max_bytes:
                self._rotate()
            with self.path.open("ab") as fh:
                fh.write(record.encode("utf-8"))

    def _rotate(self) -> None:
        # Keep the last ``keep_rotations`` files. ``activity.jsonl`` →
        # ``activity.jsonl.1`` → ``activity.jsonl.2`` → discard.
        for idx in range(self.keep_rotations, 0, -1):
            src = self.path.with_suffix(self.path.suffix + (f".{idx - 1}" if idx > 1 else ""))
            if src == self.path:
                src = self.path
            else:
                src = self.path.with_name(self.path.name + f".{idx - 1}")
            dst = self.path.with_name(self.path.name + f".{idx}")
            if src.exists():
                try:
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
                except OSError:
                    # Rotation is best-effort; if it fails, fall back to
                    # truncating the live file.
                    try:
                        atomic_write_bytes(self.path, b"")
                    except OSError:
                        pass
                    return

    def tail(
        self,
        *,
        limit: int = 100,
        kinds: Iterable[str] | None = None,
        category: str = "",
    ) -> list[dict[str, Any]]:
        """Return the last ``limit`` events (newest last in the file → newest last in the list).

        ``kinds`` filters by event kind (``write_ok``, ``write_skipped``,
        ``search``); ``category`` filters by memory category id.
        """

        if not self.path.exists():
            return []
        wanted = set(kinds or ())
        cat = (category or "").strip().lower()
        with _LOCK:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        # We could mmap + tail backwards, but for operator-scale logs
        # (<5 MiB) a forward parse is fine.
        out: list[dict[str, Any]] = []
        for raw in lines[-max(0, int(limit)) * 4 :]:  # over-fetch then filter
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if wanted and obj.get("kind") not in wanted:
                continue
            if cat and obj.get("category") != cat:
                continue
            out.append(obj)
        return out[-int(limit) :] if limit else out

    def stats(self) -> dict[str, Any]:
        """Cheap summary: total events + per-kind / per-category counters."""

        if not self.path.exists():
            return {"total": 0, "by_kind": {}, "by_category": {}, "size_bytes": 0}
        size = 0
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        by_kind: dict[str, int] = {}
        by_category: dict[str, int] = {}
        total = 0
        with _LOCK:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return {"total": 0, "by_kind": {}, "by_category": {}, "size_bytes": size}
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            total += 1
            kind = str(obj.get("kind") or "")
            if kind:
                by_kind[kind] = by_kind.get(kind, 0) + 1
            cat = str(obj.get("category") or "")
            if cat:
                by_category[cat] = by_category.get(cat, 0) + 1
        return {
            "total": total,
            "by_kind": by_kind,
            "by_category": by_category,
            "size_bytes": size,
        }
