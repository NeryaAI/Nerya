"""One offline fixture exercises the bounded SQD discovery seam and integration."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.data_api.sqd import BASE, MAX_BYTES, SqdProvider, sqd_specs
from nerya.data_api.types import DataApiContext, DataApiError
from nerya.harness.cancellation import CancelledError, CancelToken

pytestmark = pytest.mark.smoke
ADDRESS, TOPIC = "0x" + "12" * 20, "0x" + "34" * 32


@pytest.fixture(
    params=[
        "timestamp",
        "full",
        "early",
        "cap",
        "empty",
        "compact",
        "wrong_address",
        "wrong_topic",
        "wrong_order",
        "wrong_parent",
        "wrong_block",
        "malformed",
        "oversize",
        "invalid_bounds",
        "invalid_limit",
        "freeform",
        "bad_timestamp",
        "rate429",
        "capacity529",
        "api500",
        "cloudflare1010",
        "cancel_before",
        "cancel_after",
        "transport",
        "registry",
        "default_fetch",
    ]
)
def sample(request, monkeypatch):
    monkeypatch.setattr(
        "socket.create_connection", lambda *a, **k: pytest.fail("network forbidden")
    )
    monkeypatch.setattr(
        "socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden")
    )
    log = dict(
        address=ADDRESS,
        topics=[TOPIC, TOPIC],
        data="0x00",
        transactionHash="0x" + "56" * 32,
        transactionIndex=0,
        logIndex=0,
    )
    blocks = [
        dict(
            header=dict(
                number=100 + i,
                hash="0x" + f"{i + 1:064x}",
                parentHash="0x" + f"{i:064x}",
                timestamp=1000 + 12 * i,
            ),
            logs=[deepcopy(log)],
        )
        for i in range(2)
    ]
    return request.param, blocks


def test_sqd_bounded_discovery(sample, monkeypatch):
    case, blocks = sample
    args = dict(
        address=ADDRESS, from_block=100, to_block=101, topic0=TOPIC, topic1=TOPIC
    )
    status, headers, calls = 200, {}, []
    token = CancelToken()
    if case == "cancel_before":
        token.cancel()
    # The transport is fake; cancellation uses the real production check.
    context = DataApiContext(cancel_token=token)
    if case == "early":
        blocks = blocks[:1]
    if case == "empty":
        blocks = []
    if case == "cap":
        args["limit"] = 1
    if case == "wrong_address":
        blocks[0]["logs"][0]["address"] = "0x" + "ab" * 20
    if case == "wrong_topic":
        blocks[0]["logs"][0]["topics"][1] = "0x" + "ab" * 32
    if case == "wrong_order":
        blocks[0]["logs"] *= 2
    if case == "wrong_parent":
        blocks[1]["header"]["parentHash"] = "0x" + "ab" * 32
    if case == "wrong_block":
        blocks[1]["header"]["number"] = 102
    if case == "invalid_bounds":
        args["to_block"] = 200
    if case == "invalid_limit":
        args["limit"] = True
    if case == "freeform":
        args["url"] = "https://invalid.example"
    raw = ("\n".join(json.dumps(b) for b in blocks) + "\n").encode()
    if case == "malformed":
        raw = b"{bad json}"
    if case == "oversize":
        raw = b" " * (MAX_BYTES + 1)
    if case in ("timestamp", "bad_timestamp"):
        # Previous block has timestamp 1000; first >=1001 is block101 at1012.
        raw = (
            b'{\n "block_number": 101\n}'
            if case == "timestamp"
            else b'{"block_number":true}'
        )
    if case in ("rate429", "capacity529"):
        status = 429 if case == "rate429" else 529
        headers = {"Retry-After": "7", "X-Request-ID": "req-123"}
        raw = b'{"error":{"type":"rate_limit_error","code":"OVERLOADED","message":"PRIVATE provider prose"}}'
    if case == "api500":
        status = 500
        raw = b'{"error":{"type":"api_error","message":"PRIVATE provider prose"}}'
    if case == "cloudflare1010":
        status, headers = 403, {"CF-RAY": "a565c6f489705ffd"}
        raw = json.dumps(
            dict(
                type="https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/",
                title="Error1010",
                status=403,
                error_code=1010,
                error_name="browser_signature_banned",
                error_category="access_denied",
                ray_id="a565c6f489705ffd",
                retryable=False,
                owner_action_required=True,
                detail="PRIVATE provider prose",
                what_you_should_do="PRIVATE provider instructions",
            )
        ).encode()

    def fetch(method, url, body):
        calls.append((method, url, body))
        if case == "transport":
            raise OSError("PRIVATE transport prose")
        if case == "cancel_after":
            token.cancel()
        return status, headers, raw

    provider = SqdProvider(fetch)

    def run():
        return (
            provider.timestamp_block(context, {"timestamp": 1001})
            if case in ("timestamp", "bad_timestamp")
            else provider.evm_log_slice(context, args)
        )

    if case.startswith("cancel_"):
        with pytest.raises(CancelledError):
            run()
        assert len(calls) == (0 if case == "cancel_before" else 1)
        return
    if case == "cloudflare1010":
        from nerya.data_api.registry import DataApiRegistry
        from nerya.tools.native.data_api import data_api_handler
        from nerya.tools.types import ToolCall

        reg = DataApiRegistry()
        for spec in sqd_specs(fetch):
            reg.register_action(spec)
        out = data_api_handler(
            ToolCall(
                name="data_api",
                arguments=dict(
                    op="call",
                    provider="sqd",
                    action="evm_log_slice",
                    args=args,
                ),
            ),
            registry=reg,
        )
        assert out.is_error and out.error.kind.value == "provider_error"
        assert out.error.retryable is False and len(calls) == 1
        assert out.error.detail == dict(
            source_url=BASE + "/finalized-stream",
            http_status=403,
            error_code=1010,
            error_name="browser_signature_banned",
            error_category="access_denied",
            ray_id="a565c6f489705ffd",
            retryable=False,
            owner_action_required=True,
            op="call",
            provider="sqd",
            action="evm_log_slice",
        )
        assert out.text() == (
            "SQD access blocked by client signature (Cloudflare 1010); "
            "requires provider owner resolution"
        )
        serialized = json.dumps(out.asdict())
        assert (
            "PRIVATE" not in serialized
            and "developers.cloudflare.com" not in serialized
        )
        return
    invalid = {
        "wrong_address",
        "wrong_topic",
        "wrong_order",
        "wrong_parent",
        "wrong_block",
        "malformed",
        "oversize",
        "invalid_bounds",
        "invalid_limit",
        "freeform",
        "bad_timestamp",
    }
    if case in invalid or case in ("rate429", "capacity529", "api500", "transport"):
        with pytest.raises(DataApiError) as caught:
            run()
        error = caught.value
        assert "PRIVATE" not in str(error) + str(error.detail)
        assert len(calls) == (
            0 if case in ("invalid_bounds", "invalid_limit", "freeform") else 1
        )
        if case == "api500":
            assert error.kind == "provider_error" and error.retryable is False
            assert error.detail == dict(
                source_url=BASE + "/finalized-stream",
                http_status=500,
                error_type="api_error",
            )
        if case in ("rate429", "capacity529"):
            assert error.kind == "rate_limit_error" and error.retryable
            assert error.detail == dict(
                source_url=BASE + "/finalized-stream",
                http_status=status,
                retry_after="7",
                retry_after_seconds=7,
                request_id="req-123",
                error_type="rate_limit_error",
                code="OVERLOADED",
            )
        return
    if case == "registry":
        from nerya.data_api.builtins import build_data_api_registry

        reg = build_data_api_registry()
        assert {row["action"] for row in reg.list(provider="sqd")["actions"]} == {
            "timestamp_block",
            "evm_log_slice",
        }
        assert not reg.schema("sqd", "evm_log_slice").get("replay_input", False)
        assert "历史事件" in reg.schema("sqd", "evm_log_slice")["search_terms"]
        with pytest.raises(DataApiError, match="isolated replay"):
            reg.admit_replay_input("sqd", "evm_log_slice", args)
        assert not calls
        return
    if case == "default_fetch":
        from io import BytesIO
        from urllib.request import Request
        from nerya.data_api.sqd import _fetch

        class Response(BytesIO):
            code, headers = 200, {"X-Request-ID": "req-123"}

        def build(handler):
            assert (
                handler.redirect_request(
                    Request(BASE), None, 302, "", {}, BASE + "/other"
                )
                is None
            )

            def open_(req, timeout):
                assert req.full_url == BASE + "/finalized-stream" and timeout == 15
                return Response(raw)

            return SimpleNamespace(open=open_)

        monkeypatch.setattr("nerya.data_api.sqd.build_opener", build)
        assert _fetch("POST", BASE + "/finalized-stream", {}) == (
            200,
            {"X-Request-ID": "req-123"},
            raw,
        )
        return
    result = run()
    envelope = result["_envelope"]
    assert (
        envelope["provider"] == "sqd"
        and envelope["mode"] == "public_read_only"
        and envelope["mock"] is False
    )
    source = envelope["sources"][0]
    assert source["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        source["retrieved_at"].endswith("+00:00")
        and source["hash_basis"] == "raw_response_bytes"
    )
    assert len(calls) == 1 and envelope["coverage"]["replay_input"] is False
    if case == "timestamp":
        assert result["rows"] == [{"timestamp": 1001, "block_number": 101}]
        assert (
            envelope["coverage"]["timestamp_semantics"]
            == "first_block_at_or_after_not_floor"
        )
        assert calls[0] == ("GET", BASE + "/timestamps/1001/block", None)
        return
    coverage = envelope["coverage"]
    assert calls[0][2]["logs"] == [
        {"address": [ADDRESS], "topic0": [TOPIC], "topic1": [TOPIC]}
    ]
    assert calls[0][2]["includeAllBlocks"] is True and calls[0][2]["toBlock"] == 101
    assert coverage["scanned_block_count"] == len(blocks)
    assert coverage["next_block"] == {"early": 101, "empty": 100, "cap": 101}.get(
        case, 102
    )
    assert coverage["sample_truncated"] == (case in ("early", "cap", "empty"))
    assert coverage["completeness"] != "complete"
    if case == "compact":
        from nerya.data_api.registry import DataApiRegistry
        from nerya.llm.tool_compaction import _compact_table_envelope
        from nerya.tools.native.data_api import data_api_handler
        from nerya.tools.types import ToolCall

        reg = DataApiRegistry()
        for spec in sqd_specs(fetch):
            reg.register_action(spec)
        out = data_api_handler(
            ToolCall(
                name="data_api",
                arguments=dict(
                    op="call",
                    provider="sqd",
                    action="evm_log_slice",
                    args=args,
                    limit=1,
                ),
            ),
            registry=reg,
        )
        assert not out.is_error, out.text()
        payload = out.content[0].data
        assert (
            payload["truncated"]
            and len(payload["rows"]) == 1
            and "producer_ref" not in payload
        )
        kept, _ = _compact_table_envelope(payload["_envelope"])
        assert kept["coverage"] == coverage and kept["requested_window"] == dict(
            from_block=100, to_block=101
        )
        assert kept["sources"][0] == payload["_envelope"]["sources"][0]
        assert kept["sources"][0]["sha256"] == source["sha256"]
