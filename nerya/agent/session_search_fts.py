"""SQLite/FTS5-backed mirror of the session journals.

The pure-Python ``session_search`` scan is fine for a few thousand
journal rows but the runtime ships an FTS5 index so operators can grep
years of conversation in milliseconds. explicitly calls
for "session search backed by SQLite/FTS or equivalent". This module
provides the equivalent without forcing it on small workspaces:

- :class:`FTSIndex` is opt-in. The journal scan stays the source of
  truth; FTS is *only* a query accelerator on top of the same JSONL
  files.
- :func:`maybe_open_index` returns ``None`` when SQLite was built
  without FTS5 (rare on Python 3.13+, but still possible on minimal
  Linux distros) so callers transparently fall back to the substring
  scan in :mod:`nerya.agent.session_search`.
- The DB schema ships a single FTS virtual table plus a
  ``journal_files`` ledger that tracks file mtime + size + the highest
  byte offset already ingested. ``ensure_fresh()`` re-ingests only the
  rows appended since the last sync; a journal that was rewritten
  (mtime older than recorded *and* size shrank) gets fully rebuilt.
- ``search()`` mirrors :func:`session_search.search` exactly: same
  return shape, same filters, same newest-first ordering. The only
  behavioural difference is that bracketed regex queries (``/foo/``)
  fall through to the substring scan because FTS5 does not support
  regex. Plain text and prefix queries (``foo*``) use FTS5.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


# Journals that the agent kernel writes to and we want indexed.
DEFAULT_JOURNALS = (
    "turn_steps",
    "agent_decisions",
    "skills",
    "messages",
)


_SCHEMA_DDL = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS event_index USING fts5(
        journal UNINDEXED,
        ts UNINDEXED,
        kind,
        session_id,
        strategy_id,
        turn_id,
        text,
        prefix='2 3 4 5'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_payloads (
        rowid INTEGER PRIMARY KEY,
        journal TEXT NOT NULL,
        ts TEXT,
        kind TEXT,
        session_id TEXT,
        strategy_id TEXT,
        turn_id TEXT,
        payload_json TEXT NOT NULL,
        text_blob TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS event_payloads_journal_ts
        ON event_payloads (journal, ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS event_payloads_session
        ON event_payloads (session_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS event_payloads_strategy
        ON event_payloads (strategy_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS journal_files (
        journal TEXT PRIMARY KEY,
        size INTEGER NOT NULL DEFAULT 0,
        mtime REAL NOT NULL DEFAULT 0,
        last_offset INTEGER NOT NULL DEFAULT 0,
        last_synced TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

_SCHEMA_VERSION = "1"


def _supports_fts5() -> bool:
    """Return ``True`` when the running SQLite ships FTS5."""

    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE _probe USING fts5(c)")
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return True


@dataclass
class FTSIndexStats:
    """Snapshot of the index for diagnostics / tests."""

    rows: int
    journals: dict[str, dict[str, Any]]


class FTSIndex:
    """Open / build / query an FTS5 mirror of the journal files."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self._db_path))
        self._init_schema()

    # ---- lifecycle --------------------------------------------------

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "FTSIndex":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ---- schema -----------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._con.cursor()
        for stmt in _SCHEMA_DDL:
            cur.execute(stmt)
        cur.execute(
            "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('version', ?)",
            (_SCHEMA_VERSION,),
        )
        self._con.commit()

    # ---- ingestion --------------------------------------------------

    def _row_event(self, journal: str, raw: str) -> tuple | None:
        """Convert one raw JSONL line into a row tuple ready for insert."""

        line = raw.strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return (
            journal,
            str(payload.get("ts") or payload.get("timestamp") or ""),
            str(payload.get("kind") or payload.get("event") or journal),
            payload.get("session_id"),
            payload.get("strategy_id"),
            payload.get("turn_id") or payload.get("agent_turn_id"),
            payload,
            line,
        )

    def _wipe_journal(self, journal: str) -> None:
        cur = self._con.cursor()
        cur.execute(
            "DELETE FROM event_payloads WHERE journal = ?", (journal,),
        )
        cur.execute(
            "DELETE FROM event_index WHERE journal = ?", (journal,),
        )
        cur.execute(
            "DELETE FROM journal_files WHERE journal = ?", (journal,),
        )

    def ensure_fresh(self, paths, journals: Iterable[str] = DEFAULT_JOURNALS) -> None:
        """Bring the index in sync with the on-disk JSONL files.

        - New rows appended past ``last_offset`` are streamed in.
        - A journal whose size *shrank* (e.g. operator pruned the
          file) gets fully rebuilt — easier than diffing.
        - Missing files leave a zero-row entry; they get ingested
          when they next appear.
        """

        cur = self._con.cursor()
        for journal in journals:
            path = paths.journal(journal)
            cur.execute(
                "SELECT size, mtime, last_offset FROM journal_files WHERE journal = ?",
                (journal,),
            )
            row = cur.fetchone()
            if not path.exists():
                if row is not None:
                    self._wipe_journal(journal)
                    self._con.commit()
                continue

            try:
                stat = path.stat()
            except OSError:
                continue

            current_size = int(stat.st_size)
            current_mtime = float(stat.st_mtime)
            prev_size = int(row[0]) if row else 0
            prev_offset = int(row[2]) if row else 0

            if row is not None and current_size < prev_size:
                # File shrank → rebuild fully.
                self._wipe_journal(journal)
                prev_offset = 0

            if prev_offset >= current_size and row is not None:
                # No new bytes since last sync, but mtime/size may
                # have refreshed (atomic rewrite same content).
                cur.execute(
                    """
                    UPDATE journal_files
                       SET size = ?, mtime = ?, last_synced = datetime('now')
                     WHERE journal = ?
                    """,
                    (current_size, current_mtime, journal),
                )
                continue

            try:
                with path.open("rb") as fh:
                    fh.seek(prev_offset)
                    raw_bytes = fh.read()
            except OSError:
                continue

            complete_bytes = raw_bytes
            if raw_bytes and not raw_bytes.endswith((b"\n", b"\r")):
                newline = max(raw_bytes.rfind(b"\n"), raw_bytes.rfind(b"\r"))
                complete_bytes = raw_bytes[: newline + 1] if newline >= 0 else b""
            buffer = complete_bytes.decode("utf-8", errors="replace")

            for line in buffer.splitlines():
                ev = self._row_event(journal, line)
                if ev is None:
                    continue
                cur.execute(
                    """
                    INSERT INTO event_payloads
                        (journal, ts, kind, session_id, strategy_id, turn_id, payload_json, text_blob)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ev[0], ev[1], ev[2], ev[3], ev[4], ev[5],
                        json.dumps(ev[6], default=str), ev[7],
                    ),
                )
                rowid = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO event_index
                        (rowid, journal, ts, kind, session_id, strategy_id, turn_id, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rowid, ev[0], ev[1], ev[2], ev[3] or "",
                        ev[4] or "", ev[5] or "", ev[7],
                    ),
                )
            cur.execute(
                """
                INSERT INTO journal_files(journal, size, mtime, last_offset, last_synced)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(journal) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    last_offset = excluded.last_offset,
                    last_synced = excluded.last_synced
                """,
                (
                    journal,
                    current_size,
                    current_mtime,
                    prev_offset + len(complete_bytes),
                ),
            )
        self._con.commit()

    # ---- query ------------------------------------------------------

    def stats(self) -> FTSIndexStats:
        cur = self._con.cursor()
        cur.execute("SELECT COUNT(*) FROM event_payloads")
        total = int(cur.fetchone()[0])
        cur.execute("SELECT journal, size, mtime, last_offset FROM journal_files")
        journals: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            journals[row[0]] = {
                "size": int(row[1]),
                "mtime": float(row[2]),
                "last_offset": int(row[3]),
            }
        return FTSIndexStats(rows=total, journals=journals)

    @staticmethod
    def _is_regex_query(query: str) -> bool:
        q = query.strip()
        return q.startswith("/") and q.endswith("/") and len(q) > 2

    @staticmethod
    def _normalise_match(query: str) -> str:
        """Make a user-supplied query safe + useful for FTS5 MATCH.

        - Strips quotes and slashes (regex sentinels are caller-owned).
        - Wraps the whole thing in double quotes so reserved tokens
          like ``OR`` / ``AND`` don't accidentally activate boolean
          mode.
        - Preserves trailing ``*`` for prefix search when the caller
          already specified one.
        """

        stripped = query.strip()
        if stripped.endswith("*"):
            core = stripped[:-1].strip()
            if not core:
                return '"' + stripped + '"'
            return '"' + core.replace('"', "") + '" *'
        return '"' + stripped.replace('"', "") + '"'

    def search(
        self,
        query: str,
        *,
        journals: Iterable[str] = DEFAULT_JOURNALS,
        session_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Run an FTS5 query and project results into the same shape
        that :func:`nerya.agent.session_search.search` returns."""

        raw = (query or "").strip()
        if not raw or self._is_regex_query(raw):
            return []

        match = self._normalise_match(raw)
        journal_tuple = tuple(journals) or DEFAULT_JOURNALS
        placeholders = ",".join("?" * len(journal_tuple))
        sql_parts = [
            "SELECT p.journal, p.ts, p.kind, p.session_id, p.strategy_id,",
            "       p.turn_id, p.payload_json, p.text_blob, p.rowid",
            "  FROM event_index AS i",
            "  JOIN event_payloads AS p ON p.rowid = i.rowid",
            f" WHERE i.journal IN ({placeholders})",
            "   AND event_index MATCH ?",
        ]
        params: list[Any] = list(journal_tuple) + [match]
        if session_id:
            sql_parts.append("   AND p.session_id = ?")
            params.append(session_id)
        if strategy_id:
            sql_parts.append("   AND p.strategy_id = ?")
            params.append(strategy_id)
        sql_parts.append(" ORDER BY p.rowid DESC")
        sql_parts.append(" LIMIT ?")
        params.append(int(limit))

        try:
            cur = self._con.cursor()
            cur.execute(" ".join(sql_parts), params)
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            # Bad query syntax: caller falls back to substring scan.
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            journal, ts, kind, sess, strat, turn_id, payload_json, text_blob, _rowid = row
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except json.JSONDecodeError:
                payload = {}
            out.append({
                "journal": journal,
                "turn_id": turn_id,
                "session_id": sess,
                "strategy_id": strat,
                "kind": kind,
                "ts": ts,
                "preview": (text_blob or "")[:240],
                "payload": payload,
            })
        return out


# ---- module helpers ----------------------------------------------------


_FTS5_AVAILABLE: Optional[bool] = None


def is_supported() -> bool:
    """Memoised feature probe: is FTS5 available in this interpreter?"""

    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is None:
        _FTS5_AVAILABLE = _supports_fts5()
    return bool(_FTS5_AVAILABLE)


def maybe_open_index(paths) -> Optional[FTSIndex]:
    """Open the per-workspace FTS index when supported.

    Returns ``None`` when FTS5 is unavailable so callers can fall
    back to the substring scan without raising.
    """

    if not is_supported():
        return None
    db_path = paths.journals / "session_index.db"
    try:
        return FTSIndex(db_path)
    except sqlite3.DatabaseError:
        return None


__all__ = [
    "DEFAULT_JOURNALS",
    "FTSIndex",
    "FTSIndexStats",
    "is_supported",
    "maybe_open_index",
]
