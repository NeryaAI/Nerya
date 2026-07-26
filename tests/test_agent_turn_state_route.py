from __future__ import annotations

from types import SimpleNamespace

from nerya.api import routes_agent
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths


def test_turn_state_missing_journal_returns_structured_not_found(tmp_path):
    handler = next(
        h
        for method, path, h in routes_agent.routes()
        if method == "POST" and path == "/agent/turn_state"
    )
    client = SimpleNamespace(config=Config(paths=WorkspacePaths(root=tmp_path)))

    result = handler(client, {"turn_id": "missing-turn"})

    assert result["_status"] == 404
    assert result["ok"] is False
    assert result["error"] == "turn_state_not_found"
    assert result["turn_id"] == "missing-turn"


def test_run_turn_handles_registered_slash_command_without_agent_kernel(tmp_path, monkeypatch):
    handler = next(
        h
        for method, path, h in routes_agent.routes()
        if method == "POST" and path == "/agent/run_turn"
    )
    client = SimpleNamespace(
        config=Config(paths=WorkspacePaths(root=tmp_path)),
        skills=object(),
    )

    class ExplodingKernel:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("slash commands should not enter AgentKernel")

    monkeypatch.setattr(routes_agent, "AgentKernel", ExplodingKernel)

    result = handler(
        client,
        {
            "session_id": "sess-command",
            "payload": {"text": "/workflows", "platform": "dashboard"},
        },
    )

    assert result["stopped_reason"] == "command"
    assert result["transition_reason"] == "slash_command"
    assert result["harness"] == "command"
    assert result["session_id"] == "sess-command"
    assert result["events"] == []
    assert "Workflows" in result["reply_text"]
    assert "schedule" in result["reply_text"]


def test_agent_tools_route_delegates_to_agent_api():
    expected = {"ok": True, "count": 0, "tools": [], "harness": "native"}
    client = SimpleNamespace(
        agent=SimpleNamespace(list_tools=lambda: expected),
    )
    handler = next(
        h
        for method, path, h in routes_agent.routes()
        if method == "GET" and path == "/agent/tools"
    )

    assert handler(client, {}) is expected
