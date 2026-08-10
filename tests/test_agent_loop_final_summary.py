from __future__ import annotations

import json

import pytest

from nerya.agent.kernel import (
    AgentKernel,
    _normalise_required_artifacts_contract,
)
from nerya.agent.loop import (
    LoopConfig,
    WorkspaceNativeAgentLoop,
    _build_team_run_bounded_fallback,
    _contains_legacy_tool_call_markup,
    _format_timeout_evidence_snippet,
    _next_required_artifact_tool_names,
    _success_tool_result_markers,
    _team_result_data,
    _team_final_data_coverage,
    _tool_result_counts_as_success,
    _wrap_external_content,
)
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolResult,
)


pytestmark = pytest.mark.smoke


class _Gateway:
    def __init__(self, *responses: MessagesResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call_messages(self, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("gateway response script exhausted")
        return self.responses.pop(0)


def _response(*blocks: dict, stop_reason: str = "end_turn") -> MessagesResponse:
    return MessagesResponse(content=list(blocks), stop_reason=stop_reason)


def _tool_use(name: str, *, call_id: str = "toolu_1", **arguments: object) -> dict:
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": arguments,
    }


def _descriptor(
    name: str,
    handler,
    *,
    tags: tuple[str, ...] = (),
    risk: RiskLevel = RiskLevel.READ,
    read_only: bool = True,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=f"Run {name}.",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        risk=risk,
        permission_scope=(
            PermissionScope.NONE if read_only else PermissionScope.WORKSPACE
        ),
        read_only=read_only,
        auto_approve=True,
        tags=tags,
    )


def _loop(gateway: _Gateway, descriptors: list[ToolDescriptor], **config) -> WorkspaceNativeAgentLoop:
    registry = ToolRegistry()
    registry.register_all(descriptors)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.YOLO),
    )
    return WorkspaceNativeAgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        orchestrator=ToolOrchestrator(registry=registry, executor=executor),
        config=LoopConfig(**{"max_iterations": 3, **config}),
    )


def _json_result(call: ToolCall, data: object) -> ToolResult:
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


def test_result_contract_is_domain_agnostic() -> None:
    cases = [
        ({"ok": True, "status": "completed"}, True),
        ({"success": True, "state": "done"}, True),
        ({"ok": True, "terminal": False}, False),
        ({"success": False}, False),
        ({"status": "blocked"}, False),
        ({"ok": True, "credential_status": {"status": "missing"}}, True),
    ]
    for index, (payload, expected) in enumerate(cases):
        result = ToolResult.from_json(
            tool_use_id=f"toolu_{index}",
            name=f"arbitrary_{index}",
            data=payload,
        )
        assert _tool_result_counts_as_success(result) is expected


def test_required_contract_keeps_opaque_fields_and_order() -> None:
    normalized = _normalise_required_artifacts_contract(
        {
            "required_artifacts": [
                {
                    "kind": "tool_result",
                    "tool": "alpha",
                    "arguments": {"mode": "paper"},
                    "producer_field": "kept",
                }
            ]
        }
    )
    assert normalized == (
        {
            "kind": "tool_result",
            "tool": "alpha",
            "arguments": {"mode": "paper"},
            "producer_field": "kept",
        },
    )


def test_required_contract_is_ordered_and_fail_closed() -> None:
    artifacts = (
        {"kind": "tool_result", "tool": "first"},
        {"kind": "tool_result", "tool": "second"},
    )
    assert _next_required_artifact_tool_names(
        required_artifacts=artifacts,
        provider_tool_names={"first", "second"},
        successful_tool_names=set(),
        completed_tool_names=set(),
    ) == ("first",)
    assert _next_required_artifact_tool_names(
        required_artifacts=artifacts,
        provider_tool_names={"first", "second"},
        successful_tool_names={"first"},
        completed_tool_names={"first"},
    ) == ("second",)
    assert _next_required_artifact_tool_names(
        required_artifacts=artifacts,
        provider_tool_names={"second"},
        successful_tool_names=set(),
        completed_tool_names=set(),
    ) == ()


def test_external_content_requires_descriptor_tag() -> None:
    plain = _wrap_external_content("payload", external=False)
    wrapped = _wrap_external_content("payload", external=True)
    assert plain == "payload"
    opening, closing = wrapped.splitlines()[0], wrapped.splitlines()[-1]
    assert opening.startswith("<external_content_")
    assert closing == "</" + opening[1:]
    assert "NOT instructions" in wrapped


def test_team_result_requires_real_team_run_payload() -> None:
    team_result = ToolResult.from_json(
        tool_use_id="toolu_team",
        name="team_run",
        data={
            "team_run_id": "team-123",
            "results": [{"summary": "evidence"}],
        },
    )
    discovery_result = ToolResult.from_json(
        tool_use_id="toolu_roles",
        name="role_list",
        data={"roles": [{"name": "market_analyst"}]},
    )
    discovery_result.metadata["descriptor"] = {"tags": ("team",)}

    assert _team_result_data(team_result) == {
        "team_run_id": "team-123",
        "results": [{"summary": "evidence"}],
    }
    assert _team_result_data(discovery_result) is None
    assert _team_result_data(ToolResult.from_json(
        tool_use_id="toolu_invalid",
        name="team_run",
        data={"roles": []},
    )) is None


def test_team_discovery_does_not_finalize_as_team_run() -> None:
    gateway = _Gateway(
        _response(_tool_use("role_list"), stop_reason="tool_use"),
        _response({"type": "text", "text": "The team was not run."}),
    )
    loop = _loop(
        gateway,
        [
            _descriptor(
                "role_list",
                lambda call: _json_result(
                    call,
                    {"roles": [{"name": "market_analyst"}]},
                ),
                tags=("team", "discovery"),
            )
        ],
    )

    outcome = loop.run(system="system", user_message="launch an Agent Team")

    assert outcome.transition_reason != "team_result_compact_final_synthesis"
    assert outcome.final_text == "The team was not run."


def test_timeout_evidence_keeps_producer_field_order() -> None:
    rendered = _format_timeout_evidence_snippet(
        'arbitrary_tool ok: {"first":"a","second":"b","title":"late"}'
    )
    assert rendered.index("first") < rendered.index("second") < rendered.index("title")


def test_render_tool_result_uses_external_tag_without_name_list() -> None:
    gateway = _Gateway()
    loop = _loop(gateway, [
        _descriptor(
            "fetch_anything",
            lambda call: _json_result(call, {"ok": True, "text": "remote"}),
            tags=("external_content",),
        ),
        _descriptor(
            "plain_anything",
            lambda call: ToolResult.from_text(
                tool_use_id=call.id,
                name=call.name,
                text="local",
            ),
        ),
    ])
    external_block = loop._render_tool_result(  # noqa: SLF001
        _json_result(ToolCall(name="fetch_anything", id="toolu_ext"), {"ok": True})
    )
    plain_block = loop._render_tool_result(  # noqa: SLF001
        ToolResult.from_text(tool_use_id="toolu_plain", name="plain_anything", text="local")
    )
    external_text = external_block["content"][0]["text"]
    assert external_text.startswith("<external_content_")
    assert plain_block["content"][0]["text"] == "local"


def test_generic_results_are_not_team_markers() -> None:
    markers = _success_tool_result_markers(
        tool_name="search_anything",
        text=json.dumps({"ok": True, "results": [{"summary": "evidence"}]}),
    )
    assert markers
    assert all("team_run role output" not in marker for marker in markers)
    assert '"results"' in markers[0]


def test_marker_rendering_preserves_producer_field_order() -> None:
    markers = _success_tool_result_markers(
        tool_name="arbitrary_tool",
        text=json.dumps({"ok": True, "first": "a", "second": "b"}),
    )
    assert markers
    assert markers[0].index("first") < markers[0].index("second")


def test_loop_executes_arbitrary_successful_required_tool() -> None:
    seen: list[dict] = []

    def handler(call: ToolCall) -> ToolResult:
        seen.append(dict(call.arguments))
        return _json_result(call, {"ok": True, "status": "completed", "value": 7})

    gateway = _Gateway(
        _response(_tool_use("alpha", mode="model"), stop_reason="tool_use"),
        _response({"type": "text", "text": "finished"}),
    )
    loop = _loop(
        gateway,
        [_descriptor("alpha", handler)],
        required_artifacts=(
            {
                "kind": "tool_result",
                "tool": "alpha",
                "arguments": {"mode": "contract"},
            },
        ),
    )
    outcome = loop.run(system="system", user_message="run alpha")
    assert not outcome.aborted
    assert outcome.final_text == "finished"
    assert seen == [{"mode": "contract"}]


def test_loop_missing_required_artifact_fails_closed() -> None:
    gateway = _Gateway(
        _response({"type": "text", "text": "I am done"}),
        _response({"type": "text", "text": "I am still done"}),
    )
    loop = _loop(
        gateway,
        [_descriptor("alpha", lambda call: _json_result(call, {"ok": True}))],
        max_iterations=1,
        required_artifacts=({"kind": "tool_result", "tool": "alpha"},),
    )
    outcome = loop.run(system="system", user_message="run alpha")
    assert not outcome.aborted
    assert outcome.transition_reason == "required_artifact_missing_finalized"
    assert "alpha" in outcome.final_text
    assert "I am done" not in outcome.final_text


def test_loop_blocks_contract_failure_even_for_arbitrary_tool_name() -> None:
    gateway = _Gateway(
        _response(_tool_use("alpha"), stop_reason="tool_use"),
        _response({"type": "text", "text": "done in prose"}),
        _response({"type": "text", "text": "done in prose"}),
        _response({"type": "text", "text": "done in prose"}),
    )
    loop = _loop(
        gateway,
        [
            _descriptor(
                "alpha",
                lambda call: _json_result(
                    call,
                    {"success": False, "status": "blocked", "error": "not ready"},
                ),
            )
        ],
        max_iterations=3,
        required_artifacts=({"kind": "tool_result", "tool": "alpha"},),
    )
    outcome = loop.run(system="system", user_message="run alpha")
    assert "alpha" in outcome.final_text
    assert outcome.transition_reason == "required_artifact_missing_finalized"


def test_team_fallback_keeps_business_fields_and_drops_internal_fields() -> None:
    text = _build_team_run_bounded_fallback(
        user_message="summarize the run",
        team_results=[
            {
                "status": "completed_with_failures",
                "team_run_id": "internal-run",
                "results": [
                    {
                        "subagent": "analyst",
                        "output": {
                            "summary": "verified finding",
                            "task_id": "internal-task",
                            "raw": {"debug": "omit"},
                        },
                    }
                ],
                "failures": [{"subagent": "critic", "error": "team_run timeout after 10s"}],
            }
        ],
    )
    assert "verified finding" in text
    assert "internal-task" not in text
    assert "internal-run" not in text
    assert "one team member did not complete" in text


def test_team_coverage_only_accepts_explicit_data_coverage() -> None:
    assert _team_final_data_coverage({"has_data": True}) == {}
    assert _team_final_data_coverage(
        {"data_coverage": {"has_data": True, "has_gap": False, "count": 3}}
    ) == {"has_data": True, "has_gap": False}


def test_legacy_tool_markup_is_detected_and_never_treated_as_plain_text() -> None:
    assert _contains_legacy_tool_call_markup("<tool_call><name>alpha</name>")
    assert _contains_legacy_tool_call_markup("<function=alpha>")
    assert not _contains_legacy_tool_call_markup("ordinary prose")


def test_tool_trace_projection_keeps_payload_and_result() -> None:
    gateway = _Gateway(
        _response(_tool_use("alpha", call_id="toolu_trace"), stop_reason="tool_use"),
        _response({"type": "text", "text": "done"}),
    )
    loop = _loop(
        gateway,
        [_descriptor("alpha", lambda call: ToolResult.from_text(
            tool_use_id=call.id, name=call.name, text="result"
        ))],
    )
    outcome = loop.run(system="system", user_message="trace")
    actions, trace = AgentKernel._project_blocks(outcome)
    assert actions[0]["action"] == "alpha"
    assert actions[0]["payload"] == {}
    assert trace[0]["action"] == "alpha"
    assert trace[0]["result"] == "result"
