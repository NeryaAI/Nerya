from __future__ import annotations

import pytest

from nerya.agent.kernel import AgentKernel, _loop_config_from_config
from nerya.agent.loop import (
    LoopConfig,
    WorkspaceNativeAgentLoop,
    _build_team_run_final_report,
)
from nerya.api.routes_agent import _with_turn_limit_overrides
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import PermissionScope, RiskLevel, ToolDescriptor, ToolResult


pytestmark = pytest.mark.smoke


class _ToolOnlyGateway:
    def __init__(self, *, stop_reason: str = "tool_use") -> None:
        self.calls = 0
        self.stop_reason = stop_reason

    def call_messages(self, **_kwargs):  # noqa: ANN001
        self.calls += 1
        return MessagesResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": f"toolu_{self.calls}",
                    "name": "read_status",
                    "input": {},
                }
            ],
            stop_reason=self.stop_reason,
        )


def _loop(gateway: _ToolOnlyGateway) -> WorkspaceNativeAgentLoop:
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="read_status",
            description="Read status.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda call: ToolResult.from_text(
                tool_use_id=call.id,
                name=call.name,
                text="still working",
            ),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            auto_approve=True,
        )
    )
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    orchestrator = ToolOrchestrator(registry=registry, executor=executor)
    return WorkspaceNativeAgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        orchestrator=orchestrator,
        config=LoopConfig(max_iterations=1),
    )


def test_max_iterations_without_final_text_gets_deterministic_summary() -> None:
    gateway = _ToolOnlyGateway(stop_reason="end_turn")

    outcome = _loop(gateway).run(system="system", user_message="run")

    assert outcome.aborted is True
    assert outcome.abort_reason == "max_iterations"
    assert outcome.final_text
    assert "Turn stopped before a complete model-written final answer" in outcome.final_text
    assert "tool calls: 1" in outcome.final_text
    assert "tool errors: 0" in outcome.final_text
    assert "Next: resume the same turn" in outcome.final_text
    assert outcome.blocks[-1].block["kind"] == "text"


def test_projected_tool_trace_keeps_payload_and_output() -> None:
    outcome = _loop(_ToolOnlyGateway(stop_reason="end_turn")).run(
        system="system",
        user_message="run",
    )

    actions, tool_trace = AgentKernel._project_blocks(outcome)

    assert actions[0]["action"] == "read_status"
    assert actions[0]["payload"] == {}
    assert tool_trace[0]["action"] == "read_status"
    assert tool_trace[0]["payload"] == {}
    assert tool_trace[0]["result"] == "still working"


def test_team_run_tool_result_final_synthesis_receives_original_prompt() -> None:
    class TeamGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.requests = []

        def call_messages(self, **kwargs):  # noqa: ANN001
            self.calls += 1
            self.requests.append(kwargs)
            if self.calls == 2:
                return MessagesResponse(
                    content=[
                        {
                            "type": "text",
                            "text": "最终中文总结：NVDA 基本面强，但估值和集中度需要控制。",
                        }
                    ],
                    stop_reason="end_turn",
                )
            return MessagesResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_team",
                        "name": "team_run",
                        "input": {"task": "Analyze NVDA"},
                    }
                ],
                stop_reason="tool_use",
            )

    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="team_run",
            description="Run team.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda call: ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "ok": True,
                    "status": "completed",
                    "team_run_id": "team-test",
                    "task": "Analyze NVDA",
                    "roles_succeeded": ["fundamentals_analyst"],
                    "roles_failed": [],
                    "results": [
                        {
                            "subagent": "fundamentals_analyst",
                            "output": {"summary": "Revenue and margin analysis complete."},
                        }
                    ],
                    "aggregated": {"summary": "NVDA report synthesis."},
                    "tokens_total": 10,
                    "usd_total": 0.01,
                },
            ),
            risk=RiskLevel.EXEC,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            auto_approve=True,
        )
    )
    gateway = TeamGateway()
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    loop = WorkspaceNativeAgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        orchestrator=ToolOrchestrator(registry=registry, executor=executor),
        config=LoopConfig(max_iterations=24),
    )

    outcome = loop.run(system="system", user_message="帮我启动AgentTeam分析英伟达")

    assert outcome.stop_reason == "end_turn"
    assert gateway.calls == 2
    assert outcome.final_text == "最终中文总结：NVDA 基本面强，但估值和集中度需要控制。"
    assert "# AgentTeam 完整研报" not in outcome.final_text
    synthesis_messages = gateway.requests[1]["messages"]
    assert len(synthesis_messages) == 1
    assert gateway.requests[1]["tools"] == []
    synthesis_prompt = synthesis_messages[-1]["content"]
    assert "Original user prompt:" in synthesis_prompt
    assert "帮我启动AgentTeam分析英伟达" in synthesis_prompt
    assert "AgentTeam conclusions" in synthesis_prompt
    assert "NVDA report synthesis" in synthesis_prompt
    assert "Revenue and margin analysis complete" in synthesis_prompt
    assert "Do not claim that all required data was obtained" in synthesis_prompt
    assert "data gaps" in synthesis_prompt
    assert "data_coverage" in synthesis_prompt


def test_team_run_final_report_renders_structured_role_outputs_as_markdown() -> None:
    report = _build_team_run_final_report(
        {
            "ok": True,
            "status": "completed",
            "team_run_id": "team-nvda",
            "task": "Analyze NVDA",
            "roles_succeeded": ["bull_researcher", "risk_critic", "research_manager"],
            "roles_failed": [],
            "results": [
                {
                    "subagent": "bull_researcher",
                    "output": {
                        "bull_points": [
                            {
                                "claim": "Data center growth remains strong",
                                "evidence": "Revenue grew 65% year over year.",
                                "confidence": 0.9,
                            }
                        ],
                        "confidence": 0.9,
                    },
                },
                {
                    "subagent": "risk_critic",
                    "output": {
                        "verdict": "approve_with_reductions",
                        "reasons": [
                            "Single-name concentration should stay capped.",
                        ],
                        "recommended_size_pct": 0.05,
                    },
                },
                {
                    "subagent": "research_manager",
                    "output": "NVDA has strong AI infrastructure momentum, but valuation risk requires sizing discipline.",
                },
            ],
            "aggregated": {
                "summary": '{"bull_researcher": {"summary": "{\\"bull_points\\": []}"}}',
                "truncated": True,
            },
            "tokens_total": 100,
            "usd_total": 0.02,
        }
    )

    assert "## Synthesis" in report
    assert "NVDA has strong AI infrastructure momentum" in report
    assert "### bull_researcher (bull researcher)" in report
    assert "#### bull points" in report
    assert "Data center growth remains strong" in report
    assert "recommended size pct**: 5.0%" in report
    assert '{"bull_points"' not in report
    assert '\\"claim\\"' not in report


def test_team_run_final_report_fallback_keeps_schema_labels_generic() -> None:
    report = _build_team_run_final_report(
        {
            "ok": True,
            "status": "completed",
            "team_run_id": "team-nvda",
            "task": "简短分析英伟达 NVDA",
            "roles_succeeded": ["fundamentals_analyst"],
            "roles_failed": [],
            "results": [
                {
                    "subagent": "fundamentals_analyst",
                    "output": {
                        "red_flags": ["客户集中度较高"],
                        "stop_suggestions": [{"symbol": "NVDA", "stop": 171}],
                        "rating_bias": "positive",
                        "evidence": [{"source": "Yahoo Finance", "period": "FY2026"}],
                        "confidence": 0.8,
                    },
                }
            ],
            "aggregated": {
                "subagents": {
                    "fundamentals_analyst": {"summary": "财务质量较强"}
                },
                "avg_confidence": 0.8,
            },
            "tokens_total": 10,
            "usd_total": 0.01,
        }
    )

    assert "subagents" in report
    assert "fundamentals analyst" in report
    assert "red flags" in report
    assert "stop suggestions" in report
    assert "rating bias" in report
    assert "period" in report
    assert "风险提示" not in report
    assert "止损建议" not in report
    assert "评级倾向" not in report


def test_native_loop_config_inherits_legacy_harness_limits(tmp_path) -> None:
    cfg = Config(
        paths=WorkspacePaths(root=tmp_path),
        data={
            "agent": {
                "harness": {
                    "max_iterations": 60,
                    "max_tool_calls": 200,
                    "max_wall_seconds": 1200,
                }
            }
        },
    )

    loop_cfg = _loop_config_from_config(cfg)

    assert loop_cfg.max_iterations == 60
    assert loop_cfg.max_total_tool_calls == 200
    assert loop_cfg.max_wall_seconds == 1200


def test_run_turn_limit_overrides_create_native_budget_clone(tmp_path) -> None:
    cfg = Config(
        paths=WorkspacePaths(root=tmp_path),
        data={
            "agent": {
                "harness": {
                    "max_iterations": 60,
                    "max_tool_calls": 200,
                    "max_wall_seconds": 1200,
                }
            }
        },
    )

    run_cfg = _with_turn_limit_overrides(
        cfg,
        {
            "runtime_limits": {
                "max_iterations": 90,
                "max_total_tool_calls": 300,
                "max_wall_seconds": 1800,
            }
        },
    )
    loop_cfg = _loop_config_from_config(run_cfg)

    assert loop_cfg.max_iterations == 90
    assert loop_cfg.max_total_tool_calls == 300
    assert loop_cfg.max_wall_seconds == 1800
    assert cfg.get("agent.native.max_iterations") is None
