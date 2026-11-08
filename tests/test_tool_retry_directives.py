from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.agent.tool_projection import project_tool_results
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataActionSpec, DataApiError
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.execution_contracts import execution_unknown_result
from nerya.tools.types import ToolCall, ToolError, ToolErrorKind, ToolResult


pytestmark = pytest.mark.smoke


def _loop() -> WorkspaceNativeAgentLoop:
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root="/tmp/nerya-tool-retry-directives")
    return loop


def _render(result: ToolResult) -> str:
    return _loop()._render_tool_result(result)["content"][0]["text"]


def _error(
    kind: ToolErrorKind,
    *,
    retryable: bool | None = None,
    recovery_hint: dict | None = None,
    detail: dict | None = None,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id="retry-directive",
        name="fixture_tool",
        error=ToolError(
            kind=kind,
            message="fixture failure",
            retryable=retryable,
            recovery_hint=recovery_hint or {},
            detail=detail or {},
        ),
    )


def test_data_api_producer_failure_is_rendered_without_retry_and_preserves_event_raw_error():
    registry = DataApiRegistry()

    def fail(_context, _args):
        raise DataApiError(
            "Discovery deadline exhausted",
            kind="provider_error",
            detail={
                "provider": "uniswap",
                "action": "pool_discover",
                "stage": "deadline",
                "request_count": 30,
            },
            retryable=False,
        )

    registry.register_action(
        DataActionSpec(
            provider="uniswap",
            action="pool_discover",
            title="fixture discovery",
            description="bounded fixture",
            input_schema={"type": "object", "additionalProperties": False},
            handler=fail,
        )
    )
    result = data_api_handler(
        ToolCall(
            name="data_api",
            arguments={
                "op": "call",
                "provider": "uniswap",
                "action": "pool_discover",
                "args": {},
            },
        ),
        registry=registry,
    )

    assert result.is_error
    assert result.error.retryable is False
    loop = _loop()
    projected = project_tool_results(
        [result],
        render_tool_result=loop._render_tool_result,
        rendered_tool_result_text=loop._rendered_tool_result_text,
    )
    visible = projected.transcript_blocks[0]["content"][0]["text"]
    event = projected.event_blocks[0]
    assert "Pool identity discovery did not complete" in visible
    assert "Retry" not in visible
    assert "Transient error" not in visible
    assert "Do not retry this operation or replay it through another tool" in visible
    assert "independent permitted work" in visible
    assert "and stop" not in visible
    assert event["error"] == result.error.message
    assert event["error_kind"] == "provider_error"
    assert event["recovery"] == result.error.recovery_hint


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        (ToolErrorKind.PROVIDER_ERROR, True),
        (ToolErrorKind.SCHEMA_VALIDATION, False),
    ],
)
def test_explicit_unsafe_recovery_wins_conflicting_flags(kind, retryable):
    visible = _render(_error(kind, retryable=retryable, recovery_hint={
        "safe_to_retry": False, "message": "UNTRUSTED retry now",
    }))

    assert "Do not retry this operation or replay it through another tool" in visible
    assert "Preserve the original request and producer state" in visible
    assert "independent permitted work" in visible
    assert "and stop" not in visible
    assert "UNTRUSTED" not in visible
    assert "Retry" not in visible
    assert "call `fixture_tool` again" not in visible


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ToolErrorKind.PROVIDER_ERROR, "Transient error. Retry `fixture_tool` once"),
        (ToolErrorKind.SCHEMA_VALIDATION, "Fix the payload and call `fixture_tool` again"),
    ],
)
def test_legacy_none_retryability_keeps_existing_directives(kind, expected):
    assert expected in _render(_error(kind))


def test_shell_exit_127_allows_safe_recovery_without_unchanged_retry():
    result = _error(ToolErrorKind.EXECUTION_ERROR, retryable=False,
                    detail={"exit_code": 127, "stderr_preview": "command not found"})
    result.name = "run_shell"
    result.error.message = "command exited with status 127"
    visible = _render(result)

    assert "command exited with status 127" in visible
    assert "Do not automatically repeat the unchanged call" in visible
    assert "corrected inputs or a different permitted method" in visible
    assert "possible side effects" in visible
    assert "bypass authorization" in visible
    assert "replay side effects" in visible
    assert "and stop" not in visible
    assert "Do not retry this call or switch tools" not in visible


@pytest.mark.parametrize("kind", [ToolErrorKind.PROVIDER_ERROR, ToolErrorKind.SCHEMA_VALIDATION])
@pytest.mark.parametrize("recovery_hint", [{}, {"safe_to_retry": True}])
def test_nonretryable_means_change_strategy(kind, recovery_hint):
    visible = _render(_error(kind, retryable=False, recovery_hint=recovery_hint))
    assert "corrected inputs or a different permitted method" in visible
    assert "Transient error" not in visible
    assert "call `fixture_tool` again" not in visible
    assert "and stop" not in visible


@pytest.mark.parametrize("retryable", [True, False, None])
def test_unknown_execution_requires_reconciliation_before_replay(retryable):
    result = execution_unknown_result(ToolCall(name="fixture_tool"), "lost result")
    result.error.retryable = retryable
    result.error.kind = ToolErrorKind.TIMEOUT
    result.error.recovery_hint = {"safe_to_retry": True}
    visible = _render(result)

    assert "outcome is unknown" in visible
    assert "Preserve the unknown execution state" in visible
    assert "reconcile" in visible
    assert "Do not replay the operation through this or another tool" in visible
    assert "Transient error" not in visible
    assert "different permitted method" not in visible


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("retryable", [True, False, None])
def test_access_denial_does_not_invite_transport_evasion(status, retryable):
    visible = _render(_error(ToolErrorKind.PROVIDER_ERROR, retryable=retryable,
                             detail={"http_status": status}))
    assert "Do not bypass authorization with another tool or transport" in visible
    assert "configuration or access" in visible
    assert "Transient error" not in visible
    assert "different permitted method" not in visible


def test_aborted_operation_does_not_invite_automatic_recovery():
    visible = _render(_error(ToolErrorKind.ABORTED, retryable=False))
    assert "Do not resume or replay the aborted operation" in visible
    assert "different permitted method" not in visible


def test_permission_approval_and_dedup_directives_keep_their_specific_semantics():
    permission = _render(_error(ToolErrorKind.PERMISSION_DENIED, retryable=False))
    approval = _render(_error(ToolErrorKind.PERMISSION_PENDING, retryable=False))
    deduped = _render(_error(ToolErrorKind.DEDUPED, retryable=False))

    assert "lane does not permit" in permission
    assert "Approval is owed" in approval
    assert "do not re-issue this exact call" in deduped
