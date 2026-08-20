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
    _format_timeout_evidence_snippet,
    _next_required_artifact_tool_names,
    _success_tool_result_markers,
    _team_result_data,
    _team_final_data_coverage,
    _wrap_external_content,
)
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import (
    TEAM_REPORT_RESULT_PROTOCOL,
    result_counts_as_success,
)
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
        assert result_counts_as_success(result) is expected


def test_explicit_semantic_success_overrides_legacy_payload_inference() -> None:
    forced_failure = ToolResult.from_json(
        tool_use_id="toolu_false",
        name="producer_owned",
        data={"ok": True, "status": "completed"},
        semantic_success=False,
    )
    forced_success = ToolResult.from_json(
        tool_use_id="toolu_true",
        name="producer_owned",
        data={"success": False, "status": "failed"},
        semantic_success=True,
    )

    assert result_counts_as_success(forced_failure) is False
    assert result_counts_as_success(forced_success) is True
    assert forced_failure.asdict()["semantic_success"] is False
    assert forced_success.asdict()["semantic_success"] is True


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
    protocol_result = ToolResult.from_json(
        tool_use_id="toolu_protocol",
        name="parallel_committee_plugin",
        data={
            "team_run_id": "team-protocol",
            "results": [{"summary": "plugin evidence"}],
        },
        result_protocol=TEAM_REPORT_RESULT_PROTOCOL,
    )
    wrong_protocol_result = ToolResult.from_json(
        tool_use_id="toolu_wrong_protocol",
        name="team_run",
        data={
            "team_run_id": "team-wrong",
            "results": [{"summary": "must not be reclassified"}],
        },
        result_protocol="plugin.other.v1",
    )

    assert _team_result_data(team_result) == {
        "team_run_id": "team-123",
        "results": [{"summary": "evidence"}],
    }
    assert _team_result_data(protocol_result) == {
        "team_run_id": "team-protocol",
        "results": [{"summary": "plugin evidence"}],
    }
    assert protocol_result.asdict()["result_protocol"] == TEAM_REPORT_RESULT_PROTOCOL
    assert _team_result_data(discovery_result) is None
    assert _team_result_data(wrong_protocol_result) is None
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


def test_unoffered_only_tool_call_retries_without_execution() -> None:
    executed: list[str] = []
    gateway = _Gateway(
        _response(_tool_use("hidden_tool"), stop_reason="tool_use"),
        _response({"type": "text", "text": "Recovered with visible context."}),
    )
    loop = _loop(
        gateway,
        [
            _descriptor(
                "visible_tool",
                lambda call: executed.append(call.name) or _json_result(
                    call,
                    {"ok": True},
                ),
            )
        ],
        max_iterations=2,
    )

    outcome = loop.run(system="system", user_message="use a tool")

    assert outcome.final_text == "Recovered with visible context."
    assert executed == []
    assert outcome.tool_calls == 1
    assert outcome.error_count == 1
    assert len(gateway.calls) == 2
    retry_messages = gateway.calls[1]["messages"]
    assert any(
        "not exposed in this iteration" in str(message.get("content") or "")
        for message in retry_messages
        if isinstance(message, dict)
    )
    assert any(
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and "hidden_tool" in str(message.get("content") or "")
        for message in retry_messages
    )
    assert any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and "toolu_1" in str(message.get("content") or "")
        and "permission_denied" in str(message.get("content") or "")
        for message in retry_messages
    )


def test_unoffered_only_tool_call_blocks_at_iteration_limit() -> None:
    gateway = _Gateway(
        _response(_tool_use("hidden_tool"), stop_reason="tool_use"),
    )
    loop = _loop(
        gateway,
        [_descriptor("visible_tool", lambda call: _json_result(call, {"ok": True}))],
        max_iterations=1,
    )

    outcome = loop.run(system="system", user_message="use a tool")

    assert outcome.transition_reason == "provider_unoffered_tool_blocked"
    assert "hidden_tool" in outcome.final_text
    assert "not exposed" in outcome.final_text
    assert outcome.tool_calls == 1
    assert outcome.error_count == 1
    assert any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and "permission_denied" in str(message.get("content") or "")
        for message in outcome.transcript
    )


def test_mixed_offered_and_unoffered_calls_preserve_result_pairing() -> None:
    executed: list[str] = []

    def handler(call: ToolCall) -> ToolResult:
        executed.append(call.name)
        return _json_result(call, {"ok": True, "value": 7})

    gateway = _Gateway(
        _response(
            _tool_use("alpha", call_id="toolu_allowed"),
            _tool_use("hidden_tool", call_id="toolu_hidden"),
            stop_reason="tool_use",
        ),
        _response({"type": "text", "text": "Used the allowed evidence."}),
    )
    loop = _loop(gateway, [_descriptor("alpha", handler)], max_iterations=2)

    outcome = loop.run(system="system", user_message="collect evidence")

    assert outcome.final_text == "Used the allowed evidence."
    assert executed == ["alpha"]
    assert outcome.tool_calls == 2
    assert outcome.error_count == 1
    result_messages = [
        message
        for message in outcome.transcript
        if isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in message["content"]
        )
    ]
    assert result_messages
    result_blocks = result_messages[0]["content"]
    assert [block["tool_use_id"] for block in result_blocks] == [
        "toolu_allowed",
        "toolu_hidden",
    ]
    hidden_result = result_blocks[1]
    assert hidden_result["is_error"] is True
    assert "permission_denied" in str(hidden_result["content"])


def test_team_final_synthesis_is_included_in_usage_telemetry() -> None:
    final_report = "Verified team evidence supports a bounded conclusion."
    gateway = _Gateway(
        MessagesResponse(
            content=[_tool_use("team_run", call_id="toolu_team")],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 2},
            provider="fake",
            model="fake-main",
            usd_cost=0.01,
        ),
        MessagesResponse(
            content=[{"type": "text", "text": final_report}],
            stop_reason="end_turn",
            usage={"input_tokens": 30, "output_tokens": 8},
            provider="fake",
            model="fake-summary",
            usd_cost=0.02,
        ),
    )

    def team_handler(call: ToolCall) -> ToolResult:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": True,
                "status": "completed",
                "team_run_id": "team-1",
                "roles_succeeded": ["analyst"],
                "results": [
                    {
                        "subagent": "analyst",
                        "output": {"summary": "verified evidence"},
                    }
                ],
            },
            semantic_success=True,
        )

    loop = _loop(gateway, [_descriptor("team_run", team_handler)], max_iterations=2)
    outcome = loop.run(system="system", user_message="run the team")

    assert outcome.final_text == final_report
    assert outcome.llm_calls == 2
    assert outcome.input_tokens_total == 40
    assert outcome.output_tokens_total == 10
    assert outcome.usd_total == pytest.approx(0.03)
    assert outcome.provider == "fake"
    assert outcome.model == "fake-summary"
    assert [call["context_scope"] for call in outcome.model_calls] == [
        "agent_loop",
        "team_final_synthesis",
    ]


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
