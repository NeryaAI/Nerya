"""Public team tools use one dispatcher contract and real per-run durable state."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.agent.streaming import get_default_bus
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.subagents.dispatcher import SubAgentResult
from nerya.subagents.tasks import TaskStore
from nerya.teams.store import TeamStore
from nerya.tools import NativeToolExecutor, PermissionContext, PermissionEngine
from nerya.tools.native import agents, tasks
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.fixture
def team(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    registry = ToolRegistry()
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
                                  permission_context=PermissionContext())
    seen, behavior = [], {}
    bus = get_default_bus()
    bus.clear()

    class Dispatcher:
        def __init__(self, config, skills, tool_registry, executor):
            assert tool_registry is registry
            self.executor = executor

        def dispatch(self, target, **kwargs):
            name = target.split(":", 1)[1]
            seen.append((name, kwargs))
            action = behavior.get(name)
            if isinstance(action, Exception):
                raise action
            return SubAgentResult(
                ok=not bool(action), subagent=name, tier="medium", provider="fixture", model="native",
                tokens=7, usd=0.01, output={"summary": f"{name} evidence", "done": not bool(action),
                "evidence": [{"summary": "fixture fact", "source": "fixture://evidence"}],
                **(action or {})}, error="source failed" if action else None,
                error_kind="source_unavailable" if action else None,
            ).asdict()

    monkeypatch.setattr(agents, "SubAgentDispatcher", Dispatcher)
    def invoke(*, roles=None, metadata=None, **arguments):
        return agents.team_run_handler(ToolCall(
            name="team_run", id=f"call-{len(seen)}", turn_id="turn",
            arguments={"task": "Complete the assigned research", "roles": roles or [{"name": "analyst"}],
                       **arguments},
            metadata={"session_id": "session", "strategy_id": "strategy", "trigger_event_id": "event",
                      **(metadata or {})},
        ), config=config, skills=object(), tool_registry=registry, executor=executor)
    return SimpleNamespace(config=config, registry=registry, executor=executor, seen=seen,
                           behavior=behavior, invoke=invoke, bus=bus)


def test_team_events_and_durable_results_share_identity_and_usage(team):
    result = team.invoke(roles=[{"name": "analyst"}, {"name": "critic"}])
    assert not result.is_error
    data = result.content[0].data
    assert data["ok"] is True
    assert set(data["roles_succeeded"]) == {"analyst", "critic"}
    assert data["tokens_total"] == 14
    assert data["usd_total"] == pytest.approx(0.02)
    events = team.bus.recent()
    kinds = [event["kind"] for event in events]
    assert kinds.count("team.start") == kinds.count("team.end") == 1
    assert kinds.count("team.member.start") == kinds.count("team.member.end") == 2
    for name, kwargs in team.seen:
        assert kwargs["session_id"] == "session"
        assert kwargs["strategy_id"] == "strategy"
        assert kwargs["turn_id"] == "turn"
        assert kwargs["payload"]["team_run_id"] == data["team_run_id"]
    store = TeamStore(team.config.paths)
    record = store.read_run(data["team_run_id"])
    assert record.status == "completed"
    assert {task.status for task in store.list_tasks(record.id)} == {"completed"}
    assert {m.name for m in store.read_members(record.id)} == {"analyst", "critic"}
    report = (store.synthesis_dir(record.id) / "final_report.md").read_text()
    assert "analyst evidence" in report and "critic evidence" in report
    assert "do not launch" not in data["next_action"]
    assert "next permitted action" in data["next_action"]


@pytest.mark.parametrize("output", [
    {"done": False, "degraded": True, "error_kind": "unfinished"},
    {"done": False, "missing_evidence": ["required source"]},
    {"done": False, "cancelled": True, "error_kind": "cancelled"},
])
def test_failed_member_keeps_evidence_and_cost_without_claiming_completion(team, output):
    team.behavior["analyst"] = output
    result = team.invoke()
    data = result.content[0].data
    assert data["ok"] is False
    assert data["roles_succeeded"] == []
    assert data["roles_failed"] == ["analyst"]
    assert data["tokens_total"] == 7
    assert data["failures"][0]["output"]["evidence"]
    assert TeamStore(team.config.paths).read_run(data["team_run_id"]).status != "completed"


def test_member_exception_does_not_drop_other_members(team):
    team.behavior["broken"] = RuntimeError("fixture outage")
    result = team.invoke(roles=[{"name": "broken"}, {"name": "working"}])
    data = result.content[0].data
    assert data["roles_succeeded"] == ["working"]
    assert data["roles_failed"] == ["broken"]
    assert "fixture outage" in str(data["failures"])


@pytest.mark.parametrize("analysis,output", [("Chinese", "English"), ("English", "Chinese"),
    ("Japanese", "Japanese")])
def test_language_and_role_payload_are_not_overridden_by_runtime(team, analysis, output):
    result = team.invoke(roles=[{"name": "reviewer", "payload": {"focus": "valuation"},
                                "provider": "fixture", "model": "chosen"}],
                         analysis_language=analysis, output_language=output,
                         shared_payload={"asset": "fixture-asset"})
    data = result.content[0].data
    payload = team.seen[0][1]["payload"]
    assert data["analysis_language"] == payload["analysis_language"] == analysis
    assert data["output_language"] == payload["output_language"] == output
    assert payload["focus"] == "valuation"
    assert payload["asset"] == "fixture-asset"
    assert team.seen[0][1]["inline_spec"].model == "chosen"


@pytest.mark.parametrize("seconds", [0.0, 0.25, 3.0, 60.0, 300.0])
def test_team_does_not_invent_role_specific_budget_floors(team, seconds):
    args = {"timeout_s": seconds, "team_template": "market_analysis_team",
            "roles": [{"name": "fundamentals_analyst"}]}
    assert agents._effective_team_timeout_seconds(args=args, config=team.config) == seconds


def test_parent_remaining_budget_is_a_hard_cap_without_slack(team):
    assert agents._effective_team_timeout_seconds(args={"timeout_s": 900}, config=team.config,
        parent_remaining_wall_seconds=100, parent_final_reserve_seconds=25) == 75
    result = team.invoke(timeout_s=100, metadata={"remaining_wall_seconds": 10,
                         "wall_time_final_synthesis_seconds": 3})
    assert result.content[0].data["timeout_s"] == 7


def test_new_team_and_child_are_not_suppressed_by_previous_team(team):
    first = team.invoke()
    second = team.invoke(roles=[{"name": "new_critic"}], task="Check newly observed risk")
    child = agents.subagent_run_handler(ToolCall(name="subagent_run", id="new-child", turn_id="turn",
        arguments={"name": "verifier", "payload": {"task": "verify new fact"}},
        metadata={"session_id": "session"}), config=team.config, skills=object(),
        tool_registry=team.registry, executor=team.executor)
    assert not child.is_error
    assert [name for name, _ in team.seen] == ["analyst", "new_critic", "verifier"]
    assert first.content[0].data["team_run_id"] != second.content[0].data["team_run_id"]
    assert "skipped" not in child.content[0].data


@pytest.mark.parametrize("handler", [tasks.task_get_handler, tasks.task_output_handler, tasks.task_summary_handler])
def test_unknown_task_is_not_replaced_by_an_unrelated_team_result(team, handler):
    completed = team.invoke().content[0].data
    result = handler(ToolCall(name="task_lookup", id="lookup", turn_id="turn",
        arguments={"task_id": completed["team_run_id"]}, metadata={"session_id": "session"}),
        store=TaskStore(team.config.paths))
    assert result.is_error
    assert not TaskStore(team.config.paths).list()


@pytest.mark.parametrize("name,generated", [("custom_specialist", True), ("fundamentals_analyst", False)])
def test_role_discovery_does_not_require_saving_new_roles(team, name, generated):
    result = agents.role_get_handler(ToolCall(name="role_get", id="role", arguments={"name": name}),
                                    config=team.config)
    assert not result.is_error
    data = result.content[0].data
    assert data["name"] == name
    assert bool(data.get("generated")) is generated
    assert data["prompt"]
    assert "trading" not in data["allowed_skills"]
    assert "wallet" not in data["allowed_skills"]


def test_invalid_role_policy_does_not_fall_back_to_wider_defaults(team):
    with pytest.raises(ValueError):
        agents._build_inline_role_spec(team.config, name="specialist",
                                       execution_policy={"max_wall_seconds": -1})
