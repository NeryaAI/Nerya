import json
import sqlite3
from threading import Event, Thread

import pytest

from nerya.agent.command_store import CommandStore
from nerya.core.paths import WorkspacePaths


@pytest.mark.parametrize("cid", [None, "journal-command"])
def test_snapshot_reads_existing_wal_without_journal_transition(tmp_path, monkeypatch, cid):
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "wal")
    paths = WorkspacePaths(tmp_path)
    store = CommandStore(paths, "journal-test")
    store.accept(
        cid="journal-command",
        sid="journal-session",
        actor="operator",
        kind="send",
        request={"payload": {"text": "inspect state"}},
        context={},
        turn_id="journal-turn",
    )

    # A polling reader must use the existing mode, not the writer's setting.
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    snapshot = store.snapshot("journal-session", cid)
    command = snapshot["command"] if cid else snapshot["commands"][0]
    assert command["command_id"] == "journal-command"
    assert command["state"] == "queued"

    with sqlite3.connect(str(paths.db)) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(sqlite3.OperationalError, match="offline database transition"):
        with store.transaction():
            pass


def test_snapshot_initializes_missing_database_and_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    paths = WorkspacePaths(tmp_path)
    snapshot = CommandStore(paths, "new-database-test").snapshot("new-session")
    assert snapshot["commands"] == []
    assert snapshot["queue"]["paused"] == 0
    with sqlite3.connect(str(paths.db)) as con:
        assert con.execute(
            "SELECT runtime_epoch FROM agent_command_queues WHERE session_id=?",
            ("new-session",),
        ).fetchone()[0] == "new-database-test"


def test_snapshot_projection_does_not_hold_delete_reader_during_slow_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    paths = WorkspacePaths(tmp_path)
    store = CommandStore(paths, "snapshot-contention-test")
    for index in range(30):
        cid = f"snapshot-command-{index:02d}"
        store.accept(
            cid=cid,
            sid="snapshot-session",
            actor="operator",
            kind="send",
            request={"payload": {"text": f"command {index}"}},
            context={},
            turn_id=f"turn-{index:02d}",
        )
        with store.transaction() as con:
            con.execute(
                "UPDATE agent_commands SET state='succeeded', result_json=? WHERE command_id=?",
                (json.dumps({"final_text": "x" * 1_000_000}), cid),
            )

    projection_started = Event()
    release_projection = Event()
    original_projection = store._projection

    def slow_projection(*args, **kwargs):
        projection_started.set()
        assert release_projection.wait(5)
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(store, "_projection", slow_projection)
    failure = []

    def read_snapshot():
        try:
            store.snapshot("snapshot-session")
        except BaseException as exc:  # pragma: no cover - reported below
            failure.append(exc)

    reader = Thread(target=read_snapshot)
    reader.start()
    assert projection_started.wait(5)

    writer = sqlite3.connect(str(paths.db), timeout=0, isolation_level=None)
    try:
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE agent_command_queues SET lease_until=lease_until+1 WHERE session_id=?",
            ("snapshot-session",),
        )
        writer.commit()
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
        release_projection.set()
        reader.join(5)

    assert not failure
    assert not reader.is_alive()


def test_snapshot_keeps_user_projection_and_full_command_result(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    paths = WorkspacePaths(tmp_path)
    store = CommandStore(paths, "snapshot-projection-test")
    receipt = store.accept(
        cid="user-command",
        sid="projection-session",
        actor="operator",
        kind="send",
        request={"payload": {"text": "original text"}},
        context={},
        turn_id="projection-turn",
    )
    with store.transaction() as con:
        con.execute(
            "INSERT INTO agent_messages(message_id,session_id,turn_id,role,content,ts) VALUES (?,?,?,?,?,?)",
            ("projection-turn:user", "projection-session", "projection-turn", "user", "saved user text", 1),
        )
        con.execute(
            "UPDATE agent_commands SET state='succeeded',result_json=? WHERE command_id=?",
            (json.dumps({"final_text": "full result"}), receipt["command"]["command_id"]),
        )

    listed = store.snapshot("projection-session")["commands"][0]
    assert listed["input"] == "saved user text"
    assert listed["has_result"] is True
    assert "result" not in listed

    full = store.snapshot("projection-session", "user-command")["command"]
    assert full["input"] == "saved user text"
    assert full["result"] == {"final_text": "full result"}

    with store.transaction() as con:
        con.execute(
            "UPDATE agent_messages SET deleted=1 WHERE message_id=?",
            ("projection-turn:user",),
        )
    deleted = store.snapshot("projection-session", "user-command")["command"]
    assert deleted["show_user"] is False
