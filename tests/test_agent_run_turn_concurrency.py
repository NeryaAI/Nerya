from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from nerya.agent.kernel import AgentTurnResult
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def _run_turn_route():
    return next(
        handler
        for method, path, handler in routes_agent.routes()
        if method == "POST" and path == "/agent/run_turn"
    )


def test_run_turn_rejects_concurrent_turn_for_same_session(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)
    started = threading.Event()
    release = threading.Event()

    class SlowKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **kwargs):
            started.set()
            assert release.wait(timeout=5), "test timed out waiting to release slow turn"
            return AgentTurnResult(
                trigger_event_id="evt_1",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_1",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                final_text="done",
            )

    monkeypatch.setattr(routes_agent, "AgentKernel", SlowKernel)
    run_turn = _run_turn_route()
    payload = {
        "session_id": "shared-session",
        "trigger": {
            "id": "evt_1",
            "source": "dashboard",
            "kind": "user.chat",
            "payload": {"text": "first"},
        },
    }
    first_result: dict[str, object] = {}

    def call_first() -> None:
        first_result.update(run_turn(client, payload))

    worker = threading.Thread(target=call_first)
    worker.start()
    assert started.wait(timeout=2)

    second = run_turn(
        client,
        {
            **payload,
            "trigger": {
                "id": "evt_2",
                "source": "dashboard",
                "kind": "user.chat",
                "payload": {"text": "continue"},
            },
        },
    )

    release.set()
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert first_result["turn_id"] == "turn_1"
    assert second["_status"] == 409
    assert second["ok"] is False
    assert second["error"] == "session_turn_in_progress"
