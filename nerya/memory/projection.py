"""Human-readable, rebuildable projections of canonical memory."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from .store import MemoryRecord, MemoryStore


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


GENERATED_PROJECTION_MARKER = (
    "<!-- nerya-memory-projection: generated-from=nerya.db; "
    "do-not-edit; do-not-import -->"
)


class MemoryProjection:
    """Rebuild JSONL and Markdown compatibility files from SQLite."""

    def __init__(self, config: Config, store: MemoryStore) -> None:
        self.config = config
        self.store = store

    def sync(self, *, actor_id: str) -> bool:
        owner_actor = str(
            self.config.get("memory.legacy_owner_actor", "default") or "default"
        ).strip()
        if not owner_actor or actor_id != owner_actor:
            return False
        lock_path = self.config.paths.memory / ".projection.lock"
        with _file_lock(lock_path):
            records = self.store.projection_records(actor_id=actor_id)
            active = [record for record in records if record.status == "active"]
            self._write_jsonl(active)
            self._write_markdown(records, active)
        return True

    def _write_jsonl(self, records: list[MemoryRecord]) -> None:
        rows = []
        for record in records:
            if record.scope == "session" or record.category.startswith("notebook_"):
                continue
            targets = self._targets(record)
            rows.append(
                {
                    "ts": _iso(record.created_at),
                    "actor_id": record.actor_id,
                    "scope": record.scope,
                    "file": targets[0] if targets else "",
                    "strategy_id": record.strategy_id,
                    "key": record.stable_key,
                    "value": record.content,
                    "tags": list(record.tags),
                    "source_turn": record.source_turn_id,
                    "superseded": False,
                    "memory_id": record.memory_id,
                    "category": record.category,
                }
            )
        text = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        )
        atomic_write_text(self.config.paths.memory_index, text)

    def _write_markdown(
        self,
        all_records: list[MemoryRecord],
        active_records: list[MemoryRecord],
    ) -> None:
        targets = {target for record in all_records for target in self._targets(record)}
        grouped: dict[str, list[MemoryRecord]] = {target: [] for target in targets}
        for record in active_records:
            for target in self._targets(record):
                grouped.setdefault(target, []).append(record)

        for target, records in grouped.items():
            path = self._safe_path(target)
            if path is None:
                continue
            blocks = [
                "# Nerya memory projection\n",
                f"{GENERATED_PROJECTION_MARKER}\n",
            ]
            for record in records:
                title = record.title or record.stable_key or "(memory)"
                metadata = [
                    f"`{_iso(record.created_at)}`",
                    f"`{record.category}`",
                    f"`memory-id={record.memory_id}`",
                ]
                if record.stable_key:
                    metadata.append(f"`key={record.stable_key}`")
                blocks.append(
                    f"\n## {title}\n"
                    + " · ".join(metadata)
                    + f"\n\n{record.content.strip()}\n"
                )
            atomic_write_text(path, "".join(blocks))

    def _targets(self, record: MemoryRecord) -> tuple[str, ...]:
        if record.scope == "session":
            return ()
        if record.scope == "strategy":
            if not record.strategy_id:
                return ()
            return (f"strategies/{record.strategy_id}/learnings.md",)
        return tuple(record.target_files)

    def _safe_path(self, target: str) -> Path | None:
        raw = str(target or "").strip()
        if not raw:
            return None
        if "/" not in raw and "\\" not in raw:
            raw = f"memory/{raw}"
        path = (self.config.paths.root / raw).resolve()
        try:
            path.relative_to(self.config.paths.root.resolve())
        except ValueError:
            return None
        return path


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-platform exclusive lock on the projection lock file."""

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


__all__ = ["GENERATED_PROJECTION_MARKER", "MemoryProjection"]
