"""Session persistence invariants required by durable memory checkpoints."""

from __future__ import annotations

import json

import pytest

from nerya.db.repositories import AgentSessionRepository, SessionStrategyMismatch
from nerya.db.sqlite import connect


pytestmark = pytest.mark.smoke


def test_session_upsert_merges_meta_without_erasing_the_checkpoint(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    checkpoint = {"version": 1, "rendered": "checkpoint"}
    repo.upsert_session(
        session_id="s1",
        meta={"context_compaction": checkpoint, "owner": "operator-1"},
    )

    repo.upsert_session(session_id="s1", meta={"last_turn_id": "turn-2"})

    row = repo.get_session("s1")
    assert row is not None
    meta = json.loads(row["meta_json"])
    assert meta == {
        "context_compaction": checkpoint,
        "owner": "operator-1",
        "last_turn_id": "turn-2",
    }
    con.close()


def test_session_strategy_binding_is_immutable_after_first_assignment(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(
        session_id="s1",
        strategy_id="alpha",
        title="Alpha session",
        meta={"owner": "operator-1"},
        ts=10.0,
    )

    with pytest.raises(SessionStrategyMismatch):
        repo.upsert_session(
            session_id="s1",
            strategy_id="beta",
            title="Wrong title",
            meta={"owner": "attacker"},
            ts=20.0,
        )

    row = repo.get_session("s1")
    assert row is not None
    assert row["strategy_id"] == "alpha"
    assert row["title"] == "Alpha session"
    assert row["updated_at"] == 10.0
    assert json.loads(row["meta_json"]) == {"owner": "operator-1"}
    con.close()


def test_editing_a_message_invalidates_the_stored_checkpoint(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1")
    repo.record_message(
        message_id="m1",
        session_id="s1",
        turn_id="t1",
        role="user",
        content="old fact",
    )
    repo.update_session_meta(
        "s1",
        {"context_compaction": {"version": 1, "rendered": "old checkpoint"}},
    )
    before = repo.get_session("s1")
    assert before is not None
    assert before["compaction_epoch"] == 0

    assert repo.update_message_content(
        session_id="s1",
        message_id="m1",
        content="corrected fact",
    )

    after = repo.get_session("s1")
    assert after is not None
    assert after["compaction_epoch"] == 1
    assert "context_compaction" not in json.loads(after["meta_json"])
    con.close()


def test_stale_checkpoint_compare_and_swap_cannot_restore_old_facts(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1")
    repo.record_message(
        message_id="m1",
        session_id="s1",
        role="user",
        content="old fact",
    )
    assert repo.update_context_checkpoint(
        "s1",
        {"version": 2, "rendered": "first checkpoint"},
        expected_epoch=0,
    )
    assert repo.update_message_content(
        session_id="s1",
        message_id="m1",
        content="corrected fact",
    )

    assert not repo.update_context_checkpoint(
        "s1",
        {"version": 2, "rendered": "stale checkpoint"},
        expected_epoch=0,
    )
    row = repo.get_session("s1")
    assert row is not None
    assert row["compaction_epoch"] == 2
    assert "context_compaction" not in json.loads(row["meta_json"])
    con.close()


def test_checkpoint_compare_and_swap_allows_only_one_writer_per_epoch(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1")

    assert repo.update_context_checkpoint(
        "s1",
        {"version": 2, "rendered": "newer checkpoint"},
        expected_epoch=0,
    )
    assert not repo.update_context_checkpoint(
        "s1",
        {"version": 2, "rendered": "stale concurrent checkpoint"},
        expected_epoch=0,
    )

    row = repo.get_session("s1")
    assert row is not None
    assert row["compaction_epoch"] == 1
    checkpoint = json.loads(row["meta_json"])["context_compaction"]
    assert checkpoint["rendered"] == "newer checkpoint"
    assert checkpoint["compaction_epoch"] == 1
    con.close()


def test_small_incremental_window_keeps_the_existing_checkpoint():
    from nerya.agent.session_compaction import (
        CHECKPOINT_HEADER,
        SessionCompactionPolicy,
        compact_session_history,
    )

    policy = SessionCompactionPolicy(keep_recent_pairs=2, trigger_pairs=2)
    initial_rows = [
        {
            "message_id": f"m{index}",
            "turn_id": f"t{index // 2}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"initial message {index}",
            "ts": float(index),
        }
        for index in range(8)
    ]
    first = compact_session_history(initial_rows, policy=policy)
    assert first.checkpoint is not None
    incremental_rows = [
        {
            "message_id": "m8",
            "turn_id": "t4",
            "role": "user",
            "content": "new question",
            "ts": 8.0,
        },
        {
            "message_id": "m9",
            "turn_id": "t4",
            "role": "assistant",
            "content": "new answer",
            "ts": 9.0,
        },
    ]

    second = compact_session_history(
        incremental_rows,
        existing_checkpoint=first.checkpoint,
        policy=policy,
    )

    assert CHECKPOINT_HEADER in second.messages[0]["content"]
    assert [message["content"] for message in second.messages[1:]] == [
        "new question",
        "new answer",
    ]


def test_kernel_uses_the_checkpoint_cursor_after_the_first_full_read(
    tmp_path,
    monkeypatch,
):
    from nerya.agent.kernel import AgentKernel
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths

    config = Config(
        paths=WorkspacePaths(root=tmp_path),
        data={
            "agent": {
                "native": {
                    "session_autocompact_enabled": True,
                    "session_compact_keep_recent_pairs": 2,
                    "session_compact_trigger_pairs": 2,
                }
            }
        },
    )
    con = connect(config.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1")
    for index in range(10):
        repo.record_message(
            message_id=f"m{index}",
            session_id="s1",
            turn_id=f"t{index // 2}",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
            ts=float(index + 1),
        )
    con.close()

    calls: list[int] = []
    original = AgentSessionRepository.compaction_transcript

    def tracked(self, session_id: str, *, after_seq: int = 0):
        calls.append(after_seq)
        return original(self, session_id, after_seq=after_seq)

    monkeypatch.setattr(AgentSessionRepository, "compaction_transcript", tracked)
    kernel = AgentKernel(config=config, skills=None)  # type: ignore[arg-type]
    kernel._load_prior_chat_messages(session_id="s1", max_pairs=2)

    con = connect(config.paths.db)
    repo = AgentSessionRepository(con)
    for index in range(10, 12):
        repo.record_message(
            message_id=f"m{index}",
            session_id="s1",
            turn_id="t5",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
            ts=float(index + 1),
        )
    con.close()
    kernel._load_prior_chat_messages(session_id="s1", max_pairs=2)

    assert calls[0] == 0
    assert calls[1] > 0


def test_kernel_rejects_strategy_drift_before_turn_scope_is_bound(tmp_path):
    from nerya.agent.kernel import AgentKernel
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    con = connect(config.paths.db)
    AgentSessionRepository(con).upsert_session(
        session_id="s1",
        strategy_id="alpha",
    )
    con.close()
    kernel = AgentKernel(config=config, skills=None)  # type: ignore[arg-type]

    with pytest.raises(SessionStrategyMismatch):
        kernel._bind_session_strategy(
            session_id="s1",
            requested_strategy_id="beta",
        )

    assert kernel.sessions.load("s1") is None


def test_v1_checkpoint_is_rebuilt_without_carrying_stale_digest_text():
    from nerya.agent.session_compaction import (
        SessionCompactionPolicy,
        compact_session_history,
    )

    rows = [
        {
            "message_id": f"m{index}",
            "message_seq": index,
            "turn_id": f"t{index // 2}",
            "role": "user" if index % 2 else "assistant",
            "content": f"current message {index}",
            "ts": float(index),
        }
        for index in range(1, 9)
    ]
    checkpoint_v1 = {
        "version": 1,
        "rendered": "deleted secret from an edited message",
        "digest": {"user_requests": ["deleted secret from an edited message"]},
        "last_compacted_message_id": "m1",
    }

    compacted = compact_session_history(
        rows,
        existing_checkpoint=checkpoint_v1,
        policy=SessionCompactionPolicy(keep_recent_pairs=1, trigger_pairs=1),
        compaction_epoch=3,
    )

    rendered = "\n".join(str(item["content"]) for item in compacted.messages)
    assert "deleted secret" not in rendered
    assert compacted.checkpoint is not None
    assert compacted.checkpoint["version"] == 2


def test_v8_migration_removes_only_v1_checkpoints(tmp_path):
    from nerya.db.migrations import _v8_session_compaction_epoch

    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(
        session_id="legacy",
        meta={
            "context_compaction": {
                "version": 1,
                "rendered": "stale checkpoint",
            },
            "owner": "operator-1",
        },
    )
    repo.upsert_session(
        session_id="current",
        meta={
            "context_compaction": {
                "version": 2,
                "rendered": "current checkpoint",
            },
            "owner": "operator-2",
        },
    )

    _v8_session_compaction_epoch(con)

    legacy = repo.get_session("legacy")
    current = repo.get_session("current")
    assert legacy is not None
    assert json.loads(legacy["meta_json"]) == {"owner": "operator-1"}
    assert current is not None
    assert json.loads(current["meta_json"]) == {
        "context_compaction": {
            "version": 2,
            "rendered": "current checkpoint",
        },
        "owner": "operator-2",
    }
    con.close()


def test_unchanged_checkpoint_is_still_validated_with_epoch_cas(
    tmp_path,
    monkeypatch,
):
    from nerya.agent.kernel import AgentKernel
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    con = connect(config.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1")
    for index in range(12):
        repo.record_message(
            message_id=f"m{index}",
            session_id="s1",
            turn_id=f"t{index // 2}",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
            ts=float(index + 1),
        )
    con.close()
    kernel = AgentKernel(config=config, skills=None)  # type: ignore[arg-type]
    kernel._load_prior_chat_messages(session_id="s1", max_pairs=2)

    calls: list[int] = []
    original = AgentSessionRepository.update_context_checkpoint

    def tracked(self, session_id, checkpoint, *, expected_epoch):  # noqa: ANN001
        calls.append(expected_epoch)
        return original(
            self,
            session_id,
            checkpoint,
            expected_epoch=expected_epoch,
        )

    monkeypatch.setattr(
        AgentSessionRepository,
        "update_context_checkpoint",
        tracked,
    )

    kernel._load_prior_chat_messages(session_id="s1", max_pairs=2)

    assert calls == [1]


def test_file_session_store_rejects_strategy_drift_without_mutating_state(tmp_path):
    from nerya.agent.session import SessionStore

    store = SessionStore(tmp_path)
    store.ensure("s1", strategy_id="alpha")
    path = tmp_path / "sessions" / "s1.json"
    before = path.read_bytes()

    with pytest.raises(SessionStrategyMismatch):
        store.ensure("s1", strategy_id="beta")
    with pytest.raises(SessionStrategyMismatch):
        store.append_turn("s1", "t1", strategy_id="beta")
    with pytest.raises(SessionStrategyMismatch):
        store.update_meta("s1", {"owner": "beta"}, strategy_id="beta")

    assert path.read_bytes() == before


def test_db_only_session_exists_before_the_first_resume_bind(tmp_path):
    from nerya.agent.kernel import AgentKernel
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths

    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    con = connect(config.paths.db)
    AgentSessionRepository(con).upsert_session(session_id="s1")
    con.close()
    kernel = AgentKernel(config=config, skills=None)  # type: ignore[arg-type]

    assert kernel.sessions.exists("s1") is False
    assert kernel._session_exists_anywhere("s1") is True
