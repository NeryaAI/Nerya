"""Thin sqlite wrapper with pragma setup."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path


def _journal_mode(con: sqlite3.Connection) -> str:
    row = con.execute("PRAGMA journal_mode").fetchone()
    return str(row[0] if row else "").strip().lower()


def _ensure_wal_mode(con: sqlite3.Connection) -> None:
    """Enable WAL only when the database is not already in WAL mode.

    ``journal_mode=WAL`` is a database-level mode transition, not ordinary
    per-connection setup. Reissuing the transition for every short-lived
    repository connection can race with another process opening/recovering the
    WAL files and has produced transient ``disk I/O error`` failures on an
    otherwise healthy database. Read the current mode first and avoid that
    write entirely once WAL is established.

    If another connection completes the transition while ours is attempting
    it, an OperationalError is safe to accept only when a fresh read proves the
    database is now in WAL mode. Genuine storage errors remain visible.
    """
    if _journal_mode(con) == "wal":
        return
    for attempt in range(8):
        try:
            row = con.execute("PRAGMA journal_mode=WAL").fetchone()
            if str(row[0] if row else "").strip().lower() == "wal":
                return
            raise sqlite3.OperationalError("failed to enable WAL journal mode")
        except sqlite3.OperationalError as exc:
            # A concurrent connection may have won the WAL transition after
            # our initial read. Verify state before classifying the failure.
            try:
                if _journal_mode(con) == "wal":
                    return
            except sqlite3.Error:
                pass
            message = str(exc).lower()
            if not any(token in message for token in ("locked", "busy")) or attempt == 7:
                raise
            time.sleep(0.025 * (attempt + 1))


def connect(path: Path) -> sqlite3.Connection:
    # Operator-only escape hatch for hosts with unreliable WAL shared memory.
    # Never silently switch modes after an I/O error; an offline transition is
    # required for an existing database and all writers must share this setting.
    journal_mode = os.environ.get("NERYA_SQLITE_JOURNAL_MODE", "wal").strip().lower()
    if journal_mode not in {"wal", "delete"}:
        raise ValueError("NERYA_SQLITE_JOURNAL_MODE must be wal or delete")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        if journal_mode == "wal":
            _ensure_wal_mode(con)
        elif _journal_mode(con) != "delete":
            raise sqlite3.OperationalError("rollback journal mode requires an offline database transition")
        con.execute("PRAGMA synchronous=" + ("FULL" if journal_mode == "delete" else "NORMAL"))
        con.execute("PRAGMA foreign_keys=ON")
        con.row_factory = sqlite3.Row
        from .migrations import apply_migrations
        apply_migrations(con)
        return con
    except BaseException:
        con.close()
        raise


def connect_preview(path: Path) -> sqlite3.Connection:
    """Read existing state, or an empty schema in memory without creating files."""
    if Path(path).exists():
        return connect_readonly(path)
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    from .migrations import apply_migrations
    apply_migrations(con)
    con.execute("PRAGMA query_only=ON")
    return con


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a WAL-aware query-only connection without journal negotiation."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        return con
    except BaseException:
        con.close()
        raise
