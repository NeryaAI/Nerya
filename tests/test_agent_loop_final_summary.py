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
    _format_timeout_evidence_snippet,
    _next_required_artifact_tool_names,
    _success_tool_result_markers,
    _wrap_external_content,
)
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import (
    TEAM_REPORT_RESULT_PROTOCOL,
    team_report_data as _team_result_data,
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


@pytest.mark.parametrize("cancel_during_summary", [False, True])
def test_team_final_synthesis_is_included_in_usage_telemetry(monkeypatch, cancel_during_summary) -> None:
    from nerya.harness.cancellation import CancelToken
    token = CancelToken()
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

    normal_call = gateway.call_messages
    def call_messages(**kwargs):
        response = normal_call(**kwargs)
        if cancel_during_summary and len(gateway.calls) == 2:
            token.cancel("stop_during_team_summary")
        return response
    monkeypatch.setattr(gateway, "call_messages", call_messages)
    loop = _loop(gateway, [_descriptor("team_run", team_handler)], max_iterations=2)
    outcome = loop.run(system="system", user_message="run the team", cancel_token=token)

    assert outcome.stop_reason == ("cancelled" if cancel_during_summary else "end_turn")
    assert outcome.aborted is cancel_during_summary
    assert (outcome.final_text == final_report) is not cancel_during_summary
    assert outcome.llm_calls == 2
    assert outcome.input_tokens_total == 40
    assert outcome.output_tokens_total == 10
    assert outcome.usd_total == pytest.approx(0.03)
    assert outcome.provider == "fake"
    assert outcome.model == "fake-summary"
    assert [call["context_scope"] for call in outcome.model_calls] == [
        "agent_loop",
        "agent_loop",
    ]
    assert gateway.calls[1]["metadata"]["turn_id"] == outcome.checkpoint.turn_id
    assert gateway.calls[1]["metadata"]["turn_id"] == gateway.calls[0]["metadata"]["turn_id"]
    assert gateway.calls[1]["metadata"]["iteration"] == 2


def test_team_evidence_is_preserved_for_the_next_model_turn() -> None:
    payload = {
        "team_run_id": "run-evidence", "status": "completed_with_failures",
        "results": [{"subagent": "analyst", "output": {
            "summary": "verified finding", "task_id": "actual-task",
            "data_coverage": {"has_data": True, "missing": ["second source"]},
        }}], "failures": [{"subagent": "critic", "error": "source unavailable"}],
    }
    gateway = _Gateway(
        _response(_tool_use("team_run"), stop_reason="tool_use"),
        _response({"type": "text", "text": "Partial evidence; source unavailable."}),
    )
    outcome = _loop(gateway, [_descriptor("team_run", lambda call: _json_result(call, payload))]).run(
        system="system", user_message="review evidence",
    )
    delivered = str(gateway.calls[1]["messages"])
    for value in ("verified finding", "actual-task", "second source", "source unavailable"):
        assert value in delivered
    assert outcome.final_text == "Partial evidence; source unavailable."
    assert len(gateway.calls) == 2


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


@pytest.mark.parametrize("phase", ["required_tool", "team_summary"])
def test_loop_preserves_caller_model_settings_in_special_phases(phase) -> None:
    settings = {
        "max_tokens": 256,
        "temperature": 0.4,
        "reasoning_effort": "high",
        "reasoning_summary": "detailed",
    }
    gateway = _Gateway(_response({"type": "text", "text": "Verified evidence."}))
    loop = _loop(
        gateway,
        [_descriptor("alpha", lambda call: _json_result(call, {"ok": True}))],
        max_iterations=1,
        required_artifacts=({"kind": "tool_result", "tool": "alpha"},),
        **settings,
    )
    if phase == "required_tool":
        loop.run(system="system", user_message="run alpha")
        assert gateway.calls[0]["tool_choice"] == {"type": "tool", "name": "alpha"}
    else:
        gateway.responses = [
            _response(_tool_use("team_run"), stop_reason="tool_use"),
            _response({"type": "text", "text": "Verified team evidence."}),
        ]
        loop = _loop(gateway, [_descriptor("team_run", lambda call: _json_result(call, {
            "team_run_id": "team-settings", "status": "completed", "results": [],
        }))], max_iterations=2, **settings)
        loop.run(system="system", user_message="use team evidence")
        assert {key: gateway.calls[1][key] for key in settings} == settings
    assert {key: gateway.calls[0][key] for key in settings} == settings


@pytest.mark.parametrize(
    ("reserve", "wall_seconds", "expect_synthesis"),
    [(30.0, 100.0, False), (0.0, 10.0, False), (180.0, 150.0, True)],
)
def test_final_synthesis_uses_configured_reserve(
    monkeypatch, reserve, wall_seconds, expect_synthesis,
) -> None:
    from types import SimpleNamespace
    from nerya.agent import loop as loop_module

    from nerya.tools import orchestrator as orchestrator_module
    clock = SimpleNamespace(time=lambda: 1_000.0, sleep=lambda _: None)
    monkeypatch.setattr(loop_module, "time", clock)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    gateway = _Gateway(
        _response(_tool_use("alpha"), stop_reason="tool_use"),
        _response({"type": "text", "text": "Verified evidence."}),
    )
    loop = _loop(
        gateway,
        [_descriptor("alpha", lambda call: _json_result(call, {"ok": True, "value": 7}))],
        max_wall_seconds=wall_seconds,
        wall_time_final_synthesis_seconds=reserve,
    )
    outcome = loop.run(system="system", user_message="collect evidence")

    assert not outcome.aborted
    assert len(gateway.calls) == 2
    assert (gateway.calls[1]["tools"] == []) is expect_synthesis
    assert gateway.calls[1]["metadata"]["text_only_final_attempt"] is expect_synthesis
    expected_deadline = 1_000.0 + wall_seconds
    if not expect_synthesis:
        expected_deadline -= min(reserve, wall_seconds / 2)
    assert gateway.calls[1]["deadline"] == expected_deadline


@pytest.mark.parametrize("retry", [False, True])
def test_required_tool_keeps_canonical_schema_and_short_budget_recovery(monkeypatch, retry):
    from copy import deepcopy
    from types import SimpleNamespace
    from nerya.agent import loop as loop_module
    from nerya.core.errors import LLMError

    from nerya.tools import orchestrator as orchestrator_module
    clock = SimpleNamespace(time=lambda: 1_000.0, sleep=lambda _: None)
    monkeypatch.setattr(loop_module, "time", clock)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    descriptor = _descriptor("alpha", lambda call: _json_result(call, {"ok": True}))
    descriptor.input_schema.update({
        "$defs": {"payload": {"type": "string", "const": "x" * 300}},
        "properties": {
            "choice": {"enum": list(range(50))},
            "optional": {"$ref": "#/$defs/payload"},
        },
        "required": ["choice"],
        "dependentRequired": {"choice": ["optional"]},
        "additionalProperties": False,
    })
    expected = deepcopy(descriptor.to_provider_tool())
    gateway = _Gateway(
        _response(_tool_use("alpha", choice=49, optional="x" * 300), stop_reason="tool_use"),
        _response({"type": "text", "text": "Verified evidence."}),
    )
    normal_call = gateway.call_messages

    def call_messages(**kwargs):
        if retry and not gateway.calls:
            gateway.calls.append(kwargs)
            raise LLMError("provider timed out")
        return normal_call(**kwargs)

    monkeypatch.setattr(gateway, "call_messages", call_messages)
    loop = _loop(
        gateway, [descriptor], max_iterations=2,
        required_artifacts=({"kind": "tool_result", "tool": "alpha"},),
        max_wall_seconds=10.0, wall_time_final_synthesis_seconds=0.0,
        action_tool_wall_reserve_seconds=0.0, llm_retry_attempts=1,
    )
    outcome = loop.run(system="system", user_message="run alpha")

    assert outcome.final_text == "Verified evidence."
    assert outcome.tool_calls == 1
    assert outcome.error_count == 0
    assert gateway.calls[0]["tools"] == [expected]
    if retry:
        assert gateway.calls[1]["tools"] == [expected]
        assert outcome.extra_llm_attempts_by_reason == {"transient_required_tool_retry": 1}
    assert descriptor.to_provider_tool() == expected


@pytest.mark.parametrize("text", [
    "| Metric | Value |\n| --- | --- |\n| Result | Verified |",
    "Evidence\n" + "A verified observation without artificial punctuation " * 5,
    "Findings\n- Verified first finding\n- Verified second finding",
])
def test_final_output_does_not_guess_completion_from_prose_style(text):
    gateway = _Gateway(_response({"type": "text", "text": text}))
    outcome = _loop(gateway, []).run(system="system", user_message="report evidence")
    assert outcome.final_text == text
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("max_iterations", [1, 3])
def test_approval_pause_outranks_team_finalization_in_the_same_batch(max_iterations):
    from nerya.tools.approval_contracts import APPROVAL_PENDING_REASON
    from nerya.tools.types import ToolError, ToolErrorKind

    def team_handler(call):
        return ToolResult.from_json(
            tool_use_id=call.id, name=call.name,
            data={
                "ok": True, "status": "completed", "team_run_id": "team-with-pause",
                "roles_succeeded": ["analyst"],
                "results": [{"subagent": "analyst", "output": {"summary": "verified"}}],
            },
            semantic_success=True,
        )

    def approval_handler(call):
        return ToolResult.from_error(
            tool_use_id=call.id, name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_PENDING, message="await operator"),
        )

    gateway = _Gateway(
        _response(
            _tool_use("team_run", call_id="team-1"),
            _tool_use("approval_probe", call_id="approval-1"),
            stop_reason="tool_use",
        ),
        _response({"type": "text", "text": "Must not synthesize over a pending approval."}),
    )
    loop = _loop(gateway, [
        _descriptor("team_run", team_handler),
        _descriptor("approval_probe", approval_handler),
    ], max_iterations=max_iterations)
    outcome = loop.run(system="system", user_message="review and request approval")

    assert outcome.stop_reason == APPROVAL_PENDING_REASON
    assert outcome.transition_reason == APPROVAL_PENDING_REASON
    assert len(gateway.calls) == 1
    assert outcome.checkpoint is not None
    assert not outcome.checkpoint.resumable
    assert outcome.checkpoint.resume_block_reason == APPROVAL_PENDING_REASON
    result_ids = {
        block["tool_use_id"]
        for message in outcome.transcript
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    }
    assert result_ids == {"team-1", "approval-1"}
    assert not outcome.aborted
    assert not outcome.final_text


def test_required_artifact_waits_for_approval_instead_of_finalizing_a_gap():
    from nerya.tools.approval_contracts import APPROVAL_PENDING_REASON
    from nerya.tools.types import ToolError, ToolErrorKind

    def handler(call):
        return ToolResult.from_error(
            tool_use_id=call.id, name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_PENDING, message="await operator"),
        )

    gateway = _Gateway(_response(_tool_use("alpha"), stop_reason="tool_use"))
    loop = _loop(
        gateway, [_descriptor("alpha", handler)], max_iterations=1,
        required_artifacts=({"kind": "tool_result", "tool": "alpha"},),
    )
    outcome = loop.run(system="system", user_message="request approval")

    assert outcome.stop_reason == APPROVAL_PENDING_REASON
    assert outcome.transition_reason == APPROVAL_PENDING_REASON
    assert not outcome.aborted
    assert not outcome.final_text
    assert outcome.checkpoint.resume_block_reason == APPROVAL_PENDING_REASON
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("phase", ["provider_error", "provider_response", "tool_result"])
def test_cancel_at_each_execution_boundary_stops_new_work(monkeypatch, phase):
    from nerya.core.errors import LLMError
    from nerya.harness.cancellation import CancelToken

    token = CancelToken()
    executed = []
    gateway = _Gateway(MessagesResponse(
        content=[_tool_use("alpha")], stop_reason="tool_use",
        usage={"input_tokens": 10, "output_tokens": 2},
    ))
    normal_call = gateway.call_messages

    def call_messages(**kwargs):
        if phase == "provider_error":
            gateway.calls.append(kwargs)
            token.cancel("operator_stop")
            raise LLMError("provider timed out")
        response = normal_call(**kwargs)
        if phase == "provider_response":
            token.cancel("operator_stop")
        return response

    def handler(call):
        executed.append(call.id)
        token.cancel("operator_stop")
        return _json_result(call, {"ok": True, "value": 7})

    monkeypatch.setattr(gateway, "call_messages", call_messages)
    loop = _loop(gateway, [_descriptor("alpha", handler)], llm_retry_base_delay=0.0)
    outcome = loop.run(system="system", user_message="inspect", cancel_token=token)

    assert outcome.stop_reason == "cancelled"
    assert outcome.aborted
    assert len(gateway.calls) == 1
    assert len(executed) == (1 if phase == "tool_result" else 0)
    assert outcome.input_tokens_total == (0 if phase == "provider_error" else 10)
