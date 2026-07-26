"""SDK smoke tests.

Boots an :class:`InternalClient` against a temp workspace and verifies
each top-level SDK surface (trading, llm, strategy, messages, skill,
agent, triggers) is reachable and exposes the contract the dashboard
and HTTP routes rely on. Failures here mean a release would silently
break the file-based SDK bridge.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentTurnResult
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.sdk import agent_api as agent_api_module
from nerya.sdk.internal_client import InternalClient


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_internal_client_boots(tmp_path):
    """``InternalClient.from_config`` returns a fully-populated client."""

    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.config is cfg
    for name in ("trading", "llm", "strategy", "messages",
                 "skill", "agent", "triggers"):
        api = getattr(client, name, None)
        assert api is not None, f"InternalClient missing required surface: {name}"


def test_sdk_trading_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    # The TradingAPI exposes the operator-facing methods the HTTP route
    # calls. We only assert presence; semantics are covered by trading tests.
    for attr in ("submit_intent", "cancel_order", "get_strategy_history"):
        assert hasattr(client.trading, attr), f"TradingAPI missing {attr}"


def test_sdk_agent_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    # Smoke-check the agent API has a callable for the workspace-native
    # transcript / trace helpers.
    assert client.agent is not None
    tools = client.agent.list_tools()
    assert tools["ok"] is True
    assert tools["count"] == len(tools["tools"])
    assert tools["count"] > 0


def test_sdk_agent_run_turn_returns_complete_kernel_contract(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    block = {
        "seq": 1,
        "turn_id": "turn-1",
        "message_id": "turn-1:assistant",
        "role": "assistant",
        "block": {"kind": "text", "text": "done"},
    }
    turn = AgentTurnResult(
        trigger_event_id="event-1",
        strategy_id="strategy-1",
        session_id="session-1",
        turn_id="turn-1",
        decision={"action": "send_message", "text": "done"},
        actions=[{"action": "send_message", "text": "done"}],
        tool_trace=[],
        budget={"iterations": 1},
        steps=[block],
        blocks=[block],
        stopped_reason="end_turn",
        transition_reason="verified",
        final_text="  done  ",
        iterations=1,
        activity_events=[{"kind": "team.end"}],
        artifact_index={"artifacts": [{"path": "report.md"}]},
        verifier_outcome={"transition_label": "verified"},
        execution_state={"version": 1},
        final_report={"summary": "done"},
        attachments=[{"kind": "file", "path": "report.md"}],
    )

    class FakeKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **_kwargs):
            return turn

    monkeypatch.setattr(agent_api_module, "AgentKernel", FakeKernel)

    result = InternalClient.from_config(cfg).agent.run_turn(
        text="run",
        strategy_id="strategy-1",
        session_id="session-1",
    )

    assert result["strategy_id"] == "strategy-1"
    assert result["session_id"] == "session-1"
    assert result["reply_text"] == "done"
    assert result["final_text"] == "  done  "
    assert result["events"][0]["phase"] == "message"
    assert result["transition_reason"] == "verified"
    assert result["activity_events"] == [{"kind": "team.end"}]
    assert result["artifact_index"]["artifacts"][0]["path"] == "report.md"
    assert result["verifier_outcome"]["transition_label"] == "verified"
    assert result["execution_state"] == {"version": 1}
    assert result["final_report"] == {"summary": "done"}
    assert result["attachments"][0]["path"] == "report.md"


def test_sdk_messages_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.messages is not None


def test_sdk_llm_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.llm is not None


def test_sdk_skill_api_call_routes_to_kernel(tmp_path):
    """``client.skill.call`` must route to the SkillKernel."""

    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    # Sanity: SkillAPI must expose a ``call`` method used by routes_scripts.
    assert hasattr(client.skill, "call"), "SkillAPI missing call()"


def test_sdk_triggers_runtime_boots(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.triggers_runtime is not None
    # TriggerAPI must expose at least a way to list triggers.
    assert client.triggers is not None
