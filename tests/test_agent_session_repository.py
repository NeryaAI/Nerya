from __future__ import annotations

import pytest

from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect
from nerya.api.routes_agent import _rehydrate_turn_tool_events

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


def test_transcript_limit_zero_returns_full_history(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1", title="Session")
    for i in range(3):
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

    rows = repo.transcript("s1", limit=0)

    assert [r["message_id"] for r in rows] == [
        "t0:user",
        "t0:assistant",
        "t1:user",
        "t1:assistant",
        "t2:user",
        "t2:assistant",
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
    assert repo.update_session_meta("s1", {"context_compaction": {"version": 1}})
    assert "context_compaction" in repo.get_session("s1")["meta_json"]
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


def test_tool_events_returns_events_in_insert_order(tmp_path):
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1", title="Session")
    repo.record_tool_event(
        event_id="t1:0:tool_use:call_1",
        session_id="s1",
        turn_id="t1",
        call_id="call_1",
        tool="native.read_file",
        phase="tool_use",
        payload={"payload": {"path": "README.md"}},
        ts=1000,
    )
    repo.record_tool_event(
        event_id="t1:1:tool_result:call_1",
        session_id="s1",
        turn_id="t1",
        call_id="call_1",
        tool="native.read_file",
        phase="tool_result",
        ok=True,
        payload={"result": "contents"},
        ts=1000,
    )
    repo.record_tool_event(
        event_id="t2:0:tool_use:call_2",
        session_id="s1",
        turn_id="t2",
        call_id="call_2",
        tool="native.grep",
        phase="tool_use",
        payload={"payload": {"pattern": "x"}},
        ts=999,
    )

    rows = repo.tool_events("s1", turn_ids=["t1"])

    assert [r["event_id"] for r in rows] == [
        "t1:0:tool_use:call_1",
        "t1:1:tool_result:call_1",
    ]
    con.close()


def test_rehydrate_turn_payload_from_tool_events():
    turn = {
        "turn_id": "t1",
        "reply_text": "done",
        "blocks": [],
        "tool_trace": [],
        "blocks_truncated": True,
        "tool_trace_truncated": True,
    }
    events = [
        {
            "turn_id": "t1",
            "call_id": "call_1",
            "tool": "native.read_file",
            "phase": "tool_use",
            "payload_json": '{"payload": {"path": "README.md"}}',
            "ts": 1000,
        },
        {
            "turn_id": "t1",
            "call_id": "call_1",
            "tool": "native.read_file",
            "phase": "tool_result",
            "ok": 1,
            "payload_json": '{"result": "contents", "elapsed_ms": 7}',
            "ts": 1001,
        },
        {
            "turn_id": "t1",
            "session_id": "s1",
            "call_id": "team-1",
            "tool": "native.agent_activity",
            "phase": "team.member.end",
            "ok": 1,
            "payload_json": (
                '{"event": {"kind": "team.member.end", '
                '"team_run_id": "team-1", "subagent": "analyst", '
                '"output": {"summary": "done"}}}'
            ),
            "ts": 1002,
        },
    ]

    out = _rehydrate_turn_tool_events(turn, events)

    assert out is not None
    assert out["blocks_rehydrated"] is True
    assert [b["block"]["kind"] for b in out["blocks"]] == ["tool_use", "tool_result"]
    assert out["blocks"][0]["block"]["payload"] == {"path": "README.md"}
    assert out["tool_trace_rehydrated"] is True
    assert out["tool_trace"][0]["action"] == "read_file"
    assert out["tool_trace"][0]["payload"] == {"path": "README.md"}
    assert out["tool_trace"][0]["result"] == "contents"
    assert out["activity_events_rehydrated"] is True
    assert out["activity_events"][0]["kind"] == "team.member.end"
    assert out["activity_events"][0]["subagent"] == "analyst"
