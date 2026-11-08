"""Offline RPC branch diagnostics through the producer and public projection."""

from copy import deepcopy
import json

import pytest

from nerya.agent.tool_projection import project_tool_results
from nerya.data_api import uniswap_history as history
from nerya.data_api.registry import DataApiRegistry
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall, ToolErrorKind
from test_uniswap_history_data import ARGS, CatalogFake, Fake

pytestmark = pytest.mark.smoke
PRIVATE = "PRIVATE_BODY_MESSAGE_PARAMS https://user:password@private.invalid/?token=secret"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)


def tool_result(fake, args):
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(fake, catalog_http=CatalogFake())[0])
    call = ToolCall(id="rpc-diagnostic", name="data_api", arguments={
        "op": "call", "provider": "uniswap", "action": "pool_history", "args": args})
    original = deepcopy(call.arguments)
    result = data_api_handler(call, registry=registry)
    assert call.arguments == original
    assert result.is_error and result.error.retryable is False
    projection = project_tool_results([result],
        render_tool_result=lambda item: {"type": "tool_result", "content": item.text()},
        rendered_tool_result_text=lambda item: item.get("content"))
    public = result.text() + str(result.error) + str(projection)
    for forbidden in (PRIVATE, "private.invalid", "password", "secret", "https://",
                      "params", "calldata", args["pool_address"]):
        assert forbidden not in public
    return result, projection.event_blocks[0]


@pytest.mark.parametrize("provider", ["publicnode", "drpc"])
@pytest.mark.parametrize("method,count", [("eth_chainId", 1), ("eth_blockNumber", 2),
    ("eth_getBlockByNumber", 3), ("eth_call", 4)])
@pytest.mark.parametrize("mode,stage,extra", [
    ("http", "http_status", {"http_status": 429}),
    ("http_500", "http_status", {"http_status": 500}),
    ("status_string", "response_envelope", {}),
    ("status_bool", "response_envelope", {}),
    ("status_large", "response_envelope", {}),
    ("rpc", "rpc_error", {"rpc_error_code": -32000}),
    ("code_string", "response_envelope", {}),
    ("code_bool", "response_envelope", {}),
    ("code_large", "response_envelope", {}),
    ("error_null", "response_envelope", {}),
    ("both", "response_envelope", {}),
    ("envelope", "response_envelope", {}),
    ("version", "response_envelope", {}),
    ("id", "response_envelope", {}),
    ("id_bool", "response_envelope", {}),
    ("missing", "missing_result", {}),
    ("null", "missing_result", {}),
    ("transport", "transport", {}),
    ("timeout", "transport", {}),
    ("unknown_value_error", "transport", {}),
    ("json", "response_envelope", {}),
    ("nan", "response_envelope", {}),
    ("oversize", "response_size", {}),
])
def test_rpc_failure_matrix_survives_tool_and_projection(provider, method, count, mode, stage, extra):
    def mutate(req, doc, fake):
        if req["method"] != method:
            return 200, doc
        if mode == "http": return 429, {"raw": PRIVATE}
        if mode == "http_500": return 500, {"raw": PRIVATE}
        if mode == "status_string": return PRIVATE, doc
        if mode == "status_bool": return True, doc
        if mode == "status_large": return 2**64, doc
        if mode == "transport": raise RuntimeError(PRIVATE)
        if mode == "timeout": raise TimeoutError(PRIVATE)
        if mode == "unknown_value_error": raise ValueError(PRIVATE)
        if mode == "json": raise json.JSONDecodeError(PRIVATE, PRIVATE, 0)
        if mode == "envelope": return 200, [PRIVATE]
        doc["private"] = PRIVATE
        if mode in {"rpc", "code_string", "code_bool", "code_large", "error_null", "both"}:
            codes = {"rpc": -32000, "code_string": PRIVATE, "code_bool": True,
                     "code_large": 2**64, "both": -32000, "error_null": 0}
            doc["error"] = {"code": codes[mode], "message": PRIVATE, "data": PRIVATE}
            if mode != "both": doc.pop("result")
            if mode == "error_null": doc["error"] = None
        if mode == "version": doc["jsonrpc"] = PRIVATE
        if mode == "id": doc["id"] = PRIVATE
        if mode == "id_bool": doc["id"] = True
        if mode == "missing": doc.pop("result")
        if mode == "null": doc["result"] = None
        if mode == "nan": doc["extra"] = float("nan")
        if mode == "oversize": doc["extra"] = PRIVATE * history.MAX_RESPONSE_BYTES
        return 200, doc
    fake = Fake(mutate, history.RPC_ORIGINS[provider])
    result, event = tool_result(fake, {**ARGS, "rpc_provider": provider})
    assert len(fake.calls) == count  # No retry, fallback or further sampling.
    assert result.error.kind == ToolErrorKind.PROVIDER_ERROR
    assert result.error.detail == {
        "provider": "uniswap", "action": "pool_history", "op": "call",
        "reason": "historical_rpc_evidence_failure", "failure_stage": stage,
        "rpc_method": method, "rpc_provider": provider, "request_count": count,
        "coverage_complete": False, "safe_to_retry": False, **extra}
    assert event["recovery"] == {
        "action": "inspect_historical_diagnostic", "reason": "historical_rpc_evidence_failure",
        "safe_to_retry": False, "preserve_candidate": True,
        "preserve_requested_constraints": True, "failure_stage": stage,
        "rpc_method": method, "request_count": count, **extra}
    from types import SimpleNamespace
    from nerya.agent.loop import WorkspaceNativeAgentLoop

    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root="/tmp/rpc-diagnostic-test")
    projection = project_tool_results([result], render_tool_result=loop._render_tool_result,
        rendered_tool_result_text=loop._rendered_tool_result_text)
    diagnostic = {"failure_stage": stage, "rpc_method": method, "request_count": count, **extra}
    assert json.loads(projection.event_blocks[0]["error"].split("Diagnostic: ")[1]) == diagnostic
    model_text = projection.transcript_blocks[0]["content"][0]["text"]
    assert json.dumps(diagnostic, sort_keys=True) in model_text
    assert "Do not retry unchanged" in model_text
    assert projection.event_blocks[0]["recovery"] == event["recovery"]
    for forbidden in (PRIVATE, "private.invalid", "password", "secret", "https://", "params", "calldata"):
        assert forbidden not in str(projection)


@pytest.mark.parametrize("during_request", [False, True])
def test_shared_deadline_has_observed_attempt_count(monkeypatch, during_request):
    clock = [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    if not during_request: monkeypatch.setattr(history, "DEADLINE_SECONDS", 0)
    def expire(req, doc, fake):
        clock[0] = history.DEADLINE_SECONDS + 1
        return 200, doc
    fake = Fake(expire, history.RPC_ORIGINS["drpc"])
    # The reported UI scope, exercised offline; no upstream cause is inferred.
    args = {key: value for key, value in ARGS.items() if key not in {"start_block", "end_block"}}
    args.update(start="2026-09-28", end="2026-10-28", step_blocks=7200,
                tick_lower=-887270, tick_upper=887270, rpc_provider="drpc")
    result, event = tool_result(fake, args)
    assert result.error.detail["failure_stage"] == "deadline"
    assert result.error.detail["request_count"] == len(fake.calls) == int(during_request)
    assert event["recovery"]["action"] == "inspect_historical_diagnostic"


def test_request_budget_stops_before_another_attempt(monkeypatch):
    def exhaust(req, doc, fake):
        monkeypatch.setattr(history, "MAX_REQUESTS", 1)
        return 200, doc
    fake = Fake(exhaust)
    result, event = tool_result(fake, ARGS)
    assert result.error.detail["failure_stage"] == "request_budget"
    assert result.error.detail["request_count"] == len(fake.calls) == 1
    assert result.error.detail["rpc_method"] == "eth_blockNumber"
    assert event["recovery"]["safe_to_retry"] is False


def test_reported_date_scope_keeps_candidate_and_window_on_rpc_error():
    fake = Fake(lambda req, doc, fake: (200, {"jsonrpc": "2.0", "id": req["id"],
        "error": {"code": -32603, "message": PRIVATE}}), history.RPC_ORIGINS["drpc"])
    args = {"pool_address": ARGS["pool_address"], "start": "2026-09-28", "end": "2026-10-28",
            "step_blocks": 7200, "max_samples": 64, "tick_lower": -887270,
            "tick_upper": 887270, "rpc_provider": "drpc"}
    result, event = tool_result(fake, args)
    assert result.error.detail["rpc_error_code"] == -32603
    assert result.error.detail["failure_stage"] == "rpc_error"
    assert event["recovery"]["preserve_candidate"] is True
    assert event["recovery"]["preserve_requested_constraints"] is True
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_authorization_branch_keeps_existing_recovery(status):
    fake = Fake(lambda req, doc, fake: (status, {"raw": PRIVATE}))
    result, event = tool_result(fake, ARGS)
    assert result.error.detail["reason"] == "historical_rpc_authorization_required"
    assert result.error.detail["http_status"] == status
    assert result.error.detail["archive_access_established"] is False
    assert "supported_data_source_integration_required" not in result.error.detail
    assert result.error.detail["arbitrary_credentials_supported"] is False
    assert "fixed pool_history action" in str(result.error)
    assert event["recovery"]["action"] == "inspect_historical_sources"
    assert len(fake.calls) == 1
