"""Tests for schema validation error rendering + executor integration.

Covers:

* :mod:`nerya.tools.tool_errors` — multi-issue English rendering in
  the Claude Code ``formatZodValidationError`` shape.
* :mod:`nerya.tools.executor` — end-to-end: a malformed
  ``strategy_generate_proposal`` payload produces a friendly
  ``SCHEMA_VALIDATION`` result that the loop can surface verbatim.
* Schema-driven provider transport decoding without tool-specific field
  inference.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nerya.tools.executor import (
    NativeToolExecutor,
    _repair_arguments_before_validation,
    _validate_against_schema,
)
from nerya.tools.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
)
from nerya.tools.registry import ToolRegistry
from nerya.tools.tool_errors import (
    collect_schema_issues,
    format_schema_validation_error,
    schema_validation_result,
)
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolErrorKind,
    ToolResult,
)


pytestmark = pytest.mark.smoke


def test_schema_validation_result_preserves_call_and_recovery_hint():
    call = ToolCall(name="native_tool", id="toolu_bad_input")

    result = schema_validation_result(
        call,
        "field is required",
        recovery_hint={"required": ["field"]},
    )

    assert result.tool_use_id == call.id
    assert result.name == call.name
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert result.error.recovery_hint == {"required": ["field"]}


# ---------------------------------------------------------------------------
# collect_schema_issues
# ---------------------------------------------------------------------------


def test_missing_required_field_is_reported():
    schema = {
        "type": "object",
        "properties": {"strategy_id": {"type": "string"}},
        "required": ["strategy_id"],
    }
    issues = collect_schema_issues({}, schema)
    assert len(issues) == 1
    assert issues[0]["kind"] == "missing"
    assert issues[0]["field"] == "strategy_id"


def test_multiple_required_fields_all_reported():
    """The renderer should surface every missing field, not bail on the first."""
    schema = {
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "markets": {"type": "array"},
            "accounts": {"type": "array"},
        },
        "required": ["strategy_id", "markets", "accounts"],
    }
    issues = collect_schema_issues({}, schema)
    kinds = [i["kind"] for i in issues]
    fields = [i["field"] for i in issues]
    assert kinds == ["missing", "missing", "missing"]
    assert set(fields) == {"strategy_id", "markets", "accounts"}


def test_type_mismatch_is_reported_with_expected_and_actual():
    schema = {
        "type": "object",
        "properties": {"markets": {"type": "array"}},
    }
    issues = collect_schema_issues({"markets": "binance:BTC/USDT"}, schema)
    assert len(issues) == 1
    assert issues[0]["kind"] == "type"
    assert issues[0]["expected"] == "array"
    assert issues[0]["actual"] == "string"


def test_enum_mismatch_is_reported():
    schema = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["paper", "live"]}},
    }
    issues = collect_schema_issues({"mode": "shadow_box"}, schema)
    assert len(issues) == 1
    assert issues[0]["kind"] == "enum"


def test_additional_properties_false_flags_unexpected_fields():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    issues = collect_schema_issues({"a": "ok", "b": 1}, schema)
    assert any(i["kind"] == "unexpected" and i["field"] == "b" for i in issues)


def test_number_accepts_integer():
    """JSON Schema allows number to accept integers — we should too."""
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    assert collect_schema_issues({"x": 3}, schema) == []


def test_valid_payload_returns_empty_list():
    schema = {
        "type": "object",
        "properties": {"strategy_id": {"type": "string"}},
        "required": ["strategy_id"],
    }
    assert collect_schema_issues({"strategy_id": "scalper"}, schema) == []


# ---------------------------------------------------------------------------
# format_schema_validation_error
# ---------------------------------------------------------------------------


def test_single_issue_uses_singular_phrasing():
    rendered = format_schema_validation_error(
        "strategy_generate_proposal",
        [{"kind": "missing", "field": "strategy_id"}],
    )
    assert "following issue:" in rendered
    assert "The required parameter `strategy_id` is missing" in rendered


def test_multiple_issues_use_plural_phrasing():
    rendered = format_schema_validation_error(
        "strategy_generate_proposal",
        [
            {"kind": "missing", "field": "strategy_id"},
            {"kind": "missing", "field": "markets"},
        ],
    )
    assert "following issues:" in rendered
    assert "`strategy_id`" in rendered
    assert "`markets`" in rendered


def test_type_mismatch_renders_expected_vs_received():
    rendered = format_schema_validation_error(
        "foo",
        [{"kind": "type", "field": "x", "expected": "array", "actual": "string"}],
    )
    assert "expected as `array`" in rendered
    assert "provided as `string`" in rendered


def test_rendered_output_does_not_leak_raw_schema():
    """Regression: the old renderer dumped the JSON schema as a Python
    dict repr — this is exactly what confused non-Claude models. The
    new shape must never leak JSON-schema shape markers into the
    rendered text (schema dicts stringify with quoted ``'type'`` /
    ``'properties'`` / ``'additionalProperties'`` tokens)."""
    rendered = format_schema_validation_error(
        "foo", [{"kind": "missing", "field": "x"}]
    )
    assert "'type'" not in rendered
    assert "'properties'" not in rendered
    assert "additionalProperties" not in rendered


# ---------------------------------------------------------------------------
# Legacy _validate_against_schema still returns a short string
# ---------------------------------------------------------------------------


def test_legacy_validator_still_returns_none_on_valid():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }
    assert _validate_against_schema({"a": "ok"}, schema) is None


def test_legacy_validator_reports_first_missing_field():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }
    msg = _validate_against_schema({}, schema)
    assert msg == "missing required field: a"


# ---------------------------------------------------------------------------
# Executor end-to-end: malformed payload -> friendly SCHEMA_VALIDATION result
# ---------------------------------------------------------------------------


def _build_executor(descriptor: ToolDescriptor) -> NativeToolExecutor:
    registry = ToolRegistry()
    registry.register(descriptor)
    return NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )


def _proposal_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name="strategy_generate_proposal",
        description="stub",
        input_schema={
            "type": "object",
            "properties": {
                "strategy_id": {"type": "string"},
                "title": {"type": "string"},
                "markets": {"type": "array"},
                "accounts": {"type": "array"},
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "mode": {
                    "type": "string",
                    "enum": ["paper", "shadow", "live"],
                },
            },
            "required": ["strategy_id", "markets", "accounts"],
        },
        handler=lambda _call: ToolResult.from_text(
            tool_use_id=_call.id, name=_call.name, text="ok"
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.WORKSPACE,
    )


def test_executor_returns_friendly_message_for_missing_fields():
    executor = _build_executor(_proposal_descriptor())
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={"title": "Scalper"},  # no markets/accounts; title will backfill strategy_id
    )
    result = executor.execute(call)
    assert result.is_error
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.SCHEMA_VALIDATION

    # The message must read like English, not a dict dump.
    msg = result.error.message
    assert "The required parameter `markets` is missing" in msg
    assert "The required parameter `accounts` is missing" in msg
    # And it must NOT contain a Python-dict dump of the schema.
    assert "'type': 'object'" not in msg


def test_executor_surfaces_issues_in_detail_for_telemetry():
    executor = _build_executor(_proposal_descriptor())
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={},
    )
    result = executor.execute(call)
    assert result.is_error
    detail = result.error.detail
    assert "issues" in detail
    fields = {i["field"] for i in detail["issues"]}
    assert {"strategy_id", "markets", "accounts"}.issubset(fields)


def test_executor_recovery_hint_is_compact_and_actionable():
    """The new recovery_hint is a {action, tool_name} pair, not a
    schema dump. Dashboards and the loop both rely on this stable shape."""
    executor = _build_executor(_proposal_descriptor())
    call = ToolCall(name="strategy_generate_proposal", arguments={})
    result = executor.execute(call)
    hint = result.error.recovery_hint
    assert hint == {
        "action": "fix_arguments_and_retry",
        "tool_name": "strategy_generate_proposal",
    }


def test_executor_decodes_json_string_container_arguments():
    captured: list[dict] = []
    descriptor = _proposal_descriptor()
    descriptor = replace(
        descriptor,
        handler=lambda call: (
            captured.append(dict(call.arguments or {}))
            or ToolResult.from_text(tool_use_id=call.id, name=call.name, text="ok")
        ),
    )
    executor = _build_executor(descriptor)
    files = {"main.py": "print('ok')", "strategy.md": "# Strategy"}
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={
            "strategy_id": "json_files_strategy",
            "markets": json.dumps(["BINANCE:ETHUSDT"]),
            "accounts": ["paper"],
            "files": json.dumps(files),
        },
    )

    result = executor.execute(call)

    assert not result.is_error
    assert captured[0]["markets"] == ["BINANCE:ETHUSDT"]
    assert captured[0]["files"] == files


def test_executor_decodes_schema_array_item_wrapper_before_validation():
    captured: list[dict] = []
    descriptor = ToolDescriptor(
        name="array_tool",
        description="stub",
        input_schema={
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["symbols"],
        },
        handler=lambda call: (
            captured.append(dict(call.arguments or {}))
            or ToolResult.from_text(tool_use_id=call.id, name=call.name, text="ok")
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.WORKSPACE,
    )
    executor = _build_executor(descriptor)
    call = ToolCall(name="array_tool", arguments={"symbols": {"item": "BTCUSDT"}})

    result = executor.execute(call)

    assert not result.is_error
    assert captured[0]["symbols"] == ["BTCUSDT"]


def test_executor_coerces_schema_number_strings_before_validation():
    captured: list[dict] = []
    descriptor = ToolDescriptor(
        name="numeric_tool",
        description="stub",
        input_schema={
            "type": "object",
            "properties": {
                "size": {"type": "number"},
                "confidence": {"type": "number"},
            },
            "required": ["size", "confidence"],
        },
        handler=lambda call: (
            captured.append(dict(call.arguments or {}))
            or ToolResult.from_text(tool_use_id=call.id, name=call.name, text="ok")
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.WORKSPACE,
    )
    executor = _build_executor(descriptor)
    call = ToolCall(name="numeric_tool", arguments={"size": "100", "confidence": "0.75"})

    result = executor.execute(call)

    assert not result.is_error
    assert captured[0]["size"] == 100
    assert captured[0]["confidence"] == 0.75


def test_schema_transport_unwrap_does_not_infer_strategy_item_fields():
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={
            "strategy_id": "btcusdt_funding_arb",
            "markets": {
                "item": {
                    "venue": "binance",
                    "market": "BINANCE:BTCUSDT",
                    "instrument": "perp",
                },
            },
            "accounts": {
                "item": {
                    "venue": "binance",
                    "account_id": "binance_paper_001",
                    "mode": "paper",
                },
            },
        },
    )

    _repair_arguments_before_validation(call, _proposal_descriptor().input_schema)

    assert call.arguments["markets"] == [{
        "venue": "binance",
        "market": "BINANCE:BTCUSDT",
        "instrument": "perp",
    }]
    assert call.arguments["accounts"] == [{
        "venue": "binance",
        "account_id": "binance_paper_001",
        "mode": "paper",
    }]


def test_schema_transport_does_not_add_market_venue_or_extract_account_label():
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={
            "strategy_id": "btcusdt_funding_arb",
            "markets": ["BTC/USDT:USDT"],
            "accounts": [
                {
                    "venue": "binance_perpetual",
                    "label": "binance_perp_paper",
                    "mode": "paper",
                    "credentials": "missing",
                },
            ],
        },
    )

    _repair_arguments_before_validation(call, _proposal_descriptor().input_schema)

    assert call.arguments["markets"] == ["BTC/USDT:USDT"]
    assert call.arguments["accounts"] == [{
        "venue": "binance_perpetual",
        "label": "binance_perp_paper",
        "mode": "paper",
        "credentials": "missing",
    }]


def test_executor_decodes_raw_json_object_arguments_before_validation():
    captured: list[dict] = []
    descriptor = _proposal_descriptor()
    descriptor = replace(
        descriptor,
        handler=lambda call: (
            captured.append(dict(call.arguments or {}))
            or ToolResult.from_text(tool_use_id=call.id, name=call.name, text="ok")
        ),
    )
    executor = _build_executor(descriptor)
    raw = json.dumps(
        {
            "strategy_id": "binance_aster_cash_carry",
            "title": "Binance + Aster Cash Carry",
            "markets": ["BINANCE:BTCUSDT", "ASTER:BTCUSDT-PERP"],
            "accounts": ["binance_paper", "paper"],
            "mode": "paper",
            "files": {
                "main.py": "def run(ctx):\n    return ctx.result.skip('gap')",
            },
        }
    )
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={"_raw": raw},
    )

    result = executor.execute(call)

    assert not result.is_error
    assert captured[0]["strategy_id"] == "binance_aster_cash_carry"
    assert captured[0]["markets"] == ["BINANCE:BTCUSDT", "ASTER:BTCUSDT-PERP"]
    assert captured[0]["files"]["main.py"].startswith("def run")


def test_executor_rejects_truncated_raw_json_instead_of_dropping_fields():
    captured: list[dict] = []
    descriptor = _proposal_descriptor()
    descriptor = replace(
        descriptor,
        handler=lambda call: (
            captured.append(dict(call.arguments or {}))
            or ToolResult.from_text(tool_use_id=call.id, name=call.name, text="ok")
        ),
    )
    executor = _build_executor(descriptor)
    raw = (
        '{"strategy_id":"bsc_whale_copytrade",'
        '"title":"BSC Whale Copytrade",'
        '"description":"Whale flow strategy.",'
        '"prompt":"Use BSC whale flow evidence.",'
        '"strategy_class":"agent",'
        '"execution_mode":"agent",'
        '"mode":"paper",'
        '"markets":["bsc:0xMEME"],'
        '"accounts":["binance_paper"],'
        '"files": '
    )
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={"_raw": raw},
    )

    result = executor.execute(call)

    assert result.is_error
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.SCHEMA_VALIDATION
    assert captured == []


def test_executor_rejects_tool_specific_wrapper_instead_of_guessing_payload_shape():
    captured: list[dict] = []
    descriptor = _proposal_descriptor()
    descriptor = replace(
        descriptor,
        handler=lambda call: (
            captured.append(dict(call.arguments or {}))
            or ToolResult.from_text(tool_use_id=call.id, name=call.name, text="ok")
        ),
    )
    executor = _build_executor(descriptor)
    call = ToolCall(
        name="strategy_generate_proposal",
        arguments={
            "request": {
                "strategy_id": "eth_rsi_agent_breakout",
                "title": "ETH RSI Agent Breakout",
                "markets": ["BINANCE:ETHUSDT"],
                "accounts": ["binance_paper"],
                "mode": "paper",
            }
        },
    )

    result = executor.execute(call)

    assert result.is_error
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.SCHEMA_VALIDATION
    assert captured == []
