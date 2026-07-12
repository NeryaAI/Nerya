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
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..core.atomic_write import atomic_write_bytes
from ..core.config import Config
from .content_scanner import scan_memory_content


__all__ = [
    "MemoryActivityEvent",
    "MemoryActivityLog",
    "activity_log_path",
]


_LOCK = threading.RLock()

_fcntl: Any
_msvcrt: Any
try:  # pragma: no cover - platform branch
    import fcntl as _fcntl

    _msvcrt = None
except ImportError:  # pragma: no cover - platform branch
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None


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
        query_text = str(query or "")
        if scan_memory_content(query_text):
            query_text = "[redacted unsafe memory query]"
        return cls(
            kind="search",
            query=query_text,
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
            with _file_lock(self._lock_path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = self.path.stat().st_size if self.path.exists() else 0
                except OSError:
                    size = 0
                if size and size + len(record.encode("utf-8")) > self.max_bytes:
                    self._rotate()
                with self.path.open("ab") as fh:
                    fh.write(record.encode("utf-8"))

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def scrub(
        self,
        *,
        actor_id: str,
        key: str = "",
        hashes: Iterable[str] | None = None,
    ) -> int:
        """Remove matching events from the live log and every rotation.

        An event is removed only when ``actor_id`` matches and either its
        stable key or its activity hash is selected. Each file replacement is
        atomic, while the shared activity lock prevents append/rotation from
        racing the complete multi-file scrub.
        """

        actor = str(actor_id or "").strip()
        wanted_key = str(key or "").strip()
        hash_values = (hashes,) if isinstance(hashes, str) else (hashes or ())
        wanted_hashes = {
            str(value).strip() for value in hash_values if str(value or "").strip()
        }
        if not actor or (not wanted_key and not wanted_hashes):
            return 0

        removed = 0
        with _LOCK:
            with _file_lock(self._lock_path):
                for path in self._log_paths():
                    try:
                        raw = path.read_bytes()
                    except FileNotFoundError:
                        continue
                    kept: list[bytes] = []
                    changed = False
                    for line in raw.splitlines(keepends=True):
                        if self._matches_scrub(
                            line,
                            actor_id=actor,
                            key=wanted_key,
                            hashes=wanted_hashes,
                        ):
                            removed += 1
                            changed = True
                        else:
                            kept.append(line)
                    if changed:
                        atomic_write_bytes(path, b"".join(kept))
        return removed

    def _log_paths(self) -> list[Path]:
        """Return live plus every numeric rotation, oldest name last."""

        paths = [self.path] if self.path.exists() else []
        rotations = sorted(
            (
                path
                for path in self.path.parent.glob(self.path.name + ".*")
                if path.name.removeprefix(self.path.name + ".").isdigit()
            ),
            key=lambda path: int(path.name.rsplit(".", 1)[-1]),
        )
        paths.extend(rotations)
        return paths

    @staticmethod
    def _matches_scrub(
        line: bytes,
        *,
        actor_id: str,
        key: str,
        hashes: set[str],
    ) -> bool:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(obj, dict) or str(obj.get("actor_id") or "") != actor_id:
            return False
        extra = obj.get("extra") if isinstance(obj.get("extra"), dict) else {}
        event_key = str(obj.get("key") or extra.get("key") or "").strip()
        event_hash = str(obj.get("hash") or "").strip()
        return bool((key and event_key == key) or (event_hash and event_hash in hashes))

    def _rotate(self) -> None:
        # Keep the last ``keep_rotations`` files. ``activity.jsonl`` →
        # ``activity.jsonl.1`` → ``activity.jsonl.2`` → discard.
        for idx in range(self.keep_rotations, 0, -1):
            src = self.path.with_suffix(
                self.path.suffix + (f".{idx - 1}" if idx > 1 else "")
            )
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
                return {
                    "total": 0,
                    "by_kind": {},
                    "by_category": {},
                    "size_bytes": size,
                }
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


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process lock shared by append, rotation, and scrub."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if _fcntl is None and _msvcrt is None:
        yield
        return
    if _msvcrt is not None:
        with path.open("a+b") as bootstrap:
            bootstrap.seek(0, 2)
            if bootstrap.tell() == 0:
                bootstrap.write(b" ")
                bootstrap.flush()

    mode = "r+" if _msvcrt is not None else "a+"
    handle = open(path, mode, encoding="utf-8")
    try:
        if _fcntl is not None:
            _fcntl.flock(handle, _fcntl.LOCK_EX)
        else:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if _fcntl is not None:
                _fcntl.flock(handle, _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()
