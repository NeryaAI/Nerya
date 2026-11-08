from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.data_api import build_data_api_registry
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataActionSpec
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke

def test_qualified_action_uses_only_its_matching_provider_namespace():
    from nerya.data_api.types import DataApiContext, DataApiError
    registry = DataApiRegistry()
    registry.register_action(DataActionSpec(provider="example", action="history",
        title="History", description="Read only", input_schema={"type": "object"},
        handler=lambda context, args: {"rows": []}, replay_input=True))
    assert registry.schema("example", "example.history")["action"] == "history"
    assert registry.call("example", "example.history", {}, context=DataApiContext()) == {"rows": []}
    assert registry.call_replay_input("example", "example.history", {}) == {"rows": []}
    with pytest.raises(DataApiError):
        registry.schema("example", "other.history")


def _render(result):
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root="/tmp/nerya-data-api-operation-confusion")
    return loop._render_tool_result(result)["content"][0]["text"]


@pytest.mark.parametrize(
    ("arguments", "expected_route"),
    [
        (
            {
                "op": "schema",
                "provider": "defillama",
                "action": "schema",
                "query": "yield_pools",
            },
            {"op": "schema", "provider": "defillama", "action": "yield_pools"},
        ),
        (
            {
                "op": "schema",
                "provider": "defillama",
                "action": "schema",
                "args": {"action": "yield_pools"},
            },
            {"op": "schema", "provider": "defillama", "action": "yield_pools"},
        ),
        (
            {
                "op": "call",
                "provider": "defillama",
                "action": "call",
                "args": {"action": "yield_pools"},
            },
            {
                "op": "call",
                "provider": "defillama",
                "action": "yield_pools",
                "args": {},
            },
        ),
    ],
)
def test_operation_confusion_reaches_model_with_exact_read_only_route(
    arguments, expected_route, monkeypatch
):
    registry = build_data_api_registry()
    monkeypatch.setattr(registry, "call", lambda *a, **k: pytest.fail("provider action executed"))
    original = deepcopy(arguments)

    result = data_api_handler(ToolCall(name="data_api", arguments=arguments), registry=registry)

    assert result.is_error
    assert result.error.kind == ToolErrorKind.NOT_FOUND
    assert result.error.retryable is False
    assert arguments == original
    assert result.error.recovery_hint["arguments"] == expected_route
    assert result.error.recovery_hint["candidate_source"] in {"query", "args.action"}
    assert "'op' is the control operation" in result.error.message
    assert "'action' is the installed provider action" in result.error.message
    assert json.dumps(expected_route, sort_keys=True) in result.error.message
    rendered = _render(result)
    assert "'op' is the control operation" in rendered
    assert "yield_pools" in rendered
    assert "provider action executed" not in rendered


def test_operation_confusion_without_unique_candidate_has_bounded_preview_and_no_guess(monkeypatch):
    registry = build_data_api_registry()
    monkeypatch.setattr(registry, "call", lambda *a, **k: pytest.fail("provider action executed"))
    arguments = {"op": "schema", "provider": "defillama", "action": "schema"}
    original = deepcopy(arguments)

    result = data_api_handler(ToolCall(name="data_api", arguments=arguments), registry=registry)

    assert result.is_error
    assert result.error.kind == ToolErrorKind.NOT_FOUND
    assert arguments == original
    hint = result.error.recovery_hint
    assert hint["arguments"] == {"op": "list", "provider": "defillama"}
    assert hint["action"] == "discover_data_actions"
    assert len(hint["installed_actions"]) <= 8
    assert all(set(row) <= {"provider", "action", "title", "output_kind"} for row in hint["installed_actions"])
    assert "Use op='list'" in result.error.message
    assert "provider" in result.error.message and "action" in result.error.message
    rendered = _render(result)
    assert "yield_pools" in rendered
    assert "do-not-echo" not in rendered  # no arbitrary nested text is echoed


def test_ambiguous_catalog_candidates_do_not_guess(monkeypatch):
    registry = build_data_api_registry()
    arguments = {
        "op": "schema",
        "provider": "defillama",
        "action": "schema",
        "query": "yield_pools",
        "args": {"action": "yield_history"},
    }
    original = deepcopy(arguments)

    result = data_api_handler(ToolCall(name="data_api", arguments=arguments), registry=registry)

    assert result.is_error
    assert arguments == original
    assert result.error.recovery_hint["action"] == "discover_data_actions"
    assert result.error.recovery_hint["arguments"] == {"op": "list", "provider": "defillama"}
    assert "yield_pools" in result.error.message and "yield_history" in result.error.message
    assert "No unique known action" in result.error.message


def test_unknown_non_control_route_keeps_existing_discovery_shape(monkeypatch):
    registry = build_data_api_registry()
    arguments = {
        "op": "schema",
        "provider": "defillama",
        "action": "missing_action",
        "args": {"secret": "do-not-echo"},
    }
    result = data_api_handler(ToolCall(name="data_api", arguments=arguments), registry=registry)

    assert result.is_error
    assert result.error.recovery_hint["arguments"] == {"op": "list", "provider": "defillama"}
    assert "do-not-echo" not in result.error.message


def test_correct_call_route_is_unchanged():
    calls = []
    registry = DataApiRegistry()
    registry.register_action(
        DataActionSpec(
            provider="fixture",
            action="yield_pools",
            title="Fixture pools",
            description="Fixture read",
            input_schema={"type": "object", "additionalProperties": False},
            handler=lambda _context, args: calls.append(args) or {"rows": []},
        )
    )

    result = data_api_handler(
        ToolCall(
            name="data_api",
            arguments={
                "op": "call",
                "provider": "fixture",
                "action": "yield_pools",
                "args": {},
            },
        ),
        registry=registry,
    )

    assert not result.is_error, result.text()
    assert calls == [{}]
