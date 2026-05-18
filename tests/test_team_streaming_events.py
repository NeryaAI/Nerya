from __future__ import annotations

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.agent.streaming import get_default_bus
from nerya.llm.gateway import LLMCall
from nerya.subagents.registry import SubAgentSpec
from nerya.subagents.runtime import SubAgentLLMError, SubAgentRuntime
from nerya.subagents.tasks import TaskStore
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native import agents
from nerya.tools.native import tasks as native_tasks
from nerya.tools.permissions import PermissionContext, PermissionEngine
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall, ToolResult


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
            turn_id=None,
            parent_call_id=None,
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
    data = result.content[0].data
    assert data["ok"] is True
    assert data["status"] == "completed"
    assert "synchronous" in data["next_action"]
    assert "full report" in data["next_action"]
    assert "original user prompt language" in data["next_action"]
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


def test_team_run_passes_prompt_relative_language_instruction_to_members(monkeypatch) -> None:
    bus = get_default_bus()
    bus.clear()
    seen_payloads: list[dict] = []

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
            turn_id=None,
            parent_call_id=None,
        ):
            seen_payloads.append(dict(payload))
            name = target.split(":", 1)[1]
            return {
                "ok": True,
                "tier": "medium",
                "tokens": 7,
                "usd": 0.01,
                "wall_ms": 12,
                "output": {"summary": f"{name} 完成"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    call = ToolCall(
        name="team_run",
        id="toolu_team_language",
        turn_id="turn-language",
        arguments={
            "task": "帮我分析英伟达的基本面和风险",
            "roles": [{"name": "fundamentals_analyst"}, {"name": "risk_critic"}],
            "shared_payload": {"ticker": "NVDA"},
            "max_parallel": 2,
        },
    )

    result = agents.team_run_handler(call, config=object(), skills=object())

    assert not result.is_error
    data = result.content[0].data
    assert data["output_language"] == "the original user prompt language"
    assert "original user prompt language" in data["next_action"]
    assert "Chinese (中文)" not in data["next_action"]
    assert "Japanese (日本語)" not in data["next_action"]
    assert "Korean (한국어)" not in data["next_action"]
    assert "headings, labels" in data["next_action"]
    assert all(
        payload["output_language"] == "the original user prompt language"
        for payload in seen_payloads
    )
    member_starts = [
        event for event in bus.recent()
        if event["kind"] == "team.member.start"
    ]
    assert member_starts
    assert all(
        "Target user-visible language: the original user prompt language"
        in event.get("assignment_prompt", "")
        for event in member_starts
    )
    assert all(
        "role-relevant source data" in event.get("assignment_prompt", "")
        for event in member_starts
    )


def test_subagent_prompt_promotes_output_language_payload_to_instruction(tmp_path) -> None:
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=object(),
        llm=object(),
    )
    spec = SubAgentSpec(
        name="fundamentals_analyst",
        prompt_path=tmp_path / "fundamentals_analyst.agent.md",
        prompt="Analyze fundamentals.",
    )

    prompt = runtime._render_prompt(
        spec,
        {"ticker": "NVDA", "output_language": "the original user prompt language"},
        "",
        [],
        allowed=[],
        native_tools=[],
    )

    assert "Target user-visible language: the original user prompt language" in prompt
    assert "natural-language JSON values" in prompt


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
            turn_id=None,
            parent_call_id=None,
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


def test_team_run_treats_degraded_member_output_as_failure(monkeypatch) -> None:
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
            turn_id=None,
            parent_call_id=None,
        ):
            name = target.split(":", 1)[1]
            return {
                "ok": True,
                "tokens": 3,
                "usd": 0.001,
                "wall_ms": 4,
                "output": {
                    "degraded": True,
                    "error_kind": "unfinished_tool_request",
                    "summary": f"{name} requested tools but did not finish",
                },
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_degraded",
            arguments={
                "task": "Inspect AAPL",
                "roles": [{"name": "fundamentals_analyst"}],
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["ok"] is False
    assert data["status"] == "completed_with_failures"
    assert data["roles_succeeded"] == []
    assert data["roles_failed"] == ["fundamentals_analyst"]
    assert data["failures"][0]["error_kind"] == "unfinished_tool_request"


def test_team_run_suppresses_duplicate_run_in_same_turn(monkeypatch) -> None:
    bus = get_default_bus()
    bus.clear()
    dispatched: list[str] = []

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
            turn_id=None,
            parent_call_id=None,
        ):
            name = target.split(":", 1)[1]
            dispatched.append(name)
            return {
                "ok": True,
                "tokens": 2,
                "usd": 0.001,
                "wall_ms": 3,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    first = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_dup_1",
            turn_id="turn-team-dup",
            arguments={
                "task": "Build an NVDA stock report.",
                "roles": [{"name": "fundamentals_analyst"}],
            },
            metadata={"session_id": "sess-team-dup"},
        ),
        config=object(),
        skills=object(),
    )
    second = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_dup_2",
            turn_id="turn-team-dup",
            arguments={
                "task": "Continue the NVDA stock report with AgentTeam.",
                "roles": [{"name": "technical_analyst"}],
            },
            metadata={"session_id": "sess-team-dup"},
        ),
        config=object(),
        skills=object(),
    )

    assert not first.is_error
    assert not second.is_error
    first_data = first.content[0].data
    second_data = second.content[0].data
    assert dispatched == ["fundamentals_analyst"]
    assert first_data["status"] == "completed"
    assert second_data["status"] == "completed"
    assert second_data["duplicate_suppressed"] is True
    assert second_data["duplicate_status"] == "duplicate_suppressed"
    assert second_data["duplicate_of_team_run_id"] == first_data["team_run_id"]
    assert second_data["roles_succeeded"] == ["fundamentals_analyst"]
    assert "complete requested answer" in second_data["next_action"]
    assert any(event["kind"] == "team.duplicate" for event in bus.recent())


def test_subagent_run_is_suppressed_after_successful_team_in_same_turn(
    monkeypatch,
) -> None:
    bus = get_default_bus()
    bus.clear()
    dispatched: list[str] = []

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
            turn_id=None,
            parent_call_id=None,
        ):
            name = target.split(":", 1)[1]
            dispatched.append(name)
            return {
                "ok": True,
                "tokens": 2,
                "usd": 0.001,
                "wall_ms": 3,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    team_result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_then_subagent",
            turn_id="turn-team-then-subagent",
            arguments={
                "task": "Build an NVDA stock report.",
                "roles": [{"name": "fundamentals_analyst"}],
            },
            metadata={"session_id": "sess-team-then-subagent"},
        ),
        config=object(),
        skills=object(),
    )
    subagent_result = agents.subagent_run_handler(
        ToolCall(
            name="subagent_run",
            id="toolu_subagent_after_team",
            turn_id="turn-team-then-subagent",
            arguments={
                "name": "technical_analyst",
                "payload": {"ticker": "NVDA"},
            },
            metadata={"session_id": "sess-team-then-subagent"},
        ),
        config=object(),
        skills=object(),
    )

    assert not team_result.is_error
    assert not subagent_result.is_error
    data = subagent_result.content[0].data
    assert dispatched == ["fundamentals_analyst"]
    assert data["status"] == "team_already_completed"
    assert data["skipped"] is True
    assert data["team_summary"]["team_run_id"] == team_result.content[0].data["team_run_id"]
    assert "synthesize" in data["next_action"]
    assert "original user prompt language" in data["next_action"]
    assert any(event["kind"] == "team.subagent_duplicate" for event in bus.recent())


def test_task_tools_return_sync_team_result_after_team_run(
    monkeypatch,
    tmp_path,
) -> None:
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
            turn_id=None,
            parent_call_id=None,
        ):
            name = target.split(":", 1)[1]
            return {
                "ok": True,
                "tokens": 2,
                "usd": 0.001,
                "wall_ms": 3,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    store = TaskStore(WorkspacePaths(root=tmp_path))
    metadata = {"session_id": "sess-team-task-tools"}
    turn_id = "turn-team-task-tools"
    team_result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_for_task_tools",
            turn_id=turn_id,
            arguments={
                "task": "Build an NVDA stock report.",
                "roles": [{"name": "fundamentals_analyst"}],
            },
            metadata=metadata,
        ),
        config=object(),
        skills=object(),
    )

    get_result = native_tasks.task_get_handler(
        ToolCall(
            name="task_get",
            id="toolu_task_get_team",
            turn_id=turn_id,
            arguments={"task_id": "team_run_id"},
            metadata=metadata,
        ),
        store=store,
    )
    list_result = native_tasks.task_list_handler(
        ToolCall(
            name="task_list",
            id="toolu_task_list_team",
            turn_id=turn_id,
            arguments={"limit": 10},
            metadata=metadata,
        ),
        store=store,
    )
    output_result = native_tasks.task_output_handler(
        ToolCall(
            name="task_output",
            id="toolu_task_output_team",
            turn_id=turn_id,
            arguments={"task_id": "team_run_id"},
            metadata=metadata,
        ),
        store=store,
    )

    assert not team_result.is_error
    assert not get_result.is_error
    assert not list_result.is_error
    assert not output_result.is_error
    team_id = team_result.content[0].data["team_run_id"]
    assert get_result.content[0].data["task_id"] == team_id
    assert get_result.content[0].data["requested_task_id"] == "team_run_id"
    assert list_result.content[0].data["tasks"][0]["task_id"] == team_id
    assert output_result.content[0].data["task_id"] == team_id
    assert "Synthesize" in get_result.content[0].data["next_action"]
    assert "original user prompt language" in get_result.content[0].data["next_action"]


def test_role_list_surfaces_stock_research_role_guidance(tmp_path) -> None:
    result = agents.role_list_handler(
        ToolCall(name="role_list", id="toolu_roles", arguments={}),
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )

    assert not result.is_error
    data = result.content[0].data
    assert "public-company" in data["guidance"]
    assert "valuation_reviewer" in data["guidance"]
    assert "fundamentals_analyst" in data["recommended_stock_research_roles"]
    assert any(r["name"] == "fundamentals_analyst" for r in data["roles"])


def test_team_run_forwards_parent_tool_registry(monkeypatch) -> None:
    seen = {}
    registry = ToolRegistry()

    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            seen["tool_registry"] = tool_registry

        def dispatch(  # noqa: ANN201
            self,
            target: str,
            *,
            payload,
            trigger_event_id,
            strategy_id,
            session_id,
            turn_id=None,
            parent_call_id=None,
        ):
            return {
                "ok": True,
                "tokens": 1,
                "usd": 0.0,
                "wall_ms": 1,
                "output": {"summary": target},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_registry",
            arguments={
                "task": "Inspect AMZN",
                "roles": [{"name": "technical_analyst"}],
            },
        ),
        config=object(),
        skills=object(),
        tool_registry=registry,
    )

    assert not result.is_error
    assert seen["tool_registry"] is registry


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
        turn_id="turn-1",
        parent_call_id="toolu-team",
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
    assert start["turn_id"] == "turn-1"
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


def test_subagent_runtime_continues_after_skill_observations(tmp_path) -> None:
    class FakeEntry:
        id = "quote_skill"

    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return [FakeEntry()]

    class FakeRuntime:
        def call(self, skill, action, *, payload, **_kwargs):  # noqa: ANN201
            assert skill == "quote_skill"
            assert action == "latest"
            assert payload == {"ticker": "AAPL"}
            return {"price": 293.32, "source": "live_test"}

    class FakeSkills:
        registry = FakeRegistry()
        runtime = FakeRuntime()

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            self.prompts.append(kwargs["prompt"])
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=7,
                    usd=0.001,
                    raw='{"skill_calls":[{"skill":"quote_skill","action":"latest","payload":{"ticker":"AAPL"}}]}',
                    parsed={
                        "skill_calls": [
                            {
                                "skill": "quote_skill",
                                "action": "latest",
                                "payload": {"ticker": "AAPL"},
                            }
                        ]
                    },
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=9,
                usd=0.001,
                raw='{"summary":"AAPL quote observed","done":true}',
                parsed={"summary": "AAPL quote observed", "done": True},
                provider="fake",
                model="fake-model",
            )

    fake_llm = FakeLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=fake_llm,
    )
    spec = SubAgentSpec(
        name="technical_analyst",
        prompt_path=tmp_path / "technical_analyst.agent.md",
        prompt="Analyze the market.",
        allowed_skills=["quote_skill"],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"market": "yahoo:AAPL"},
    )

    assert fake_llm.calls == 2
    assert "prior observations" in fake_llm.prompts[1]
    assert result["output"]["summary"] == "AAPL quote observed"
    assert result["metrics"]["skill_calls"][0]["skill"] == "quote_skill"


def test_subagent_runtime_executes_legacy_xml_tool_call(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            self.prompts.append(kwargs["prompt"])
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=7,
                    usd=0.001,
                    raw=(
                        "<tool_call>\n"
                        "<function=market_data>\n"
                        "<parameter=action>get_ticker</parameter>\n"
                        "<parameter=venue>yahoo</parameter>\n"
                        "<parameter=market>AAPL</parameter>\n"
                        "</function>\n"
                        "</tool_call>"
                    ),
                    parsed={},
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=9,
                usd=0.001,
                raw='{"summary":"AAPL ticker observed","done":true}',
                parsed={"summary": "AAPL ticker observed", "done": True},
                provider="fake",
                model="fake-model",
            )

    native_calls: list[dict] = []

    def fake_market_data(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"market": call.arguments["market"], "last": 123.45},
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="market_data",
            description="test market data",
            input_schema={},
            handler=fake_market_data,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    fake_llm = FakeLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=fake_llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="fundamentals_analyst",
        prompt_path=tmp_path / "fundamentals_analyst.agent.md",
        prompt="Analyze fundamentals.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"market": "yahoo:AAPL"},
    )

    assert fake_llm.calls == 2
    assert native_calls == [
        {"venue": "yahoo", "market": "AAPL", "action": "get_ticker"}
    ]
    assert "prior observations" in fake_llm.prompts[1]
    assert result["output"]["summary"] == "AAPL ticker observed"
    assert result["metrics"]["skill_calls"][0]["skill"] == "market_data"


def test_subagent_runtime_executes_legacy_xml_tool_call_inside_raw_wrapper(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    legacy = (
        "I'll start by gathering filings."
        "<tool_call>\n"
        "<function=mcp__edgar__get_recent_filings>\n"
        "<parameter=ticker>NVDA</parameter>\n"
        "<parameter=limit>10</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=7,
                    usd=0.001,
                    raw=legacy,
                    parsed={"raw": legacy},
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=9,
                usd=0.001,
                raw='{"summary":"NVDA filings observed","done":true}',
                parsed={"summary": "NVDA filings observed", "done": True},
                provider="fake",
                model="fake-model",
            )

    native_calls: list[dict] = []

    def fake_edgar(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ticker": call.arguments["ticker"], "filings": []},
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="mcp__edgar__get_recent_filings",
            description="test edgar filings",
            input_schema={},
            handler=fake_edgar,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    fake_llm = FakeLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=fake_llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="earnings_reviewer",
        prompt_path=tmp_path / "earnings_reviewer.agent.md",
        prompt="Analyze earnings.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert fake_llm.calls == 2
    assert native_calls == [{"ticker": "NVDA", "limit": 10, "identifier": "NVDA"}]
    assert result["output"]["summary"] == "NVDA filings observed"
    assert result["metrics"]["skill_calls"][0]["skill"] == "mcp__edgar__get_recent_filings"


def test_subagent_runtime_executes_legacy_skill_calls_block_and_normalises_payloads(
    tmp_path,
) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    legacy = (
        "<tool_call>\n"
        "<skill_calls>\n"
        "["
        "{\"skill\":\"market_data\",\"action\":\"get_quote\","
        "\"payload\":{\"symbol\":\"NVDA\",\"venue\":\"yahoo\"}},"
        "{\"skill\":\"mcp__yahoo__get_stock_info\","
        "\"payload\":{\"symbol\":\"NVDA\"}}"
        "]\n"
        "</skill_calls>\n"
        "</tool_call>"
    )

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=7,
                    usd=0.001,
                    raw=legacy,
                    parsed={"raw": legacy},
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=9,
                usd=0.001,
                raw='{"summary":"NVDA data observed","done":true}',
                parsed={"summary": "NVDA data observed", "done": True},
                provider="fake",
                model="fake-model",
            )

    calls: list[tuple[str, dict]] = []

    def fake_tool(call):  # noqa: ANN001, ANN202
        calls.append((call.name, dict(call.arguments or {})))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True},
        )

    registry = ToolRegistry()
    for name in ("market_data", "mcp__yahoo__get_stock_info"):
        registry.register(
            make_native_descriptor(
                name=name,
                description=f"test {name}",
                input_schema={},
                handler=fake_tool,
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.NETWORK,
                read_only=True,
                auto_approve=True,
            )
        )

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=FakeLLM(),
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="technical_analyst",
        prompt_path=tmp_path / "technical_analyst.agent.md",
        prompt="Analyze the market.",
        allowed_skills=["websearch", "news_social", "market_data"],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert calls == [
        ("market_data", {"symbol": "NVDA", "venue": "yahoo", "action": "get_ticker", "market": "NVDA"}),
        ("mcp__yahoo__get_stock_info", {"symbol": "NVDA", "ticker": "NVDA"}),
    ]
    assert result["output"]["summary"] == "NVDA data observed"


def test_subagent_runtime_falls_back_to_tool_observations_after_tool_only_budget(
    tmp_path,
) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class ToolOnlyLLM:
        def call(self, **kwargs):  # noqa: ANN201, ARG002
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:risk_critic",
                tokens=3,
                usd=0.001,
                raw='{"skill_calls":[{"skill":"market_data","action":"get_ticker","payload":{"market":"NVDA"}}]}',
                parsed={
                    "skill_calls": [
                        {
                            "skill": "market_data",
                            "action": "get_ticker",
                            "payload": {"market": "NVDA"},
                        }
                    ]
                },
                provider="fake",
                model="fake-model",
            )

    def fake_market_data(call):  # noqa: ANN001, ANN202
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"market": call.arguments["market"], "last": 123.45},
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="market_data",
            description="test market data",
            input_schema={},
            handler=fake_market_data,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    runtime = SubAgentRuntime(
        config=Config(
            paths=WorkspacePaths(root=tmp_path),
            data={"agent": {"subagents": {"max_iterations": 1}}},
        ),
        skills=FakeSkills(),
        llm=ToolOnlyLLM(),
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="risk_critic",
        prompt_path=tmp_path / "risk_critic.agent.md",
        prompt="Analyze risk.",
        allowed_skills=["market_data"],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert result["output"]["done"] is True
    assert result["output"]["partial"] is True
    assert result["output"]["quality"] == "tool_observation_fallback"
    assert result["metrics"]["skill_calls"][0]["skill"] == "market_data"


def test_subagent_runtime_falls_back_after_raw_tool_request_followup(
    tmp_path,
) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    malformed_tool_request = (
        '<tool_call>\n<{"skill_calls": [{"skill": "market_data", '
        '"payload": {"market": "NVDA"}}]}'
    )

    class ToolThenRawLLM:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, **kwargs):  # noqa: ANN201, ARG002
            self.calls += 1
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task="subagent_analysis",
                    caller="subagent:fundamentals_analyst",
                    tokens=3,
                    usd=0.001,
                    raw='{"skill_calls":[{"skill":"market_data","payload":{"market":"NVDA"}}]}',
                    parsed={
                        "skill_calls": [
                            {"skill": "market_data", "payload": {"market": "NVDA"}}
                        ]
                    },
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:fundamentals_analyst",
                tokens=3,
                usd=0.001,
                raw=malformed_tool_request,
                parsed={"raw": malformed_tool_request},
                provider="fake",
                model="fake-model",
            )

    def fake_market_data(call):  # noqa: ANN001, ANN202
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"market": call.arguments["market"], "last": 225.32},
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="market_data",
            description="test market data",
            input_schema={},
            handler=fake_market_data,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    runtime = SubAgentRuntime(
        config=Config(
            paths=WorkspacePaths(root=tmp_path),
            data={"agent": {"subagents": {"max_iterations": 2}}},
        ),
        skills=FakeSkills(),
        llm=ToolThenRawLLM(),
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="fundamentals_analyst",
        prompt_path=tmp_path / "fundamentals_analyst.agent.md",
        prompt="Analyze fundamentals.",
        allowed_skills=["market_data"],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert result["output"]["quality"] == "tool_observation_fallback"
    assert result["output"]["observations"]
    assert result["metrics"]["skill_calls"][0]["skill"] == "market_data"


def test_subagent_runtime_prefetches_required_fundamental_data(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FinalOnlyLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.prompts.append(kwargs["prompt"])
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:fundamentals_analyst",
                tokens=3,
                usd=0.001,
                raw='{"quality":"ok","growth":"ok","valuation":"ok","confidence":0.8,"done":true}',
                parsed={
                    "quality": "ok",
                    "growth": "ok",
                    "valuation": "ok",
                    "confidence": 0.8,
                    "done": True,
                },
                provider="fake",
                model="fake-model",
            )

    native_calls: list[tuple[str, dict]] = []

    def fake_tool(call):  # noqa: ANN001, ANN202
        native_calls.append((call.name, dict(call.arguments or {})))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "tool": call.name, "args": call.arguments},
        )

    registry = ToolRegistry()
    for name in (
        "market_data",
        "mcp__yahoo__get_stock_info",
        "mcp__yahoo__get_financial_statement",
    ):
        registry.register(
            make_native_descriptor(
                name=name,
                description=f"test {name}",
                input_schema={},
                handler=fake_tool,
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.NETWORK,
                read_only=True,
                auto_approve=True,
            )
        )
    llm = FinalOnlyLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="fundamentals_analyst",
        prompt_path=tmp_path / "fundamentals_analyst.agent.md",
        prompt="Analyze fundamentals.",
        allowed_skills=["market_data"],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert (
        "market_data",
        {"market": "NVDA", "action": "get_ticker", "venue": "yahoo"},
    ) in native_calls
    assert (
        "mcp__yahoo__get_stock_info",
        {"ticker": "NVDA"},
    ) in native_calls
    statement_types = [
        payload.get("financial_type")
        for name, payload in native_calls
        if name == "mcp__yahoo__get_financial_statement"
    ]
    assert statement_types == ["income_stmt", "balance_sheet", "cashflow"]
    assert "prior observations" in llm.prompts[0]
    assert [call["skill"] for call in result["metrics"]["skill_calls"][:5]] == [
        "market_data",
        "mcp__yahoo__get_stock_info",
        "mcp__yahoo__get_financial_statement",
        "mcp__yahoo__get_financial_statement",
        "mcp__yahoo__get_financial_statement",
    ]
    assert result["output"]["data_coverage"]["has_market_data"] is True
    assert result["output"]["data_coverage"]["has_financial_statement"] is True


def test_subagent_runtime_marks_unfinished_tool_request_degraded(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class ToolOnlyLLM:
        def call(self, **kwargs):  # noqa: ANN201, ARG002
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:fundamentals_analyst",
                tokens=3,
                usd=0.001,
                raw='{"skill_calls":[{"skill":"missing","action":"x"}]}',
                parsed={"skill_calls": [{"skill": "missing", "action": "x"}]},
                provider="fake",
                model="fake-model",
            )

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=ToolOnlyLLM(),
    )
    spec = SubAgentSpec(
        name="fundamentals_analyst",
        prompt_path=tmp_path / "fundamentals_analyst.agent.md",
        prompt="Analyze fundamentals.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"market": "yahoo:AAPL"},
    )

    assert result["output"]["degraded"] is True
    assert result["output"]["error_kind"] == "unfinished_tool_request"


def test_subagent_runtime_marks_empty_output_as_degraded(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class EmptyLLM:
        def call(self, **kwargs):  # noqa: ANN201, ARG002
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:technical_analyst",
                tokens=3,
                usd=0.001,
                raw="",
                parsed={},
                provider="fake",
                model="fake-model",
            )

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=EmptyLLM(),
    )
    spec = SubAgentSpec(
        name="technical_analyst",
        prompt_path=tmp_path / "technical_analyst.agent.md",
        prompt="Analyze the market.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"market": "yahoo:AAPL"},
    )

    assert result["output"]["degraded"] is True
    assert result["output"]["error_kind"] == "empty_model_output"


def test_subagent_runtime_raises_when_llm_fails_before_output(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FailingLLM:
        def call(self, **kwargs):  # noqa: ANN201, ARG002
            raise RuntimeError("provider unavailable")

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=FailingLLM(),
    )
    spec = SubAgentSpec(
        name="technical_analyst",
        prompt_path=tmp_path / "technical_analyst.agent.md",
        prompt="Analyze the market.",
        allowed_skills=[],
        tier="medium",
    )

    with pytest.raises(SubAgentLLMError, match="failed before producing output"):
        runtime.run(
            spec,
            trigger_event_id="trigger-1",
            session_id="sess-1",
            payload={"market": "mock:AMZN"},
        )
