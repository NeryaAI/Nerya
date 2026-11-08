"""Operator-selected rollback journaling, never an automatic error fallback."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

import pytest

from nerya.db.sqlite import connect, connect_readonly


def test_explicit_rollback_mode_persists_and_serializes_writers(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    path = tmp_path / "runtime.db"
    with closing(connect(path)) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 2
        con.execute("CREATE TABLE probe(value INTEGER PRIMARY KEY)")

    def write(value):
        with closing(connect(path)) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO probe VALUES (?)", (value,))
            con.commit()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(36)))
    with closing(connect_readonly(path)) as con:
        assert con.execute("SELECT count(*) FROM probe").fetchone()[0] == 36
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM probe")
    assert not path.with_name(path.name + "-shm").exists()
    assert not path.with_name(path.name + "-wal").exists()


def test_default_stays_wal(tmp_path, monkeypatch):
    monkeypatch.delenv("NERYA_SQLITE_JOURNAL_MODE", raising=False)
    with closing(connect(tmp_path / "runtime.db")) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_wal_database_requires_offline_transition(tmp_path, monkeypatch):
    monkeypatch.delenv("NERYA_SQLITE_JOURNAL_MODE", raising=False)
    path = tmp_path / "runtime.db"
    with closing(connect(path)):
        pass
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    with pytest.raises(sqlite3.OperationalError, match="offline database transition"):
        connect(path)
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("value", ["off", "memory", "unknown", "wal; PRAGMA synchronous=OFF"])
def test_unsafe_or_unknown_mode_rejected_before_creating_database(tmp_path, monkeypatch, value):
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", value)
    path = tmp_path / "runtime.db"
    with pytest.raises(ValueError, match="NERYA_SQLITE_JOURNAL_MODE"):
        connect(path)
    assert not path.exists()
