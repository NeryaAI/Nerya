from __future__ import annotations

import pytest

from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect

pytestmark = pytest.mark.smoke


def test_transcript_returns_latest_messages_in_chronological_order(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1", title="Session")
    for i in range(5):
        repo.record_message(
            message_id=f"t{i}:user",
            session_id="s1",
            turn_id=f"t{i}",
            role="user",
            content=f"user {i}",
            ts=1000 + i * 2,
        )
        repo.record_message(
            message_id=f"t{i}:assistant",
            session_id="s1",
            turn_id=f"t{i}",
            role="assistant",
            content=f"assistant {i}",
            ts=1001 + i * 2,
        )

    rows = repo.transcript("s1", limit=4)

    assert [r["message_id"] for r in rows] == [
        "t3:user",
        "t3:assistant",
        "t4:user",
        "t4:assistant",
    ]
    con.close()


def test_message_edit_delete_and_session_lookup(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1", title="Original")
    repo.record_message(
        message_id="t1:user",
        session_id="s1",
        turn_id="t1",
        role="user",
        content="before",
        ts=1000,
    )

    assert repo.get_session("s1")["title"] == "Original"
    assert repo.update_message_content(
        session_id="s1",
        message_id="t1:user",
        content="after",
        ts=1001,
    )
    assert repo.transcript("s1", limit=10)[0]["content"] == "after"
    assert repo.delete_session_message(
        session_id="s1",
        message_id="t1:user",
        ts=1002,
    )
    assert repo.transcript("s1", limit=10) == []
    con.close()
