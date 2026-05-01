from __future__ import annotations

import pytest

from nerya.agent.streaming import get_default_bus
from nerya.tools.native import agents
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def test_team_run_publishes_member_lifecycle_events(monkeypatch) -> None:
    bus = get_default_bus()
    bus.clear()

    class FakeDispatcher:
        def __init__(self, config, skills) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills

        def dispatch(  # noqa: ANN201
            self,
            target: str,
            *,
            payload,
            trigger_event_id,
            strategy_id,
            session_id,
        ):
            name = target.split(":", 1)[1]
            assert session_id == "sess-1"
            assert strategy_id == "strategy-1"
            assert trigger_event_id == "trigger-1"
            return {
                "ok": True,
                "tier": "medium",
                "tokens": 7,
                "usd": 0.01,
                "wall_ms": 12,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    call = ToolCall(
        name="team_run",
        id="toolu_team",
        turn_id="turn-1",
        arguments={
            "task": "Compare BTC and ETH market structure.",
            "roles": [{"name": "market_analyst"}, {"name": "risk_critic"}],
            "shared_payload": {"market": "BTCUSDT"},
            "max_parallel": 2,
        },
        metadata={
            "session_id": "sess-1",
            "strategy_id": "strategy-1",
            "trigger_event_id": "trigger-1",
        },
    )

    result = agents.team_run_handler(call, config=object(), skills=object())

    assert not result.is_error
    events = bus.recent()
    kinds = [event["kind"] for event in events]
    assert kinds.count("team.start") == 1
    assert kinds.count("team.member.start") == 2
    assert kinds.count("team.member.end") == 2
    assert kinds.count("team.end") == 1
    assert {event.get("session_id") for event in events} == {"sess-1"}
    assert {event.get("turn_id") for event in events} == {"turn-1"}

    end = [event for event in events if event["kind"] == "team.end"][0]
    assert set(end["roles_succeeded"]) == {"market_analyst", "risk_critic"}
    assert end["roles_failed"] == []
