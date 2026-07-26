"""Append-only JSONL primitives with best-effort atomicity.

Single-process, append-only writers use a small file lock so that
multiple threads in the same process cannot interleave partial lines.
For cross-process we rely on append mode being atomic for small writes
on POSIX; Windows uses `msvcrt.locking` when available."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .atomic_write import atomic_write_text
from .time import now_iso


def _lock(file):
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        except Exception:
            pass
    else:
        try:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass


def _unlock(file):
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except Exception:
            pass
    else:
        try:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass


@contextmanager
def _open_append(path: Path) -> Iterator:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", encoding="utf-8", newline="")
    try:
        _lock(fh)
        yield fh
    finally:
        try:
            _unlock(fh)
        finally:
            fh.close()


def append(path: Path, record: dict[str, Any], *, stamp: bool = True) -> dict[str, Any]:
    if stamp and "ts" not in record:
        record = {"ts": now_iso(), **record}
    line = json.dumps(record, default=str, ensure_ascii=False)
    with _open_append(Path(path)) as fh:
        fh.write(line + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            pass
    return record


def append_many(path: Path, records: Iterable[dict[str, Any]], *, stamp: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_append(path) as fh:
        for record in records:
            if stamp and "ts" not in record:
                record = {"ts": now_iso(), **record}
            out.append(record)
            fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            pass
    return out


def read_all(path: Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_all(path: Path, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically replace a JSONL file with ``records``."""

    rows = list(records)
    text = "".join(
        json.dumps(row, default=str, ensure_ascii=False) + "\n"
        for row in rows
    )
    atomic_write_text(Path(path), text)
    return rows


def tail(path: Path, n: int = 100) -> list[dict[str, Any]]:
    records = read_all(path)
    return records[-n:]
