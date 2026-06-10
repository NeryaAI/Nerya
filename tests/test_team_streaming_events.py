from __future__ import annotations

import pytest

from nerya.core.errors import LLMError
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.agent.streaming import get_default_bus
from nerya.llm.gateway import LLMCall
from nerya.subagents.registry import SubAgentSpec
from nerya.subagents.runtime import SubAgentLLMError, SubAgentRuntime
from nerya.subagents.tasks import TaskStore
from nerya.teams.store import TeamStore
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native import agents
from nerya.tools.native import tasks as native_tasks
from nerya.tools.permissions import PermissionContext, PermissionEngine
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall, ToolError, ToolErrorKind, ToolResult


pytestmark = pytest.mark.smoke


def test_team_run_coerces_provider_wrapped_role_items() -> None:
    args = {
        "roles": [{"name": "fundamentals_analyst"}],
        "item": {
            "name": "technical_analyst",
            "payload": {"market": "ETH"},
            "item": [
                {"name": "sentiment_analyst"},
                {"name": "risk_critic", "instructions": "Report blockers."},
            ],
            "invoke name=\"task_create\"": {"task_type": "agent"},
        },
    }

    roles = agents._coerce_roles_arg(args["roles"], args=args)

    assert roles == [
        {"name": "fundamentals_analyst"},
        {"name": "technical_analyst", "payload": {"market": "ETH"}},
        {"name": "sentiment_analyst"},
        {"name": "risk_critic", "instructions": "Report blockers."},
    ]


def test_team_run_coerces_provider_role_payloads_and_raw_roles() -> None:
    args = {
        "role_payloads": {
            "fundamental_analyst": {"ticker": "NVDA", "focus": "business quality"},
            "dcf_modeler": {"ticker": "NVDA", "focus": "valuation"},
        },
        "_raw": {
            "roles": [
                {
                    "name": "sec_filing_analyst",
                    "payload": {"ticker": "NVDA", "form": "10-K"},
                }
            ],
            "role_payloads": {
                "guru_perspective": {"ticker": "NVDA", "focus": "investor lens"},
            },
        },
    }

    roles = agents._coerce_roles_arg(None, args=args)

    assert roles == [
        {
            "name": "fundamental_analyst",
            "payload": {"ticker": "NVDA", "focus": "business quality"},
        },
        {"name": "dcf_modeler", "payload": {"ticker": "NVDA", "focus": "valuation"}},
        {
            "name": "sec_filing_analyst",
            "payload": {"ticker": "NVDA", "form": "10-K"},
        },
        {"name": "guru_perspective", "payload": {"ticker": "NVDA", "focus": "investor lens"}},
    ]


def test_team_run_timeout_scales_for_multi_wave_deep_teams(tmp_path) -> None:
    args = {
        "team_template": "market_analysis_team",
        "max_parallel": 4,
        "roles": [
            {"name": "fundamentals_analyst"},
            {"name": "technical_analyst"},
            {"name": "sentiment_analyst"},
            {"name": "bull_researcher"},
            {"name": "bear_researcher"},
            {"name": "risk_critic"},
            {"name": "research_manager"},
        ],
    }

    timeout = agents._effective_team_timeout_seconds(
        args=args,
        shared_payload={"deadline": "5s"},
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )

    assert timeout == 720.0


def test_team_run_explicit_timeout_keeps_priority(tmp_path) -> None:
    args = {
        "timeout_s": 60,
        "team_template": "market_analysis_team",
        "max_parallel": 4,
        "roles": [{"name": f"role_{idx}"} for idx in range(7)],
    }

    timeout = agents._effective_team_timeout_seconds(
        args=args,
        shared_payload={"deadline": "5s"},
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )

    assert timeout == 60.0


def test_team_run_does_not_let_model_timeout_undercut_deep_team_floor(tmp_path) -> None:
    args = {
        "timeout_s": 240,
        "team_template": "market_analysis_team",
        "max_parallel": 4,
        "roles": [
            {"name": "fundamentals_analyst"},
            {"name": "technical_analyst"},
            {"name": "valuation_analyst"},
            {"name": "bull_researcher"},
            {"name": "bear_researcher"},
            {"name": "risk_critic"},
            {"name": "research_manager"},
        ],
    }

    timeout = agents._effective_team_timeout_seconds(
        args=args,
        shared_payload={},
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )

    assert timeout == 720.0


def test_curated_single_wave_deep_team_keeps_research_floor(tmp_path) -> None:
    args = {
        "team_template": "market_analysis_team",
        "max_parallel": 4,
        "roles": [
            {"name": "fundamentals_analyst"},
            {"name": "valuation_analyst"},
            {"name": "sec_analyst"},
            {"name": "investor_perspective"},
        ],
    }

    timeout = agents._effective_team_timeout_seconds(
        args=args,
        shared_payload={},
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )

    assert timeout == 600.0


def test_team_run_caps_model_requested_parallel_to_template_limit(monkeypatch, tmp_path) -> None:
    dispatched: list[str] = []

    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
                "subagent": name,
                "tier": "medium",
                "tokens": 1,
                "usd": 0.0,
                "wall_ms": 1,
                "output": {"summary": f"{name} done", "done": True},
                "metrics": {},
                "steps": [],
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    roles = [
        "fundamentals_analyst",
        "technical_analyst",
        "sentiment_analyst",
        "bull_researcher",
        "bear_researcher",
        "risk_critic",
        "research_manager",
    ]

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_parallel_cap",
            arguments={
                "task": "Deep NVDA research",
                "team_template": "market_analysis_team",
                "max_parallel": 7,
                "roles": [{"name": name} for name in roles],
            },
        ),
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert sorted(dispatched) == sorted(roles)
    assert data["max_parallel"] == 4
    assert data["timeout_uncapped_s"] == 720.0


def test_stock_research_subagents_default_wall_time_allows_final_synthesis(tmp_path) -> None:
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=object(),
        llm=object(),
    )
    spec = SubAgentSpec(name="fundamentals_analyst", prompt_path=tmp_path / "fundamentals.agent.md")

    assert runtime._max_wall_seconds(spec) >= 360.0  # noqa: SLF001


def test_team_run_parent_remaining_wall_budget_caps_model_timeout(monkeypatch) -> None:
    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
                "tier": "medium",
                "tokens": 1,
                "usd": 0.0,
                "wall_ms": 1,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_parent_budget",
            turn_id="turn-team-parent-budget",
            arguments={
                "task": "Deep public-company research.",
                "roles": [{"name": "fundamentals_analyst"}],
                "timeout_s": 840,
            },
            metadata={"remaining_wall_seconds": 200.0},
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["timeout_s"] == 80.0
    assert data["timeout_capped_by_parent"] is True
    assert data["parent_remaining_wall_seconds"] == 200.0


def test_team_run_parent_budget_keeps_deep_floor_when_cap_delta_is_small(tmp_path) -> None:
    args = {
        "team_template": "market_analysis_team",
        "max_parallel": 4,
        "roles": [
            {"name": "fundamentals_analyst"},
            {"name": "technical_analyst"},
            {"name": "sentiment_analyst"},
            {"name": "bull_researcher"},
            {"name": "bear_researcher"},
            {"name": "risk_critic"},
            {"name": "research_manager"},
        ],
    }

    timeout = agents._effective_team_timeout_seconds(
        args=args,
        shared_payload={},
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        parent_remaining_wall_seconds=747.0,
        parent_final_reserve_seconds=150.0,
    )

    assert timeout == 720.0


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


def test_native_team_run_is_listed_by_team_runs_api_store(monkeypatch, tmp_path) -> None:
    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
                "tier": "medium",
                "tokens": 11,
                "usd": 0.02,
                "wall_ms": 25,
                "output": {"summary": f"{name} complete", "signal": "neutral"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    call = ToolCall(
        name="team_run",
        id="toolu_team_store",
        turn_id="turn-team-store",
        arguments={
            "task": "Analyze TSLA with the market team.",
            "roles": [{"name": "fundamentals_analyst"}, {"name": "risk_critic"}],
            "max_parallel": 2,
        },
        metadata={
            "session_id": "sess-team-store",
            "original_user_prompt": "Use AgentTeam to analyze TSLA and give buy/hold/sell rating.",
        },
    )

    result = agents.team_run_handler(call, config=cfg, skills=object())

    assert not result.is_error
    assert result.content[0].data["team_template"] == "ad_hoc_parallel_team"
    store = TeamStore(cfg.paths)
    runs = store.list_runs(limit=5)
    assert runs[0]["template"] == "ad_hoc_parallel_team"
    assert runs[0]["template_id"] == "ad_hoc_parallel_team"
    assert runs[0]["session_id"] == "sess-team-store"
    assert [m.name for m in store.read_members(runs[0]["id"])] == [
        "fundamentals_analyst",
        "risk_critic",
    ]
    assert {t.status for t in store.list_tasks(runs[0]["id"])} == {"completed"}
    report = (store.synthesis_dir(runs[0]["id"]) / "final_report.md").read_text(
        encoding="utf-8"
    )
    assert "AgentTeam Report" not in report
    assert "fundamentals_analyst complete" in report
    assert "risk_critic complete" in report
    forbidden = (
        "Aggregated",
        "Final output language",
        "Role analysis language",
        "Team status",
        "succeeded_roles",
        "incomplete_roles",
        "team_run_id",
        "tokens_total",
        "usd_total",
        '{"',
    )
    for marker in forbidden:
        assert marker not in report


def test_team_run_dispatches_provider_role_payloads_and_persists_requested_roles(
    monkeypatch,
    tmp_path,
) -> None:
    dispatched: list[str] = []
    seen_payloads: list[dict] = []

    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
            seen_payloads.append(dict(payload))
            return {
                "ok": True,
                "tier": "medium",
                "tokens": 3,
                "usd": 0.001,
                "wall_ms": 5,
                "output": {"summary": f"{name} complete", "done": True},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    role_payloads = {
        "fundamental_analyst": {"ticker": "NVDA", "focus": "business quality"},
        "dcf_modeler": {"ticker": "NVDA", "focus": "valuation"},
        "sec_filing_analyst": {"ticker": "NVDA", "form": "10-K"},
        "guru_perspective": {"ticker": "NVDA", "focus": "investor lens"},
    }

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_provider_roles",
            turn_id="turn-provider-roles",
            arguments={
                "task": "Build a public-company research report.",
                "team_template": "market_analysis_team",
                "role_payloads": role_payloads,
                "max_parallel": 4,
            },
            metadata={"session_id": "sess-provider-roles"},
        ),
        config=cfg,
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["roles_requested"] == list(role_payloads)
    assert data["roles_total"] == 4
    assert dispatched == list(role_payloads)
    assert [payload["focus"] for payload in seen_payloads if "focus" in payload] == [
        "business quality",
        "valuation",
        "investor lens",
    ]
    store = TeamStore(cfg.paths)
    runs = store.list_runs(limit=1)
    assert runs[0]["metrics"]["roles_total"] == 4
    assert [m.name for m in store.read_members(runs[0]["id"])] == list(role_payloads)


def test_native_team_run_store_preserves_degraded_member_output(monkeypatch, tmp_path) -> None:
    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
                "tier": "medium",
                "tokens": 7,
                "usd": 0.01,
                "wall_ms": 12,
                "output": {
                    "done": True,
                    "partial": True,
                    "quality": "tool_observation_fallback",
                    "summary": f"{name} gathered evidence but did not finalize",
                    "observations": [
                        {
                            "skill": "market_data",
                            "ok": True,
                            "summary": {"last": 123.45},
                        }
                    ],
                },
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_store_degraded",
            turn_id="turn-team-store-degraded",
            arguments={
                "task": "Analyze a public company with a research team.",
                "roles": [{"name": "fundamentals_analyst"}],
            },
            metadata={"session_id": "sess-team-store-degraded"},
        ),
        config=cfg,
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["status"] == "completed_with_failures"
    store = TeamStore(cfg.paths)
    task = store.list_tasks(data["team_run_id"])[0]
    assert task.status == "failed"
    assert task.payload["output"]["quality"] == "tool_observation_fallback"
    assert task.payload["output"]["observations"]


def test_team_run_treats_missing_research_evidence_contract_as_member_failure(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
                "tier": "medium",
                "tokens": 7,
                "usd": 0.01,
                "wall_ms": 12,
                "output": {
                    "done": True,
                    "role": name,
                    "role_profile": "fundamentals_analyst",
                    "summary": "The company appears attractive.",
                    "data_coverage": {
                        "has_market_data": True,
                        "has_financial_statement": False,
                        "has_sec_filing": False,
                        "tool_errors": [
                            {
                                "skill": "data_api",
                                "action": "(native)",
                                "error": "credential_missing",
                            }
                        ],
                    },
                    "evidence_contract": {
                        "status": "degraded",
                        "missing_evidence": ["financial_statement"],
                    },
                },
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_missing_evidence",
            turn_id="turn-team-missing-evidence",
            arguments={
                "task": "Build a public-company research report.",
                "roles": [{"name": "fundamental_analyst"}],
            },
            metadata={"session_id": "sess-team-missing-evidence"},
        ),
        config=cfg,
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["status"] == "completed_with_failures"
    assert data["roles_failed"] == ["fundamental_analyst"]
    assert data["failures"][0]["error_kind"] == "insufficient_research_evidence"
    assert data["failures"][0]["output"]["missing_evidence"] == ["financial_statement"]
    store = TeamStore(cfg.paths)
    task = store.list_tasks(data["team_run_id"])[0]
    assert task.status == "failed"
    assert task.payload["output"]["missing_evidence"] == ["financial_statement"]


def test_team_run_template_resolution_respects_explicit_input() -> None:
    assert agents._resolve_team_template(
        requested="investment_committee_team",
        role_names=["bull_researcher", "bear_researcher", "risk_critic"],
        task="Comprehensively analyze TSLA and give a buy/hold/sell rating.",
    ) == "investment_committee_team"
    assert agents._resolve_team_template(
        requested="investment_committee_team",
        role_names=["bull_researcher", "bear_researcher", "risk_critic"],
        task="Debate whether we should go long TSLA and stress-test the thesis.",
    ) == "investment_committee_team"
    assert agents._resolve_team_template(
        requested="investment_committee_team",
        role_names=[
            "fundamentals_analyst",
            "technical_analyst",
            "sentiment_analyst",
            "bull_researcher",
            "bear_researcher",
            "risk_critic",
            "research_manager",
        ],
    ) == "investment_committee_team"
    assert agents._resolve_team_template(
        requested="ad_hoc_parallel_team",
        role_names=["bull_researcher", "bear_researcher", "risk_critic"],
    ) == "ad_hoc_parallel_team"
    assert agents._resolve_team_template(
        requested="ad_hoc_parallel_team",
        role_names=[
            "fundamentals_analyst",
            "technical_analyst",
            "sentiment_analyst",
            "bull_researcher",
            "bear_researcher",
            "risk_critic",
            "research_manager",
        ],
    ) == "ad_hoc_parallel_team"
    assert agents._resolve_team_template(
        requested="ad_hoc_parallel_team",
        role_names=["market_analyst", "risk_critic", "execution_planner"],
    ) == "ad_hoc_parallel_team"


def test_team_run_respects_explicit_committee_template_without_prompt_keywords(
    monkeypatch,
) -> None:
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
            assert payload["team_template"] == "investment_committee_team"
            assert payload["asset"] == "TSLA"
            return {
                "ok": True,
                "tier": "medium",
                "tokens": 1,
                "usd": 0.0,
                "wall_ms": 1,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)
    call = ToolCall(
        name="team_run",
        id="toolu_team_expand",
        arguments={
            "task": (
                "对 TSLA 进行全面的多空对抗式分析，给出明确的 "
                "buy/hold/sell 评级与目标价。"
            ),
            "roles": [
                {"name": "bull_researcher"},
                {"name": "bear_researcher"},
                {"name": "risk_critic"},
            ],
            "team_template": "investment_committee_team",
            "shared_payload": {"asset": "TSLA"},
        },
    )

    result = agents.team_run_handler(call, config=object(), skills=object())

    assert not result.is_error
    data = result.content[0].data
    assert data["team_template"] == "investment_committee_team"
    assert set(data["roles_requested"]) == {
        "bull_researcher",
        "bear_researcher",
        "risk_critic",
    }
    assert set(dispatched) == set(data["roles_requested"])


def test_team_run_respects_explicit_roles_and_short_deadline(monkeypatch) -> None:
    dispatched: list[str] = []
    seen_payloads: list[dict] = []

    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
            seen_payloads.append(dict(payload))
            return {
                "ok": True,
                "tier": "medium",
                "tokens": 1,
                "usd": 0.0,
                "wall_ms": 1,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_quick",
            turn_id="turn-team-quick",
            arguments={
                "task": "对 ETH (Ethereum) 进行快速多角度分析，5 秒内给出明确观点",
                "roles": [
                    {"name": "fundamentals_analyst"},
                    {"name": "technical_analyst"},
                    {"name": "sentiment_analyst"},
                ],
                "team_template": "market_analysis_team",
                "shared_payload": {"asset": "ETH", "deadline": "5s"},
                "timeout_s": 60,
                "max_parallel": 3,
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["team_template"] == "market_analysis_team"
    assert data["roles_requested"] == [
        "fundamentals_analyst",
        "technical_analyst",
        "sentiment_analyst",
    ]
    assert dispatched == data["roles_requested"]
    assert data["roles_total"] == 3
    assert data["timeout_s"] == 60.0
    assert all(payload["team_template"] == "market_analysis_team" for payload in seen_payloads)


def test_team_run_uses_shared_deadline_when_no_outer_timeout(monkeypatch) -> None:
    class FakeDispatcher:
        def __init__(self, config, skills, tool_registry=None) -> None:  # noqa: ANN001
            self.config = config
            self.skills = skills
            self.tool_registry = tool_registry

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
                "tier": "medium",
                "tokens": 1,
                "usd": 0.0,
                "wall_ms": 1,
                "output": {"summary": f"{name} complete"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_shared_deadline",
            arguments={
                "task": "Quick three-role ETH check.",
                "roles": [{"name": "fundamentals_analyst"}],
                "shared_payload": {"deadline": "5s"},
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    assert result.content[0].data["timeout_s"] == 30.0


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


def test_team_run_supports_split_analysis_and_final_output_languages(monkeypatch) -> None:
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
                "output": {"summary": f"{name} 完成中文分析"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_split_language",
            turn_id="turn-split-language",
            arguments={
                "task": "Research ETH with three analysts.",
                "roles": [{"name": "fundamentals_analyst"}, {"name": "risk_critic"}],
                "analysis_language": "Chinese",
                "output_language": "English",
                "max_parallel": 2,
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["analysis_language"] == "Chinese"
    assert data["output_language"] == "English"
    assert "English" in data["next_action"]
    assert all(payload["analysis_language"] == "Chinese" for payload in seen_payloads)
    assert all(payload["output_language"] == "English" for payload in seen_payloads)
    member_starts = [
        event for event in bus.recent()
        if event["kind"] == "team.member.start"
    ]
    assert member_starts
    assert all(
        "Role analysis language: Chinese" in event.get("assignment_prompt", "")
        for event in member_starts
    )
    assert all(
        "Final report language: English" in event.get("assignment_prompt", "")
        for event in member_starts
    )


def test_team_run_role_language_does_not_override_final_output_language(monkeypatch) -> None:
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
                "output": {"summary": f"{name} completed"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_role_language",
            turn_id="turn-role-language",
            arguments={
                "task": "Three analysts research ETH in Chinese and write the final report in English.",
                "roles": [
                    {
                        "name": "fundamentals_analyst",
                        "payload": {"symbol": "ETH", "language": "zh"},
                    },
                    {
                        "name": "risk_critic",
                        "language": "zh",
                        "payload": {"symbol": "ETH"},
                    },
                ],
                "shared_payload": {
                    "analysis_language": "Chinese",
                    "output_language": "English",
                },
                "max_parallel": 2,
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["analysis_language"] == "Chinese"
    assert data["output_language"] == "English"
    assert all(payload["analysis_language"] == "Chinese" for payload in seen_payloads)
    assert all(payload["output_language"] == "English" for payload in seen_payloads)
    member_starts = [
        event for event in bus.recent()
        if event["kind"] == "team.member.start"
    ]
    assert member_starts
    assert all(
        "Role analysis language: Chinese" in event.get("assignment_prompt", "")
        for event in member_starts
    )
    assert all(
        "Final report language: English" in event.get("assignment_prompt", "")
        for event in member_starts
    )


def test_team_run_role_language_without_final_contract_is_analysis_only(monkeypatch) -> None:
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
            return {
                "ok": True,
                "tier": "medium",
                "tokens": 7,
                "usd": 0.01,
                "wall_ms": 12,
                "output": {"summary": "completed"},
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_role_analysis_language",
            turn_id="turn-role-analysis-language",
            arguments={
                "task": "Run a two-role ETH research team.",
                "roles": [
                    {"name": "fundamentals_analyst", "payload": {"language": "zh"}},
                    {"name": "risk_critic", "payload": {"language": "zh"}},
                ],
                "max_parallel": 2,
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["output_language"] == "the original user prompt language"
    assert data["analysis_language"] == "zh"
    assert all(payload["analysis_language"] == "zh" for payload in seen_payloads)
    assert all(
        payload["output_language"] == "the original user prompt language"
        for payload in seen_payloads
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


def test_subagent_prompt_uses_analysis_language_for_split_language_role_outputs(tmp_path) -> None:
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
        {
            "ticker": "ETH",
            "analysis_language": "Chinese",
            "output_language": "English",
        },
        "",
        [],
        allowed=[],
        native_tools=[],
    )

    assert "Role analysis language: Chinese" in prompt
    assert "Final report language: English" in prompt
    assert "role conclusions in the analysis language" in prompt
    assert "Target user-visible language: English" not in prompt


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


def test_team_run_treats_partial_tool_observation_fallback_as_failure(monkeypatch) -> None:
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
                    "done": True,
                    "partial": True,
                    "quality": "tool_observation_fallback",
                    "summary": f"{name} collected tools but did not finalize",
                },
            }

    monkeypatch.setattr(agents, "SubAgentDispatcher", FakeDispatcher)

    result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_partial",
            arguments={
                "task": "Inspect NVDA",
                "roles": [{"name": "fundamentals_analyst"}],
            },
        ),
        config=object(),
        skills=object(),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["ok"] is False
    assert data["roles_succeeded"] == []
    assert data["roles_failed"] == ["fundamentals_analyst"]
    assert data["failures"][0]["error_kind"] == "tool_observation_fallback"


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


def test_subagent_run_async_returns_sync_team_result_after_team_run(
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
    metadata = {"session_id": "sess-team-async-guard"}
    turn_id = "turn-team-async-guard"
    team_result = agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="toolu_team_before_async",
            turn_id=turn_id,
            arguments={
                "task": "Build a multi-stock report.",
                "roles": [{"name": "fundamentals_analyst"}],
            },
            metadata=metadata,
        ),
        config=object(),
        skills=object(),
    )

    async_result = native_tasks.subagent_run_async_handler(
        ToolCall(
            name="subagent_run_async",
            id="toolu_async_after_team",
            turn_id=turn_id,
            arguments={
                "name": "fundamentals_analyst",
                "payload": {"prompt": "continue the team report"},
            },
            metadata=metadata,
        ),
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=object(),  # type: ignore[arg-type]
        store=store,
    )

    assert not team_result.is_error
    assert not async_result.is_error
    data = async_result.content[0].data
    assert data["status"] == "team_already_completed"
    assert data["skipped"] is True
    assert data["team_summary"]["team_run_id"] == team_result.content[0].data["team_run_id"]
    assert "do not inspect task_list/task_get/task_output again" in data["next_action"]
    assert store.list() == []


def test_role_list_surfaces_catalog_without_domain_route_guidance(tmp_path) -> None:
    result = agents.role_list_handler(
        ToolCall(name="role_list", id="toolu_roles", arguments={}),
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
    )

    assert not result.is_error
    data = result.content[0].data
    assert "Role catalog only" in data["guidance"]
    assert "public-company" not in data["guidance"]
    assert "valuation_reviewer" not in data["guidance"]
    assert "recommended_stock_research_roles" not in data
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
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.calls.append(dict(kwargs))
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

    fake_llm = FakeLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=fake_llm,
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
    assert fake_llm.calls[0]["metadata"] == {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "iteration": 0,
        "subagent": "demo_researcher",
        "strategy_id": None,
        "trigger_event_id": "trigger-1",
        "parent_call_id": "toolu-team",
        "context_scope": "subagent",
        "team_run_id": "team-1",
        "llm_attempt": 1,
    }
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


def test_subagent_prompt_keeps_team_control_fields_out_of_untrusted_payload(
    tmp_path,
) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.prompts.append(str(kwargs["prompt"]))
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=5,
                usd=0.001,
                raw='{"summary":"reviewed payload","done":true}',
                parsed={"summary": "reviewed payload", "done": True},
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
        name="investment_gurus",
        prompt_path=tmp_path / "investment_gurus.agent.md",
        prompt="Use a value-investing lens and cite evidence.",
        allowed_skills=[],
        tier="medium",
    )

    runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        turn_id="turn-1",
        parent_call_id="toolu-team",
        payload={
            "team_run_id": "team-1",
            "team_template": "market_analysis_team",
            "team_call_id": "toolu-team",
            "task_id": "role-investment_gurus",
            "task_owner": "investment_gurus",
            "task_subject": "Research NVDA with multiple roles",
            "__team_task": "Research NVDA with multiple roles",
            "__team_instructions": "Focus on long-term capital allocation.",
            "open_work_items": [
                {"content": "retrieve latest SEC filing", "status": "pending"},
                {"content": "run valuation sensitivity", "status": "pending"},
            ],
            "research_requirements": {
                "source": "parent_turn_open_work_items",
                "policy": "Complete open work items or report concrete source gaps.",
            },
            "ticker": "NVDA",
            "source_notes": "Latest filings were not available from the source.",
        },
    )

    prompt = fake_llm.prompts[0]
    assert "=== team assignment ===" in prompt
    assert "Research NVDA with multiple roles" in prompt
    assert "Focus on long-term capital allocation." in prompt
    assert "Open parent work items:" in prompt
    assert "retrieve latest SEC filing" in prompt
    assert "run valuation sensitivity" in prompt
    assert "Complete open work items or report concrete source gaps." in prompt
    assert "=== task payload ===" in prompt

    untrusted_block = prompt.split('<untrusted source="payload"', 1)[1]
    untrusted_block = untrusted_block.split("</untrusted>", 1)[0]
    assert "NVDA" in untrusted_block
    assert "source_notes" in untrusted_block
    assert "team_run_id" not in untrusted_block
    assert "team-1" not in untrusted_block
    assert "task_owner" not in untrusted_block
    assert "task_subject" not in untrusted_block
    assert "__team_task" not in untrusted_block
    assert "__team_instructions" not in untrusted_block
    assert "open_work_items" not in untrusted_block
    assert "research_requirements" not in untrusted_block
    assert "retrieve latest SEC filing" not in untrusted_block


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


def test_subagent_runtime_retries_transient_llm_error_once(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FlakyLLM:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            if self.calls == 1:
                raise LLMError(
                    "router dispatch failed: network error calling provider: "
                    "Remote end closed connection without response"
                )
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=5,
                usd=0.001,
                raw='{"summary":"recovered","done":true}',
                parsed={"summary": "recovered", "done": True},
                provider="fake",
                model="fake-model",
            )

    llm = FlakyLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=llm,
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
        payload={"task_subject": "company research"},
    )

    assert llm.calls == 2
    assert result["output"]["summary"] == "recovered"
    assert result["metrics"]["iterations"] == 1


def test_subagent_runtime_recovers_last_raw_json_tool_request(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    native_calls: list[dict] = []

    def fake_market_data(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "last": 205.1},
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

    class RawToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=3,
                    usd=0.001,
                    raw=(
                        "I will inspect sources.\n"
                        '{"skill_calls":[{"skill":"connector_list","payload":{}}]}\n'
                        "Corrected call follows.\n"
                        '{"skill_calls":[{"skill":"market_data","payload":'
                        '{"action":"get_ticker","market":"YAHOO:NVDA"}}]}'
                    ),
                    parsed={},
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=4,
                usd=0.001,
                raw='{"summary":"quote observed","done":true}',
                parsed={"summary": "quote observed", "done": True},
                provider="fake",
                model="fake-model",
            )

    llm = RawToolLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="sentiment_analyst",
        prompt_path=tmp_path / "sentiment_analyst.agent.md",
        prompt="Analyze sentiment.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert llm.calls == 2
    assert native_calls == [{"action": "get_ticker", "market": "YAHOO:NVDA"}]
    assert result["output"]["summary"] == "quote observed"


def test_subagent_runtime_settles_tool_calls_when_replan_false(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    native_calls: list[dict] = []

    def fake_market_data(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "features": {"rsi14": 54.2}},
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

    class SettleLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            self.prompts.append(str(kwargs.get("prompt") or ""))
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=3,
                    usd=0.001,
                    raw='{"skill_calls":[{"skill":"market_data","payload":{"action":"get_ticker","market":"YAHOO:NVDA"}}]}',
                    parsed={
                        "skill_calls": [
                            {
                                "skill": "market_data",
                                "payload": {
                                    "action": "get_ticker",
                                    "market": "YAHOO:NVDA",
                                },
                            }
                        ]
                    },
                    provider="fake",
                    model="fake-model",
                )
            if self.calls == 2:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=4,
                    usd=0.001,
                    raw=(
                        '{"summary":"feature check requested","replan":false,'
                        '"skill_calls":[{"skill":"market_data","payload":'
                        '{"action":"calculate_features","market":"YAHOO:NVDA"}}]}'
                    ),
                    parsed={
                        "summary": "feature check requested",
                        "replan": False,
                        "skill_calls": [
                            {
                                "skill": "market_data",
                                "payload": {
                                    "action": "calculate_features",
                                    "market": "YAHOO:NVDA",
                                },
                            }
                        ],
                    },
                    provider="fake",
                    model="fake-model",
                )
            assert "Finalization mode" in str(kwargs.get("prompt") or "")
            assert "Preferred callable tools" not in str(kwargs.get("prompt") or "")
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=4,
                usd=0.001,
                raw='{"summary":"NVDA feature summary from observed RSI","done":true}',
                parsed={
                    "summary": "NVDA feature summary from observed RSI",
                    "done": True,
                },
                provider="fake",
                model="fake-model",
            )

    llm = SettleLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="research_manager",
        prompt_path=tmp_path / "research_manager.agent.md",
        prompt="Synthesize the research.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    assert llm.calls == 3
    assert native_calls == [
        {"action": "get_ticker", "market": "YAHOO:NVDA"},
        {"action": "calculate_features", "market": "YAHOO:NVDA"},
    ]
    assert result["output"]["summary"] == "NVDA feature summary from observed RSI"
    assert "quality" not in result["output"]
    assert result["metrics"]["iterations"] == 2


def test_subagent_runtime_uses_finalization_reserve_before_next_normal_llm(
    monkeypatch,
    tmp_path,
) -> None:
    from nerya.subagents import runtime as subagent_runtime

    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(subagent_runtime.time, "monotonic", fake_monotonic)

    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    native_calls: list[dict] = []

    def fake_market_data(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        clock["now"] = 25.0
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "last": 912.5},
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

    class ReserveLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201
            self.calls += 1
            prompt = str(kwargs.get("prompt") or "")
            self.prompts.append(prompt)
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task=kwargs["task"],
                    caller=kwargs["caller"],
                    tokens=3,
                    usd=0.001,
                    raw='{"skill_calls":[{"skill":"market_data","payload":{"action":"get_ticker","market":"YAHOO:NVDA"}}]}',
                    parsed={
                        "skill_calls": [
                            {
                                "skill": "market_data",
                                "payload": {
                                    "action": "get_ticker",
                                    "market": "YAHOO:NVDA",
                                },
                            }
                        ]
                    },
                    provider="fake",
                    model="fake-model",
                )
            assert "Finalization mode" in prompt
            assert "Preferred callable tools" not in prompt
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=4,
                usd=0.001,
                raw='{"summary":"NVDA quote evidence synthesized before wall time ran out","done":true}',
                parsed={
                    "summary": (
                        "NVDA quote evidence synthesized before wall time ran out"
                    ),
                    "done": True,
                },
                provider="fake",
                model="fake-model",
            )

    llm = ReserveLLM()
    runtime = SubAgentRuntime(
        config=Config(
            paths=WorkspacePaths(root=tmp_path),
            data={"agent": {"subagents": {"max_wall_seconds": 60}}},
        ),
        skills=FakeSkills(),
        llm=llm,
        tool_registry=registry,
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
        payload={"ticker": "NVDA"},
    )

    assert llm.calls == 2
    assert native_calls == [{"action": "get_ticker", "market": "YAHOO:NVDA"}]
    assert "Finalization mode" in llm.prompts[-1]
    assert result["output"]["summary"].startswith("NVDA quote evidence")
    close_steps = [step for step in result["steps"] if step["kind"] == "close"]
    assert close_steps[-1]["detail"]["close_reason"] == "subagent_finalization_reserve"


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
        "data_api",
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
    statement_types = [
        payload.get("financial_type")
        for name, payload in native_calls
        if name == "mcp__yahoo__get_financial_statement"
    ]
    assert statement_types == []
    data_api_calls = [payload for name, payload in native_calls if name == "data_api"]
    assert data_api_calls == [
        {
            "op": "call",
            "provider": "financial_datasets",
            "action": "all_statements",
            "args": {"ticker": "NVDA", "period": "annual", "limit": 4},
            "limit": 12,
        },
        {
            "op": "call",
            "provider": "financial_datasets",
            "action": "metrics_snapshot",
            "args": {"ticker": "NVDA"},
            "limit": 20,
        },
        {
            "op": "call",
            "provider": "financial_datasets",
            "action": "filings",
            "args": {"ticker": "NVDA", "form": "10-K", "limit": 3},
            "limit": 5,
        },
    ]
    assert "prior observations" in llm.prompts[0]
    assert [call["skill"] for call in result["metrics"]["skill_calls"][:5]] == [
        "market_data",
        "data_api",
        "data_api",
        "data_api",
    ]
    assert result["output"]["data_coverage"]["has_market_data"] is True
    assert result["output"]["data_coverage"]["has_financial_statement"] is True


def test_subagent_native_tool_error_preserves_provider_action_and_detail(
    tmp_path,
) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class ToolLLM:
        def call(self, **kwargs):  # noqa: ANN201, ARG002
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:fundamental_analyst",
                tokens=3,
                usd=0.001,
                raw=(
                    '{"skill_calls":[{"skill":"data_api","payload":'
                    '{"op":"call","provider":"financial_datasets",'
                    '"action":"all_statements","args":{"ticker":"NVDA"}}}]}'
                ),
                parsed={
                    "skill_calls": [
                        {
                            "skill": "data_api",
                            "payload": {
                                "op": "call",
                                "provider": "financial_datasets",
                                "action": "all_statements",
                                "args": {"ticker": "NVDA"},
                            },
                        }
                    ]
                },
                provider="fake",
                model="fake-model",
            )

    def fake_data_api(call):  # noqa: ANN001, ANN202
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="financial dataset credential missing",
                retryable=False,
                detail={
                    "provider": "financial_datasets",
                    "action": "all_statements",
                    "args": {"ticker": "NVDA"},
                    "field": "api_key",
                },
            ),
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="data_api",
            description="test data api",
            input_schema={},
            handler=fake_data_api,
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
        llm=ToolLLM(),
        tool_registry=registry,
    )

    result = runtime.run(
        SubAgentSpec(
            name="fundamental_analyst",
            prompt_path=tmp_path / "fundamental_analyst.agent.md",
            prompt="Analyze fundamentals.",
            canonical_name="fundamentals_analyst",
            allowed_skills=[],
            tier="medium",
        ),
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA"},
    )

    rejected = result["metrics"]["rejected_actions"][0]
    assert rejected["skill"] == "data_api"
    assert rejected["payload"]["provider"] == "financial_datasets"
    assert rejected["payload"]["action"] == "all_statements"
    assert rejected["payload"]["args"]["ticker"] == "NVDA"
    assert rejected["error_kind"] == "schema_validation"
    assert rejected["error_detail"]["field"] == "api_key"
    assert rejected["retryable"] is False


def test_subagent_runtime_returns_prefetch_evidence_when_initial_llm_transient_fails(
    tmp_path,
) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FailingLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201, ARG002
            self.calls += 1
            self.prompts.append(str(kwargs.get("prompt") or ""))
            raise LLMError(
                "router dispatch failed: network error calling provider: "
                "Remote end closed connection without response"
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
        "data_api",
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
    llm = FailingLLM()
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

    assert llm.calls == 3
    assert "Finalization mode" in llm.prompts[-1]
    assert "Preferred callable tools" not in llm.prompts[-1]
    assert native_calls
    assert result["output"]["quality"] == "tool_observation_fallback"
    assert result["output"]["close_reason"] == "llm_error_after_tool_observations"
    assert result["output"]["observations"]
    assert result["output"]["data_coverage"]["has_financial_statement"] is True
    assert result["metrics"]["skill_calls"]


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


def test_final_subagent_output_keeps_substantive_raw_with_tool_calls() -> None:
    from nerya.subagents import runtime as subagent_runtime

    raw = (
        "```json\n"
        "{\n"
        '  "role": "market_data_specialist",\n'
        '  "status": "completed_with_gaps",\n'
        '  "data_inventory": {"primary": "credential_missing"},\n'
        '  "skill_calls": [{"skill": "connector_list", "payload": {}}],\n'
        '  "done": true\n'
        "}\n"
        "```"
    )

    output = subagent_runtime._final_subagent_output(
        {"skill_calls": [{"skill": "connector_list", "payload": {}}]},
        raw,
    )

    assert output["done"] is True
    assert output["quality"] == "raw_substantive_with_tool_request"
    assert "degraded" not in output


def test_subagent_runtime_recovers_from_repeated_successful_tool_request(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class RepeatingToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, **kwargs):  # noqa: ANN201, ARG002
            self.calls += 1
            if self.calls >= 3:
                assert "duplicate" in str(kwargs.get("prompt") or "").lower()
                return LLMCall(
                    tier="medium",
                    task="subagent_analysis",
                    caller="subagent:risk_critic",
                    tokens=5,
                    usd=0.001,
                    raw=(
                        '{"done":true,"summary":"NVDA risk review completed '
                        'from existing candle observations.","risk_flags":["drawdown"]}'
                    ),
                    parsed={
                        "done": True,
                        "summary": (
                            "NVDA risk review completed from existing candle observations."
                        ),
                        "risk_flags": ["drawdown"],
                    },
                    provider="fake",
                    model="fake-model",
                )
            parsed = {
                "skill_calls": [
                    {
                        "skill": "market_data",
                        "payload": {
                            "action": "get_candles",
                            "market": "NASDAQ:NVDA",
                            "interval": "1d",
                            "count": 90,
                        },
                    }
                ],
            }
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:risk_critic",
                tokens=3,
                usd=0.001,
                raw='{"skill_calls":[{"skill":"market_data","payload":{"action":"get_candles","market":"NASDAQ:NVDA","interval":"1d","count":90}}]}',
                parsed=parsed,
                provider="fake",
                model="fake-model",
            )

    native_calls: list[dict] = []

    def fake_tool(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "rows": 90},
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="market_data",
            description="test market data",
            input_schema={},
            handler=fake_tool,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    llm = RepeatingToolLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="risk_critic",
        prompt_path=tmp_path / "risk_critic.agent.md",
        prompt="Analyze risk.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"task_subject": "NVDA risk review"},
    )

    assert llm.calls == 3
    assert native_calls == [
        {
            "action": "get_candles",
            "market": "NASDAQ:NVDA",
            "interval": "1d",
            "count": 90,
        }
    ]
    assert result["output"]["done"] is True
    assert result["output"]["summary"].startswith("NVDA risk review completed")
    assert "partial" not in result["output"]
    assert result["metrics"]["rejected_actions"][0]["reason"] == "duplicate_successful_skill_call"
    assert result["metrics"]["iterations"] == 3


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


def test_subagent_runtime_marks_unstructured_output_without_evidence_as_degraded(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class IntentOnlyLLM:
        def call(self, **kwargs):  # noqa: ANN201, ARG002
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:sec_filing_reviewer",
                tokens=5,
                usd=0.001,
                raw=(
                    "I'll review the most recent filing. Let me start by "
                    "searching for the document and gathering key information."
                ),
                parsed={
                    "raw": (
                        "I'll review the most recent filing. Let me start by "
                        "searching for the document and gathering key information."
                    )
                },
                provider="fake",
                model="fake-model",
            )

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=IntentOnlyLLM(),
    )
    spec = SubAgentSpec(
        name="sec_filing_reviewer",
        prompt_path=tmp_path / "sec_filing_reviewer.agent.md",
        prompt="Review the primary filing and cite evidence.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"ticker": "NVDA", "task_subject": "latest annual filing review"},
    )

    assert result["output"]["degraded"] is True
    assert result["output"]["error_kind"] == "unstructured_output_without_evidence"
    assert result["metrics"]["skill_calls"] == []


def test_subagent_runtime_retries_unstructured_intent_before_degrading(tmp_path) -> None:
    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class IntentThenToolLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def call(self, **kwargs):  # noqa: ANN201, ARG002
            self.calls += 1
            self.prompts.append(str(kwargs.get("prompt") or ""))
            if self.calls == 1:
                return LLMCall(
                    tier="medium",
                    task="subagent_analysis",
                    caller="subagent:filing_reviewer",
                    tokens=5,
                    usd=0.001,
                    raw="I will gather the source document and then summarize it.",
                    parsed={
                        "raw": "I will gather the source document and then summarize it.",
                    },
                    provider="fake",
                    model="fake-model",
                )
            if self.calls == 2:
                assert "protocol" in self.prompts[-1].lower()
                return LLMCall(
                    tier="medium",
                    task="subagent_analysis",
                    caller="subagent:filing_reviewer",
                    tokens=5,
                    usd=0.001,
                    raw=(
                        '{"skill_calls":[{"skill":"web_search_fetch",'
                        '"payload":{"query":"company filing","max_results":1}}]}'
                    ),
                    parsed={
                        "skill_calls": [
                            {
                                "skill": "web_search_fetch",
                                "payload": {"query": "company filing", "max_results": 1},
                            }
                        ],
                    },
                    provider="fake",
                    model="fake-model",
                )
            return LLMCall(
                tier="medium",
                task="subagent_analysis",
                caller="subagent:filing_reviewer",
                tokens=5,
                usd=0.001,
                raw=(
                    '{"done":true,"summary":"Filing evidence reviewed",'
                    '"evidence":[{"url":"https://example.test/filing"}]}'
                ),
                parsed={
                    "done": True,
                    "summary": "Filing evidence reviewed",
                    "evidence": [{"url": "https://example.test/filing"}],
                },
                provider="fake",
                model="fake-model",
            )

    native_calls: list[dict] = []

    def fake_fetch(call):  # noqa: ANN001, ANN202
        native_calls.append(dict(call.arguments or {}))
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "url": "https://example.test/filing"},
        )

    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="web_search_fetch",
            description="test web search fetch",
            input_schema={},
            handler=fake_fetch,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    llm = IntentThenToolLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=FakeSkills(),
        llm=llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="filing_reviewer",
        prompt_path=tmp_path / "filing_reviewer.agent.md",
        prompt="Review the primary filing and cite evidence.",
        allowed_skills=[],
        tier="medium",
    )

    result = runtime.run(
        spec,
        trigger_event_id="trigger-1",
        session_id="sess-1",
        payload={"task_subject": "review filing"},
    )

    assert llm.calls == 3
    assert native_calls == [{"query": "company filing", "max_results": 1}]
    assert result["output"]["summary"] == "Filing evidence reviewed"
    assert result["output"]["done"] is True
    assert result["metrics"]["skill_calls"][0]["skill"] == "web_search_fetch"
    protocol_steps = [
        step for step in result["steps"]
        if step["kind"] == "observe"
        and step["detail"].get("reason") == "unstructured_output_protocol_retry"
    ]
    assert protocol_steps


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
