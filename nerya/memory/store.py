"""Transactional SQLite storage for canonical long-term memory."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..db.sqlite import connect


_WORD_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")


class MemoryScopeError(ValueError):
    """Raised when a caller asks for a memory partition it does not own."""


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    actor_id: str
    writer_id: str
    scope: str
    scope_id: str
    strategy_id: str
    session_id: str
    category: str
    stable_key: str
    title: str
    content: str
    tags: tuple[str, ...]
    source_ref: str
    source_turn_id: str
    evidence_refs: tuple[str, ...]
    confidence: float
    importance: float
    status: str
    retention_days: int
    created_at: float
    updated_at: float
    expires_at: float | None
    target_files: tuple[str, ...] = field(default_factory=tuple)
    score: float = 0.0


@dataclass(frozen=True)
class StoreWriteResult:
    record: MemoryRecord
    created: bool
    skip_reason: str = ""


@dataclass(frozen=True)
class StoreRecallResult:
    records: tuple[MemoryRecord, ...]
    expired_count: int = 0


@dataclass(frozen=True)
class StoreForgetResult:
    records: tuple[MemoryRecord, ...]

    @property
    def count(self) -> int:
        return len(self.records)


def _json_list(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item or "").strip())


def _normalise_list(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value).strip().lower()
            for value in (values or ())
            if str(value or "").strip()
        )
    )


def _terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = {match.group(0) for match in _WORD_RE.finditer(lowered)}
    for match in _CJK_RE.finditer(lowered):
        chunk = match.group(0)
        if len(chunk) == 1:
            terms.add(chunk)
        else:
            terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return terms


def _content_hash(category: str, content: str, stable_key: str) -> str:
    raw = f"{category}::{stable_key}::{content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class MemoryStore:
    """Small repository with strict ACL filtering and transactional writes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        with connect(self.db_path):
            pass

    def remember(
        self,
        *,
        actor_id: str,
        writer_id: str,
        scope: str,
        scope_id: str,
        strategy_id: str,
        session_id: str,
        category: str,
        content: str,
        stable_key: str = "",
        title: str = "",
        tags: Iterable[str] | None = None,
        source_ref: str = "",
        source_turn_id: str = "",
        evidence_refs: Iterable[str] | None = None,
        confidence: float = 1.0,
        importance: float = 0.5,
        retention_days: int = 0,
        max_entries: int = 0,
        dedupe: str = "none",
        target_files: Iterable[str] | None = None,
    ) -> StoreWriteResult:
        now = time.time()
        memory_id = uuid.uuid4().hex
        clean_tags = _normalise_list(tags)
        clean_evidence = tuple(
            str(ref).strip() for ref in (evidence_refs or ()) if str(ref or "").strip()
        )
        clean_targets = tuple(
            str(path).strip()
            for path in (target_files or ())
            if str(path or "").strip()
        )
        digest = _content_hash(category, content, stable_key)
        expires_at = now + retention_days * 86400 if retention_days > 0 else None

        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            self._expire(con, now, actor_id=actor_id)
            if dedupe == "by_hash" or (dedupe == "by_key" and not stable_key):
                row = con.execute(
                    """
                    SELECT * FROM memory_records
                    WHERE actor_id = ? AND category = ? AND scope = ? AND scope_id = ?
                      AND content_hash = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (actor_id, category, scope, scope_id, digest),
                ).fetchone()
                if row is not None:
                    con.commit()
                    return StoreWriteResult(self._record(row), False, "duplicate_hash")

            superseded_ids: list[str] = []
            if stable_key:
                superseded_ids = [
                    str(row["memory_id"])
                    for row in con.execute(
                        """
                        SELECT memory_id FROM memory_records
                        WHERE actor_id = ? AND scope = ? AND scope_id = ?
                          AND stable_key = ? AND status = 'active'
                        """,
                        (actor_id, scope, scope_id, stable_key),
                    ).fetchall()
                ]
                con.execute(
                    """
                    UPDATE memory_records
                    SET status = 'superseded', updated_at = ?, invalidated_at = ?
                    WHERE actor_id = ? AND scope = ? AND scope_id = ?
                      AND stable_key = ? AND status = 'active'
                    """,
                    (now, now, actor_id, scope, scope_id, stable_key),
                )

            con.execute(
                """
                INSERT INTO memory_records (
                    memory_id, actor_id, writer_id, scope, scope_id,
                    strategy_id, session_id, category, stable_key, title,
                    content, content_hash, tags_json, source_ref, source_turn_id,
                    evidence_refs_json, confidence, importance, retention_days,
                    created_at, updated_at, expires_at, target_files_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    actor_id,
                    writer_id,
                    scope,
                    scope_id,
                    strategy_id,
                    session_id,
                    category,
                    stable_key,
                    title,
                    content,
                    digest,
                    json.dumps(clean_tags, ensure_ascii=False),
                    source_ref,
                    source_turn_id,
                    json.dumps(clean_evidence, ensure_ascii=False),
                    confidence,
                    importance,
                    retention_days,
                    now,
                    now,
                    expires_at,
                    json.dumps(clean_targets, ensure_ascii=False),
                ),
            )
            if superseded_ids:
                con.executemany(
                    "UPDATE memory_records SET superseded_by = ? WHERE memory_id = ?",
                    ((memory_id, prior_id) for prior_id in superseded_ids),
                )
            self._enforce_cap(
                con,
                actor_id=actor_id,
                category=category,
                scope=scope,
                scope_id=scope_id,
                max_entries=max_entries,
                now=now,
            )
            row = con.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            con.commit()
            assert row is not None
            return StoreWriteResult(self._record(row), True)
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def recall(
        self,
        *,
        actor_id: str,
        query: str,
        strategy_id: str = "",
        session_id: str = "",
        scope: str = "visible",
        limit: int = 10,
    ) -> StoreRecallResult:
        con = connect(self.db_path)
        try:
            now = time.time()
            con.execute("BEGIN IMMEDIATE")
            expired = self._expire(con, now, actor_id=actor_id)
            rows = con.execute(
                """
                SELECT * FROM memory_records
                WHERE actor_id = ? AND status = 'active'
                  AND category NOT IN ('notebook_agent', 'notebook_operator')
                  AND (
                    scope = 'global'
                    OR (scope = 'strategy' AND scope_id = ? AND ? <> '')
                    OR (scope = 'session' AND scope_id = ? AND ? <> '')
                  )
                ORDER BY updated_at DESC
                """,
                (actor_id, strategy_id, strategy_id, session_id, session_id),
            ).fetchall()
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

        wanted = _terms(query)
        ranked: list[MemoryRecord] = []
        for row in rows:
            record = self._record(row)
            if scope != "visible" and record.scope != scope:
                continue
            haystack = _terms(
                f"{record.stable_key} {record.title} {record.content} {' '.join(record.tags)}"
            )
            overlap = len(wanted & haystack)
            if wanted and overlap == 0:
                continue
            age_days = max(0.0, (time.time() - record.updated_at) / 86400)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = (
                overlap * 10.0 + record.importance * 2.0 + record.confidence + recency
            )
            ranked.append(MemoryRecord(**{**record.__dict__, "score": score}))
        ranked.sort(key=lambda item: (-item.score, -item.updated_at, item.memory_id))
        return StoreRecallResult(
            records=tuple(ranked[: max(0, int(limit))]),
            expired_count=expired,
        )

    def active_by_key(
        self,
        *,
        actor_id: str,
        scope: str,
        scope_id: str,
        stable_key: str,
    ) -> MemoryRecord | None:
        if not stable_key:
            return None
        con = connect(self.db_path)
        try:
            row = con.execute(
                """
                SELECT * FROM memory_records
                WHERE actor_id = ? AND scope = ? AND scope_id = ?
                  AND stable_key = ? AND status = 'active'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (actor_id, scope, scope_id, stable_key),
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            con.close()

    def begin_legacy_import(self, *, actor_id: str, legacy_source: str) -> bool:
        """Claim one legacy source for exactly one actor until completion."""

        return legacy_source in self.begin_legacy_imports(
            actor_id=actor_id,
            legacy_sources=(legacy_source,),
        )

    def begin_legacy_imports(
        self,
        *,
        actor_id: str,
        legacy_sources: Iterable[str],
    ) -> set[str]:
        sources = tuple(
            dict.fromkeys(
                str(source or "").strip()
                for source in legacy_sources
                if str(source or "").strip()
            )
        )
        if not sources:
            return set()

        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            con.executemany(
                """
                INSERT OR IGNORE INTO memory_legacy_imports (
                    legacy_source, actor_id, claimed_at
                ) VALUES (?, ?, ?)
                """,
                ((source, actor_id, now) for source in sources),
            )
            placeholders = ",".join("?" for _ in sources)
            rows = con.execute(
                "SELECT legacy_source FROM memory_legacy_imports "
                f"WHERE legacy_source IN ({placeholders}) "
                "AND actor_id = ? AND completed_at IS NULL",
                (*sources, actor_id),
            ).fetchall()
            con.commit()
            return {str(row["legacy_source"]) for row in rows}
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def complete_legacy_import(self, *, actor_id: str, legacy_source: str) -> None:
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                UPDATE memory_legacy_imports
                SET completed_at = ?
                WHERE legacy_source = ? AND actor_id = ?
                """,
                (time.time(), legacy_source, actor_id),
            )
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def import_legacy_record(
        self,
        *,
        actor_id: str,
        scope: str,
        scope_id: str,
        strategy_id: str,
        category: str,
        content: str,
        stable_key: str,
        title: str,
        tags: Iterable[str] | None,
        source_turn_id: str,
        target_file: str,
        created_at: float,
        legacy_source: str,
        legacy_ref: str,
    ) -> bool:
        """Import one pre-SQLite fact exactly once."""

        now = time.time()
        stamp = created_at if created_at > 0 else now
        digest = _content_hash(category, content, stable_key)
        memory_id = uuid.uuid4().hex
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            imported = con.execute(
                """
                SELECT memory_id FROM memory_import_sources
                WHERE actor_id = ? AND legacy_source = ? AND legacy_ref = ?
                """,
                (actor_id, legacy_source, legacy_ref),
            ).fetchone()
            if imported is not None:
                con.commit()
                return False

            existing = None
            if stable_key:
                existing = con.execute(
                    """
                    SELECT memory_id FROM memory_records
                    WHERE actor_id = ? AND scope = ? AND scope_id = ?
                      AND stable_key = ?
                    ORDER BY CASE status WHEN 'forgotten' THEN 0 ELSE 1 END,
                             updated_at DESC
                    LIMIT 1
                    """,
                    (actor_id, scope, scope_id, stable_key),
                ).fetchone()
            if existing is None:
                existing = con.execute(
                    """
                    SELECT memory_id FROM memory_records
                    WHERE actor_id = ? AND scope = ? AND scope_id = ?
                      AND content = ? AND status = 'active'
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (actor_id, scope, scope_id, content),
                ).fetchone()
            if existing is not None:
                con.execute(
                    """
                    INSERT INTO memory_import_sources (
                        actor_id, legacy_source, legacy_ref, memory_id, imported_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        actor_id,
                        legacy_source,
                        legacy_ref,
                        str(existing["memory_id"]),
                        now,
                    ),
                )
                con.commit()
                return False

            cur = con.execute(
                """
                INSERT OR IGNORE INTO memory_records (
                    memory_id, actor_id, writer_id, scope, scope_id,
                    strategy_id, session_id, category, stable_key, title, content,
                    content_hash, tags_json, source_turn_id, confidence,
                    importance, created_at, updated_at, target_files_json,
                    legacy_source, legacy_ref
                ) VALUES (?, ?, 'legacy_import', ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?,
                          0.8, 0.5, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    actor_id,
                    scope,
                    scope_id,
                    strategy_id,
                    category,
                    stable_key,
                    title,
                    content,
                    digest,
                    json.dumps(_normalise_list(tags), ensure_ascii=False),
                    source_turn_id,
                    stamp,
                    stamp,
                    json.dumps([target_file] if target_file else []),
                    legacy_source,
                    legacy_ref,
                ),
            )
            target_id = memory_id
            if cur.rowcount <= 0:
                row = con.execute(
                    """
                    SELECT memory_id FROM memory_records
                    WHERE actor_id = ? AND scope = ? AND scope_id = ?
                      AND ((stable_key <> '' AND stable_key = ?) OR content = ?)
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (actor_id, scope, scope_id, stable_key, content),
                ).fetchone()
                if row is None:
                    con.rollback()
                    return False
                target_id = str(row["memory_id"])
            con.execute(
                """
                INSERT INTO memory_import_sources (
                    actor_id, legacy_source, legacy_ref, memory_id, imported_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (actor_id, legacy_source, legacy_ref, target_id, now),
            )
            con.commit()
            return cur.rowcount > 0
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def forget(
        self,
        *,
        actor_id: str,
        scope: str,
        scope_id: str,
        stable_key: str = "",
        memory_id: str = "",
    ) -> StoreForgetResult:
        """Scrub a scoped id or every version of a key, retaining tombstones."""

        stable_key = str(stable_key or "").strip()
        memory_id = str(memory_id or "").strip()
        if not stable_key and not memory_id:
            return StoreForgetResult(())
        con = connect(self.db_path)
        now = time.time()
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """
                SELECT * FROM memory_records
                WHERE actor_id = ? AND scope = ? AND scope_id = ?
                  AND status <> 'forgotten'
                  AND (
                    (? <> '' AND stable_key = ?)
                    OR (? <> '' AND memory_id = ?)
                  )
                ORDER BY created_at, memory_id
                """,
                (
                    actor_id,
                    scope,
                    scope_id,
                    stable_key,
                    stable_key,
                    memory_id,
                    memory_id,
                ),
            ).fetchall()
            if not rows:
                con.commit()
                return StoreForgetResult(())
            ids = [str(row["memory_id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            con.execute(
                """
                UPDATE memory_records
                SET status = 'forgotten', title = '', content = '',
                    content_hash = '', tags_json = '[]', source_ref = '',
                    source_turn_id = '', evidence_refs_json = '[]',
                    meta_json = '{}', updated_at = ?, invalidated_at = ?
                WHERE memory_id IN ("""
                + placeholders
                + ")",
                (now, now, *ids),
            )
            con.commit()
            return StoreForgetResult(tuple(self._record(row) for row in rows))
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def forget_candidates(
        self,
        *,
        actor_id: str,
        scope: str,
        scope_id: str,
        stable_key: str = "",
        memory_id: str = "",
    ) -> list[MemoryRecord]:
        stable_key = str(stable_key or "").strip()
        memory_id = str(memory_id or "").strip()
        if not stable_key and not memory_id:
            return []
        con = connect(self.db_path)
        try:
            rows = con.execute(
                """
                SELECT * FROM memory_records
                WHERE actor_id = ? AND scope = ? AND scope_id = ?
                  AND status <> 'forgotten'
                  AND (
                    (? <> '' AND stable_key = ?)
                    OR (? <> '' AND memory_id = ?)
                  )
                ORDER BY created_at, memory_id
                """,
                (
                    actor_id,
                    scope,
                    scope_id,
                    stable_key,
                    stable_key,
                    memory_id,
                    memory_id,
                ),
            ).fetchall()
            return [self._record(row) for row in rows]
        finally:
            con.close()

    def projection_records(self, *, actor_id: str) -> list[MemoryRecord]:
        """Return a consistent all-status snapshot for derived projections."""

        con = connect(self.db_path)
        try:
            con.execute("BEGIN")
            rows = con.execute(
                """
                SELECT * FROM memory_records
                WHERE actor_id = ?
                ORDER BY created_at, memory_id
                """,
                (actor_id,),
            ).fetchall()
            con.commit()
            return [self._record(row) for row in rows]
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _expire(
        con: sqlite3.Connection,
        now: float,
        *,
        actor_id: str | None = None,
    ) -> int:
        if actor_id is None:
            cur = con.execute(
                """
                UPDATE memory_records
                SET status = 'expired', updated_at = ?, invalidated_at = ?
                WHERE status = 'active' AND pinned = 0
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, now, now),
            )
        else:
            cur = con.execute(
                """
                UPDATE memory_records
                SET status = 'expired', updated_at = ?, invalidated_at = ?
                WHERE actor_id = ? AND status = 'active' AND pinned = 0
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, now, actor_id, now),
            )
        return max(0, int(cur.rowcount))

    def maintain(self, *, actor_id: str) -> int:
        """Expire due records transactionally and return the changed count."""

        con = connect(self.db_path)
        now = time.time()
        try:
            con.execute("BEGIN IMMEDIATE")
            expired = self._expire(con, now, actor_id=actor_id)
            con.commit()
            return expired
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _enforce_cap(
        con: sqlite3.Connection,
        *,
        actor_id: str,
        category: str,
        scope: str,
        scope_id: str,
        max_entries: int,
        now: float,
    ) -> None:
        if max_entries <= 0:
            return
        con.execute(
            """
            UPDATE memory_records
            SET status = 'expired', updated_at = ?, invalidated_at = ?
            WHERE memory_id IN (
                SELECT memory_id FROM memory_records
                WHERE actor_id = ? AND category = ? AND scope = ? AND scope_id = ?
                  AND status = 'active' AND pinned = 0
                ORDER BY created_at DESC, memory_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (now, now, actor_id, category, scope, scope_id, max_entries),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            actor_id=str(row["actor_id"]),
            writer_id=str(row["writer_id"]),
            scope=str(row["scope"]),
            scope_id=str(row["scope_id"]),
            strategy_id=str(row["strategy_id"]),
            session_id=str(row["session_id"]),
            category=str(row["category"]),
            stable_key=str(row["stable_key"]),
            title=str(row["title"]),
            content=str(row["content"]),
            tags=_json_list(str(row["tags_json"])),
            source_ref=str(row["source_ref"]),
            source_turn_id=str(row["source_turn_id"]),
            evidence_refs=_json_list(str(row["evidence_refs_json"])),
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            status=str(row["status"]),
            retention_days=int(row["retention_days"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"])
            if row["expires_at"] is not None
            else None,
            target_files=_json_list(str(row["target_files_json"])),
        )


__all__ = [
    "MemoryRecord",
    "MemoryScopeError",
    "MemoryStore",
    "StoreWriteResult",
]
