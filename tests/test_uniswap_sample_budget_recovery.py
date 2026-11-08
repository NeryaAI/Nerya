"""Offline sample-budget diagnostics; no request correction or runtime writes."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from nerya.agent.tool_projection import project_tool_results
from nerya.data_api import uniswap_history as history
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataApiContext, DataApiError
from nerya.harness.cancellation import CancelToken, CancelledError
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke
PRIVATE = "PRIVATE_PROVIDER_BODY /private/local/path https://user:secret@private.invalid"
BASE_TIME = 1_500_000_000
ARGS = dict(pool_address="0x" + "ab" * 20, start_block=100, end_block=110,
            step_blocks=3, max_samples=4, tick_lower=-120, tick_upper=120)


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr(history.time, "time", lambda: stamp("2026-10-30"))


class HeaderOnlyFake:
    def __init__(self):
        self.calls = []
        self.head = (stamp("2026-10-29") - BASE_TIME) // 12 + 100

    def request(self, method, url, **kwargs):
        assert method == "POST" and url == history.RPC_URL
        req = deepcopy(kwargs["body"])
        self.calls.append(req)
        rpc, params = req["method"], req["params"]
        if rpc == "eth_chainId":
            result = "0x1"
        elif rpc == "eth_blockNumber":
            result = hex(self.head)
        else:
            assert rpc == "eth_getBlockByNumber", "sample/state access forbidden"
            block = int(params[0], 16)
            assert params == [hex(block), False]
            result = dict(number=hex(block), hash="0x" + format(block + 1, "064x"),
                          timestamp=hex(BASE_TIME + block * 12), ignored=PRIVATE)
        return 200, dict(jsonrpc="2.0", id=req["id"], result=result)


class NoCatalog:
    def request(self, *args, **kwargs):
        pytest.fail("catalog access forbidden")


@pytest.mark.parametrize("end,step,maximum,required,minimum", [
    (110, 3, 4, 5, 4),   # non-divisible delta includes the end once
    (112, 3, 4, 5, 4),   # divisible delta also includes both endpoints
    (101, 99, 1, 2, None),
    (history.MAX_BLOCK, 1, 30, history.MAX_BLOCK - 99,
     (history.MAX_BLOCK - 100 + 28) // 29),  # integer precision above 2**53
])
def test_exact_count_and_minimum_preserve_block_request(end, step, maximum, required, minimum):
    args = {**ARGS, "end_block": end, "step_blocks": step, "max_samples": maximum}
    original = deepcopy(args)
    fake = HeaderOnlyFake()
    with pytest.raises(DataApiError) as caught:
        history.UniswapHistoryProvider(fake, catalog_http=NoCatalog()).pool_history(DataApiContext(), args)
    error = caught.value
    assert error.kind == "schema_validation" and error.retryable is False
    detail = error.detail
    assert detail["reason"] == "historical_sample_budget_exceeded"
    assert detail["required_samples"] == required
    assert detail["max_samples"] == maximum
    assert detail["resolved_start_block"] == 100
    assert detail["resolved_end_block"] == end
    assert detail["step_blocks"] == step
    assert detail["minimum_step_blocks_to_fit"] == minimum
    assert detail["requested_window"] == dict(start_block=100, end_block=end, step_blocks=step)
    assert detail["coverage_complete"] is False and detail["safe_to_retry"] is False
    assert f"requires {required} samples" in error.message
    assert f"max_samples allows {maximum}" in error.message
    if minimum is None:
        assert "No step_blocks can fit both endpoints" in error.message
    else:
        assert f"minimum step_blocks to fit max_samples is {minimum}" in error.message
        corrected = {**args, "step_blocks": minimum}
        assert len(history._blocks(corrected)) <= maximum
        if minimum > 1:
            with pytest.raises(DataApiError):
                history._blocks({**corrected, "step_blocks": minimum - 1})
    assert args == original and fake.calls == []


@pytest.mark.parametrize("start,required,minimum", [
    ("2026-09-30", 62, 7200),
    ("2026-09-29", 64, 7449),
])
def test_resolved_utc_diagnostic_survives_serialization_and_message_only_projection(start, required, minimum):
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update(start=start, end="2026-10-29", step_blocks=3457, max_samples=30)
    original = deepcopy(args)
    fake = HeaderOnlyFake()
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(fake, catalog_http=NoCatalog())[0])
    result = data_api_handler(ToolCall(id="sample-budget", name="data_api", arguments={
        "op": "call", "provider": "uniswap", "action": "pool_history", "args": args,
    }), registry=registry)
    assert result.is_error and result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert result.error.retryable is False
    detail = json.loads(json.dumps(result.asdict()))["error"]["detail"]
    assert detail["reason"] == "historical_sample_budget_exceeded"
    assert detail["required_samples"] == required and detail["max_samples"] == 30
    assert detail["resolved_start_block"] == (stamp(start) - BASE_TIME) // 12
    assert detail["resolved_end_block"] == (stamp(args["end"]) - BASE_TIME) // 12
    assert detail["step_blocks"] == 3457
    assert detail["minimum_step_blocks_to_fit"] == minimum
    assert detail["requested_window"] == dict(start=start, end=args["end"], step_blocks=3457)
    assert detail["coverage_complete"] is False and detail["safe_to_retry"] is False
    assert result.error.recovery_hint["action"] == "inspect_schema"
    projection = project_tool_results([result],
        render_tool_result=lambda item: {"type": "tool_result", "content": item.text()},
        rendered_tool_result_text=lambda item: item.get("content"))
    message = projection.event_blocks[0]["error"]
    assert message == result.text() == result.error.message
    assert f"requires {required} samples" in message
    assert "max_samples allows 30" in message
    assert f"minimum step_blocks to fit max_samples is {minimum}" in message
    assert "Preserve the requested pool, window and tick bounds" in message
    public = json.dumps(result.asdict()) + str(projection)
    for forbidden in (PRIVATE, "/private/", "private.invalid", "secret", "ignored"):
        assert forbidden not in public
    assert args == original
    assert 30 < len(fake.calls) <= 2 + 2 * history.MAX_RESOLVER_READS
    assert {req["method"] for req in fake.calls} == {
        "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber"}


@pytest.mark.parametrize("args,expected", [
    ({"start_block": 100, "end_block": 100, "max_samples": 1}, [100]),
    ({"step_blocks": 4}, [100, 104, 108, 110]),
    ({"step_blocks": 10, "max_samples": 2}, [100, 110]),
])
def test_accepted_sampling_keeps_endpoints_and_budget(args, expected):
    request = {**ARGS, **args}
    original = deepcopy(request)
    assert history._blocks(request) == expected
    assert request == original


def test_request_budget_is_not_raised_or_presented_as_a_sample_only_fix(monkeypatch):
    monkeypatch.setattr(history, "MAX_REQUESTS", 18)
    args = {**ARGS, "end_block": 100, "max_samples": 1}
    with pytest.raises(DataApiError) as caught:
        history._blocks(args)
    assert caught.value.detail["required_samples"] == 1
    assert caught.value.detail["required_requests"] == 19
    assert caught.value.detail["request_budget"] == 18
    assert caught.value.detail["minimum_step_blocks_to_fit"] == 1
    assert "request budget" in caught.value.message
    assert history.MAX_REQUESTS == 18


def test_advertised_schema_explains_endpoint_arithmetic_and_utc_resolution():
    spec = history.uniswap_specs(HeaderOnlyFake(), catalog_http=NoCatalog())[0]
    properties = spec.input_schema["properties"]
    for key in ("step_blocks", "max_samples"):
        description = properties[key]["description"]
        assert "ceil((end_block - start_block) / step_blocks) + 1" in description
        assert "both endpoints" in description
    for key in ("start", "end"):
        assert "only known after" in properties[key]["description"]
    assert "ceil((end_block - start_block) / step_blocks) + 1" in spec.description


@pytest.mark.parametrize("cancel_at,transport_error", [(0, False), (1, False), (4, False),
                                                        (1, True)])
def test_handler_metadata_cancellation_stops_before_next_rpc(cancel_at, transport_error):
    token = CancelToken()
    fake = HeaderOnlyFake()
    request = fake.request
    cancellation = CancelledError("operator_interrupt")

    def cancel_during_request(*args, **kwargs):
        response = request(*args, **kwargs)
        if len(fake.calls) == cancel_at:
            token.cancel("operator_interrupt")
            if transport_error:
                raise cancellation
        return response

    fake.request = cancel_during_request
    if not cancel_at:
        token.cancel("operator_interrupt")
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update(start="2026-09-30", end="2026-10-29", step_blocks=40000, max_samples=30)
    original = deepcopy(args)
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(fake, catalog_http=NoCatalog())[0])
    with pytest.raises(CancelledError, match="operator_interrupt") as caught:
        data_api_handler(ToolCall(name="data_api", metadata={"cancel_token": token}, arguments={
            "op": "call", "provider": "uniswap", "action": "pool_history", "args": args,
        }), registry=registry)
    if transport_error:
        assert caught.value is cancellation
    assert len(fake.calls) == cancel_at
    assert args == original


@pytest.mark.parametrize("stage", ["state", "before_catalog", "catalog_response", "catalog_error"])
def test_handler_cancellation_preserves_exception_through_state_and_catalog(stage):
    from test_uniswap_history_data import ARGS as sample_args, CatalogFake, Fake

    token = CancelToken()
    cancellation = CancelledError("operator_interrupt")

    def mutate(req, doc, fake):
        if (stage == "state" and req["method"] == "eth_call") or (
                stage == "before_catalog" and len(fake.calls) == 53):
            token.cancel("operator_interrupt")
        return 200, doc

    def catalog_effect():
        token.cancel("operator_interrupt")
        if stage == "catalog_error":
            raise cancellation

    fake = Fake(mutate)
    catalog = CatalogFake(effect=catalog_effect)
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(fake, catalog_http=catalog)[0])
    with pytest.raises(CancelledError, match="operator_interrupt") as caught:
        data_api_handler(ToolCall(name="data_api", metadata={"cancel_token": token}, arguments={
            "op": "call", "provider": "uniswap", "action": "pool_history",
            "args": deepcopy(sample_args),
        }), registry=registry)
    assert len(fake.calls) == (4 if stage == "state" else 53)
    assert len(catalog.calls) == (1 if stage.startswith("catalog_") else 0)
    if stage == "catalog_error":
        assert caught.value is cancellation
