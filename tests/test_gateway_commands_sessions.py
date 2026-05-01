from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api.gateway_commands import CommandContext, DEFAULT_REGISTRY
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect

pytestmark = pytest.mark.smoke


def _ctx(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    client = SimpleNamespace(config=Config(paths=paths, data={}))
    state: dict[str, object] = {}

    def save_state(next_state):
        state.clear()
        state.update(dict(next_state or {}))

    return CommandContext(
        client=client,
        platform="telegram",
        chat_id="chat-1",
        session_id="gw-current",
        raw_text="",
        state=state,
        save_state=save_state,
    ), state


def _seed_db_session(tmp_path, session_id: str = "db-only") -> None:
    con = connect(tmp_path / "nerya.db")
    repo = AgentSessionRepository(con)
    repo.upsert_session(
        session_id=session_id,
        title="DB Shared Session",
        source="dashboard",
        ts=1000,
    )
    repo.record_message(
        message_id=f"{session_id}:user",
        session_id=session_id,
        turn_id=f"{session_id}:turn",
        role="user",
        content="hello from dashboard",
        ts=1001,
    )
    repo.record_message(
        message_id=f"{session_id}:assistant",
        session_id=session_id,
        turn_id=f"{session_id}:turn",
        role="assistant",
        content="last answer from sqlite",
        ts=1002,
    )
    con.close()


def test_sessions_lists_db_only_sessions(tmp_path):
    _seed_db_session(tmp_path)
    ctx, _state = _ctx(tmp_path)

    outcome = DEFAULT_REGISTRY.handle("/sessions", ctx)

    assert outcome.handled is True
    assert "db-only" in outcome.reply_text
    assert "DB Shared Session" in outcome.reply_text
    assert "1 turn(s)" in outcome.reply_text


def test_session_switch_accepts_db_only_session_and_returns_last_message(tmp_path):
    _seed_db_session(tmp_path)
    ctx, state = _ctx(tmp_path)
    ctx.raw_text = "/session db-only"

    outcome = DEFAULT_REGISTRY.handle(ctx.raw_text, ctx)

    assert outcome.handled is True
    assert "Switched to `db-only`" in outcome.reply_text
    assert "DB Shared Session" in outcome.reply_text
    assert "Last assistant message:" in outcome.reply_text
    assert "last answer from sqlite" in outcome.reply_text
    assert state["active_sessions"] == {"chat-1": "db-only"}
