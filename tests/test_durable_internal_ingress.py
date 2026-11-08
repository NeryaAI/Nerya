from copy import deepcopy

from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.agent.command_runtime import runtime as command_runtime


def test_internal_dashboard_ingress_admits_before_running_loop(tmp_path, monkeypatch):
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    manager = command_runtime(config)
    monkeypatch.setattr(manager, "kick", lambda _sid: None)
    handler = next(
        handler for method, path, handler in routes_agent.routes()
        if method == "POST" and path == "/agent/run_turn_internal"
    )

    result = handler(
        type("Client", (), {"config": config, "auth_actor_id": "local"})(),
        {
            "session_id": "fresh-dashboard-session",
            "source": "user_chat",
            "kind": "user.chat",
            "payload": {"text": "create a paper EMA strategy"},
        },
    )

    assert result["ok"] is True
    assert result["admitted"] is True
    assert result["command"]["session_id"] == "fresh-dashboard-session"
    snapshot = manager.store.snapshot("fresh-dashboard-session")
    assert len(snapshot["commands"]) == 1
    assert snapshot["commands"][0]["state"] == "queued"
