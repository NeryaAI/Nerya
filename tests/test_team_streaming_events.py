from __future__ import annotations

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.agent.streaming import get_default_bus
from nerya.llm.gateway import LLMCall
from nerya.subagents.registry import SubAgentSpec
from nerya.subagents.runtime import SubAgentRuntime
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native import agents
from nerya.tools.permissions import PermissionContext, PermissionEngine
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall


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
    team_ids = {
        event.get("team_run_id")
        for event in events
        if event["kind"].startswith("team.")
    }
    assert len(team_ids) == 1
    member_starts = [event for event in events if event["kind"] == "team.member.start"]
    assert all(event.get("payload") for event in member_starts)
    assert all("Agent Team member assignment" in event.get("assignment_prompt", "")
               for event in member_starts)

    end = [event for event in events if event["kind"] == "team.end"][0]
    assert set(end["roles_succeeded"]) == {"market_analyst", "risk_critic"}
    assert end["roles_failed"] == []
    assert end["results"]
    assert end["aggregated"]


def test_team_run_executor_repairs_stringified_roles(monkeypatch) -> None:
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
            return {
                "ok": True,
                "tokens": 3,
                "usd": 0.001,
                "wall_ms": 4,
                "output": {"summary": f"{name} ok"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="team_run",
            description="test team",
            input_schema=agents.TEAM_RUN_SCHEMA,
            handler=lambda call: agents.team_run_handler(
                call, config=object(), skills=object(),
            ),
            risk=RiskLevel.EXEC,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            auto_approve=True,
        )
    )
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(),
    )
    call = ToolCall(
        name="team_run",
        id="toolu_team_string_roles",
        arguments={
            "task": "Compare BTC and ETH market structure.",
            "roles": '[{"name":"market_analyst"},{"name":"risk_critic"}]',
        },
    )

    result = executor.execute(call)

    assert not result.is_error
    assert isinstance(call.arguments["roles"], list)
    kinds = [event["kind"] for event in bus.recent()]
    assert kinds.count("team.start") == 1
    assert kinds.count("team.member.start") == 2


def test_subagent_runtime_publishes_prompt_payload_and_output(tmp_path) -> None:
    bus = get_default_bus()
    bus.clear()

    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FakeLLM:
        def call(self, **kwargs):  # noqa: ANN201
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=11,
                usd=0.002,
                raw='{"summary":"done","done":true}',
                parsed={"summary": "done", "done": True},
                provider="fake",
                model="fake-model",
            )

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=FakeLLM(),
    )
    spec = SubAgentSpec(
        name="demo_researcher",
        prompt_path=tmp_path / "demo_researcher.agent.md",
        prompt="Role body: inspect the company and cite evidence.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={
            "team_run_id": "team-1",
            "team_template": "ad_hoc_parallel_team",
            "team_call_id": "toolu-team",
            "task_id": "role-demo_researcher",
            "task_owner": "demo_researcher",
            "task_subject": "Inspect TSLA",
            "secret": "sk-123456789012345678901",
        },
    )

    assert result["output"]["summary"] == "done"
    events = bus.recent()
    start = [e for e in events if e["kind"] == "subagent.start"][0]
    assert start["team_run_id"] == "team-1"
    assert start["team_call_id"] == "toolu-team"
    assert start["payload"]["secret"]["__redacted__"] is True
    assert "Role body" in start["role_prompt"]

    prompt_event = [
        e for e in events
        if e["kind"] == "subagent.step" and e.get("step_kind") == "prompt"
    ][0]
    assert "Role body: inspect the company" in prompt_event["prompt"]
    assert "Inspect TSLA" in prompt_event["prompt"]
    assert prompt_event["prompt_chars"] >= len(prompt_event["prompt"])

    end = [e for e in events if e["kind"] == "subagent.end"][0]
    assert end["output"]["summary"] == "done"
