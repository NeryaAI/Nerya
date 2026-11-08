from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.data_api import build_data_api_registry
from nerya.llm.tool_compaction import compact_tool_result
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.result_contracts import result_allows_repair
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _call(action, args, **kwargs):
    return ToolCall(name="data_api", arguments={
        "op": "call", "provider": "onchainos", "action": action,
        "args": args, **kwargs,
    })


@pytest.mark.parametrize(("action", "args", "field"), [
    ("cli_read", {"args": ["defi", "chart", "--help"]}, "command"),
    ("cli_read", {"command": ["defi", "chart", "--help"]}, "command"),
    ("cli_read", {"command": "defi chart --help"}, "command"),
    ("cli_read", {"command": "defi_rate_chart --help"}, "command"),
    ("defi_tvl_chart", {"chain": "196", "limit": 200, "platform": "aave"}, "investment_id"),
])
def test_observed_wrong_calls_point_to_exact_schema_without_execution(action, args, field, monkeypatch):
    registry = build_data_api_registry()
    monkeypatch.setattr(registry, "call", lambda *a, **k: pytest.fail("invalid call executed"))
    call = _call(action, args)
    original = deepcopy(call.arguments)
    result = data_api_handler(call, registry=registry)
    assert result.is_error
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert result.error.retryable is True
    assert not result_allows_repair(result)
    assert call.arguments == original
    assert result.error.detail["field"] == field
    hint = result.error.recovery_hint
    assert hint["tool"] == "data_api"
    assert hint["arguments"] == {"op": "schema", "provider": "onchainos", "action": action}
    assert "max_corrected_calls" not in hint
    assert "unchanged" in hint["hint"]
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    loop = object.__new__(WorkspaceNativeAgentLoop)
    directive = loop._retry_directive_for(result.error.kind.value, result)
    assert "corrected arguments" in directive
    assert "and stop" not in directive
    schema_result = data_api_handler(ToolCall(name="data_api", arguments=hint["arguments"]), registry=registry)
    assert not schema_result.is_error
    assert schema_result.content[0].data["input_schema"] == result.error.detail["input_schema"]


def test_unknown_jupiter_swap_stays_read_only_and_points_to_discovery(monkeypatch):
    registry = build_data_api_registry()
    monkeypatch.setattr(registry, "call", lambda *a, **k: pytest.fail("unknown call executed"))
    result = data_api_handler(_call("swap", {"inTokenSymbol": "SOL", "outTokenSymbol": "USDC"}, provider="jupiter"), registry=registry)
    assert result.error.kind == ToolErrorKind.NOT_FOUND
    assert result.error.retryable is False
    hint = result.error.recovery_hint
    assert hint["arguments"] == {"op": "list"}
    assert "read-only" in hint["hint"]
    assert "financial_components" in hint["hint"]
    assert "execution" in hint["hint"]
    assert "financial_prepare" not in str(hint)


def test_month_request_reports_observed_extent_and_survives_compaction(monkeypatch):
    registry = build_data_api_registry()
    # Synthetic rate values; the real event established 60 rows and the first
    # timestamps, not a complete retained historical dataset.
    rows = [{"timestamp": 1791781200000 + i * 14400000, "rate": "0.04"} for i in range(60)]
    monkeypatch.setattr(registry, "call", lambda *a, **k: deepcopy(rows))
    result = data_api_handler(_call("defi_rate_chart", {"chain": "196", "investment_id": "33923", "time_range": "MONTH"}, limit=500, columns=["timestamp", "rate"]), registry=registry)
    assert not result.is_error
    payload = result.content[0].data
    assert payload["row_count"] == 60
    assert payload["rows"] == rows
    hint = payload["next_required_action"]
    assert hint["requested_time_range"] == "MONTH"
    extent = hint["observed_time_extent"]
    assert extent["scope"] == "returned_rows"
    assert extent["timestamp_count"] == 60
    assert extent["start_utc"] == datetime.fromtimestamp(rows[0]["timestamp"] / 1000, timezone.utc).isoformat()
    assert extent["end_utc"] == datetime.fromtimestamp(rows[-1]["timestamp"] / 1000, timezone.utc).isoformat()
    assert extent["span_seconds"] == 59 * 14400 < 30 * 86400
    assert hint["coverage_verified"] is False
    assert "unchanged" in hint["hint"]
    compacted = compact_tool_result("data_api", payload, size_threshold=0)
    assert {key: compacted.kept["next_required_action"][key] for key in hint} == hint
