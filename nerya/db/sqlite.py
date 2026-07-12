"""Thin sqlite wrapper with pragma setup."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        for attempt in range(8):
            try:
                con.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.row_factory = sqlite3.Row
        from .migrations import apply_migrations
        apply_migrations(con)
        return con
    except BaseException:
        con.close()
        raise
