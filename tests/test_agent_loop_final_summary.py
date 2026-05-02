from __future__ import annotations

import pytest

from nerya.agent.loop import LoopConfig, WorkspaceNativeAgentLoop
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
