"""One offline slice contract; no full-window or fee-reconstruction assertion."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from nerya.data_api import uniswap_events as events
from nerya.data_api.types import DataApiContext, DataApiError
from test_uniswap_history_data import Fake, POOL, digest, selected


@pytest.mark.parametrize("mode", ["swap", "post_mint_burn", "unknown_after_swap",
    "no_swap", "no_swap_changed", "price_mismatch", "duplicate", "reorg", "oversize",
    "range_unchanged", "range_continuous", "range_reset", "range_zero_burn",
    "range_unknown", "range_net_mismatch", "range_gross_mismatch", "range_uninitialized",
    "range_invalid_state", "range_spacing", "invalid_lower_only", "invalid_upper_only",
    "invalid_reversed", "invalid_equal", "invalid_lower_bound", "invalid_upper_bound",
    "invalid_bool", "invalid_string", "timestamp_exact", "timestamp_gap",
    "timestamp_head", "timestamp_future", "timestamp_naive", "timestamp_mixed",
    "batch_missing", "batch_duplicate", "batch_bad_id", "batch_error", "batch_null",
    "keepalive", "keepalive_error", "multi_block_headers", "timestamp_anchor", "timestamp_anchor_hash",
    "timestamp_anchor_future", "timestamp_anchor_head", "timestamp_anchor_time"])
def test_pool_event_slice_checkpoint_evidence_is_always_incomplete(monkeypatch, mode):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    request_log, http_log, response_log, opened, closed = [], [], [], [], []
    liquidity = 1000
    mint_topic = "0x" + keccak(text="Mint(address,address,int24,int24,uint128,uint256,uint256)").hex()
    burn_topic = "0x" + keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)").hex()
    timed = mode.startswith("timestamp_")
    anchored = mode.startswith("timestamp_anchor")
    ranged = mode.startswith("range_") or timed
    changes = []
    if mode == "post_mint_burn":
        changes = [(mint_topic, 0, 60, 10), (burn_topic, -60, 60, 11), (mint_topic, -60, 0, 100)]
    elif mode == "range_continuous":
        changes = [(mint_topic, -120, 120, 10), (burn_topic, -120, 120, 5),
                   (mint_topic, -180, -120, 7), (mint_topic, 120, 180, 9)]
    elif mode == "range_reset":
        changes = [(burn_topic, -120, 120, 99), (mint_topic, -120, 120, 99)]
    elif mode == "range_zero_burn":
        changes = [(burn_topic, -120, 120, 0)]

    def event(topic, index, data="0x"):
        return {"address": POOL, "blockNumber": "0x65", "blockHash": "0x" + format(102, "064x"),
            "transactionIndex": hex(index), "logIndex": hex(index), "transactionHash": "0x" + format(index + 5, "064x"),
            "removed": False, "topics": [topic, "0x" + "00" * 32, "0x" + "00" * 32],
            "data": data, "upstream_extra": "retain raw evidence"}

    swap = event(events.SWAP_TOPIC, 0, "0x" + encode(events.SWAP_TYPES, [1, -1, 2**96, liquidity, 0]).hex())
    unknown = event("0x" + "ff" * 32, 1)
    logs = [] if mode.startswith("no_swap") or anchored else [swap]
    if changes:
        for topic, lower, upper, amount in changes:
            mint = topic == mint_topic
            row = event(topic, len(logs), "0x" + encode(
                ["address", "uint128", "uint256", "uint256"] if mint else ["uint128", "uint256", "uint256"],
                [POOL, amount, 1, 1] if mint else [amount, 1, 1]).hex())
            row["topics"] = [topic, "0x" + encode(["address"], [POOL]).hex(),
                "0x" + encode(["int24"], [lower]).hex(), "0x" + encode(["int24"], [upper]).hex()]
            logs.append(row)
    elif mode in ("unknown_after_swap", "range_unknown"):
        logs += [unknown]
    if mode == "duplicate":
        logs += [deepcopy(swap)]

    def mutate(req, doc, transport):
        if req["method"] == "eth_getBlockByNumber":
            block = int(req["params"][0], 16)
            doc["result"]["parentHash"] = "0x" + format(block, "064x")
            if mode == "reorg" and sum(r["method"] == "eth_getBlockByNumber" for r in request_log) > 3:
                doc["result"]["hash"] = "0x" + "ff" * 32
        if selected(req, "liquidity()"):
            changed = mode in ("post_mint_burn", "no_swap_changed") and req["params"][1] == "0x65"
            added = 5 if mode == "range_continuous" and req["params"][1] == "0x65" else 0
            doc["result"] = "0x" + encode(["uint128"], [liquidity - int(changed) + added]).hex()
        if selected(req, "ticks(int24)"):
            tick = decode(["int24"], bytes.fromhex(req["params"][0]["data"][10:]))[0]
            bound = list(decode(events.TICK_TYPES, bytes.fromhex(doc["result"][2:])))
            if mode == "range_reset":
                bound[1] = 99 if tick == -120 else -99
            if req["params"][1] == "0x65":
                if mode == "range_continuous":
                    bound[:2] = [111, -12] if tick == -120 else [113, -6]
                elif mode == "range_net_mismatch":
                    bound[1] += 1
                elif mode == "range_gross_mismatch":
                    bound[0] += 1
            if mode == "range_uninitialized":
                bound = [0, 0, 0, 0, 0, 0, 0, False]
            elif mode == "range_invalid_state":
                bound[7] = False
            doc["result"] = "0x" + encode(events.TICK_TYPES, bound).hex()
        if mode == "price_mismatch" and selected(req, "slot0()") and req["params"][1] == "0x65":
            doc["result"] = "0x" + encode(events.SLOT_TYPES, [2**96 + 1, 0, 1, 2, 2, 0, True]).hex()
        return 200, doc

    class Rpc(Fake):
        def __enter__(self):
            opened.append(self)
            return self

        def __exit__(self, *args):
            closed.append(self)

        def request(self, method, url, **kwargs):
            req = deepcopy(kwargs["body"])
            http_log.append(req)
            if isinstance(req, list):
                assert 1 < len(req) <= 3 and all(item["method"] == req[0]["method"] for item in req)
                assert req[0]["method"] in ("eth_call", "eth_getBlockByNumber")
                request_log.extend(req)
                documents = []
                for item in req:
                    status, document = super().request(method, url, **{**kwargs, "body": item})
                    assert status == 200
                    documents.append(document)
                response_log.extend(deepcopy(documents))
                if mode == "batch_missing":
                    documents.pop()
                elif mode == "batch_duplicate":
                    documents[-1] = deepcopy(documents[0])
                elif mode == "batch_bad_id":
                    documents[0]["id"] = True
                elif mode in ("batch_error", "keepalive_error"):
                    documents[-1]["error"] = {"code": -32000, "message": "fixture private body"}
                elif mode == "batch_null":
                    documents[-1]["result"] = None
                return 200, list(reversed(documents))
            request_log.append(req)
            if req["method"] == "eth_getLogs":
                assert method == "POST" and url == events.RPC_ORIGINS["drpc"]
                if anchored:
                    assert req["params"][0]["fromBlock"] == req["params"][0]["toBlock"]
                else:
                    assert req["params"] == [{"address": POOL, "fromBlock": "0x65" if timed else "0x64",
                                              "toBlock": hex(args["end_block"]) if not timed else "0x65"}]
                document = {"jsonrpc": "2.0", "id": req["id"], "result":
                    ("x" * events.MAX_RESPONSE_BYTES if mode == "oversize" else list(reversed(logs)))}
                status = 200
            else:
                status, document = super().request(method, url, **kwargs)
            response_log.append(deepcopy(document))
            return status, document

    rpc = Rpc(mutate=mutate, rpc_url=events.RPC_ORIGINS["drpc"])
    owned = mode.startswith("keepalive")
    if owned:
        def keepalive(**kwargs):
            assert kwargs["max_response_bytes"] == 256 * 1024 and kwargs["strict_json"] is True
            assert kwargs["allowed_origins"] == tuple(events.RPC_ORIGINS.values())
            assert kwargs["cancel_token"] is None and kwargs["rate_limit_per_sec"] == 0
            assert events.time.monotonic() < kwargs["deadline"] <= events.time.monotonic() + 120
            return rpc
        monkeypatch.setattr(events, "KeepAliveHttp", keepalive)
    spec = events.uniswap_event_specs(None if owned else rpc)[0]
    assert spec.action == "pool_event_slice" and spec.replay_input is False
    args = dict(pool_address=POOL, rpc_provider="drpc", start_block=100, end_block=101)
    if mode == "multi_block_headers":
        args["end_block"] = 199
    if ranged:
        args.update(tick_lower=-120, tick_upper=121 if mode == "range_spacing" else 120)
    if timed:
        target = 1_600_000_000 + 12 * (1000 if mode in ("timestamp_head", "timestamp_future") else 100)
        target += 1 if mode == "timestamp_future" else 5 if mode == "timestamp_gap" else 0
        args["at_utc"] = datetime.fromtimestamp(target, timezone.utc).isoformat().replace("+00:00", "Z")
        if mode != "timestamp_mixed":
            args.pop("start_block")
            args.pop("end_block")
        if mode == "timestamp_naive":
            args["at_utc"] = args["at_utc"][:-1]
    anchor = None
    if anchored:
        prior = spec.handler(DataApiContext(), {**args, "at_utc":
            datetime.fromtimestamp(target - 300, timezone.utc).isoformat().replace("+00:00", "Z")})["_envelope"]
        a, b = prior["headers"]
        anchor = dict(block_number=a["number"], block_hash=a["hash"],
                      block_timestamp=a["timestamp"], next_block_timestamp=b["timestamp"])
        global_reads = spec.handler(DataApiContext(), args)["_envelope"]["observation_time"]["resolver_reads"]
        request_log.clear()
        http_log.clear()
        response_log.clear()
        if mode == "timestamp_anchor_hash":
            anchor["block_hash"] = "0x" + "ff" * 32
        elif mode == "timestamp_anchor_future":
            anchor.update(block_timestamp=target + 1, next_block_timestamp=target + 13)
        elif mode == "timestamp_anchor_head":
            anchor["block_number"] = 1000
        elif mode == "timestamp_anchor_time":
            anchor["next_block_timestamp"] += 1
        if mode != "timestamp_anchor":
            with pytest.raises(DataApiError, match="anchor"):
                spec.handler(DataApiContext(), args, anchor=anchor)
            assert not any(r["method"] == "eth_call" for r in request_log)
            assert len(request_log) == (0 if mode == "timestamp_anchor_future" else 2 if mode == "timestamp_anchor_head" else 4)
            return
    invalid = {
        "invalid_lower_only": {"tick_lower": -120}, "invalid_upper_only": {"tick_upper": 120},
        "invalid_reversed": {"tick_lower": 120, "tick_upper": -120},
        "invalid_equal": {"tick_lower": 120, "tick_upper": 120},
        "invalid_lower_bound": {"tick_lower": -887273, "tick_upper": 120},
        "invalid_upper_bound": {"tick_lower": -120, "tick_upper": 887273},
        "invalid_bool": {"tick_lower": False, "tick_upper": 120},
        "invalid_string": {"tick_lower": "-120", "tick_upper": 120}}
    args.update(invalid.get(mode, {}))
    if mode in invalid or mode.startswith("batch_") or mode in ("duplicate", "reorg", "oversize", "range_spacing", "range_invalid_state",
                                  "keepalive_error",
                                  "timestamp_head", "timestamp_future", "timestamp_naive", "timestamp_mixed"):
        with pytest.raises(DataApiError) as caught:
            spec.handler(DataApiContext(), args)
        assert caught.value.detail["action"] == "pool_event_slice"
        assert caught.value.detail["coverage_complete"] is False
        assert "fixture private body" not in str(caught.value)
        assert opened == closed == ([rpc] if owned else [])
        if mode.startswith("batch_") or mode == "keepalive_error":
            assert len([body for body in http_log if isinstance(body, list)]) == 1  # No retry/fallback.
        if mode in invalid or mode in ("timestamp_naive", "timestamp_mixed"):
            assert request_log == []
        if mode in ("timestamp_head", "timestamp_future"):
            assert "successor" in caught.value.message
            assert len(request_log) == 3 and not any(r["method"] == "eth_call" for r in request_log)
        if mode == "range_spacing":
            assert any(selected(req, "tickSpacing()") for req in request_log)
            assert not any(selected(req, "ticks(int24)") for req in request_log)
        return
    out = spec.handler(DataApiContext(), args, anchor=anchor)
    assert opened == closed == ([rpc] if owned else [])
    env, rows = out["_envelope"], out["rows"]
    comparison = env["checkpoint_comparison"]
    assert env["incomplete"] is True
    assert env["coverage"]["requested_window_complete"] is False
    assert env["tick_fee_reconstruction_verified"] is False
    assert env["performance_evidence"] is env["financial_execution_allowed"] is False
    assert env["range_receipt"]["complete"] is True
    assert env["range_receipt"]["cryptographic_log_completeness_proven"] is False
    assert sum(r["method"] == "eth_getLogs" for r in request_log) == 1
    assert len(request_log) == env["request_count"] == (37 + env["observation_time"]["resolver_reads"] if timed
        else 131 if mode == "multi_block_headers" else 37 if ranged else 33)
    header_batches = (len(env["headers"]) + 2) // 3
    assert env["http_request_count"] == len(http_log) == env["request_count"] - (20 if ranged else 16) - (len(env["headers"]) - header_batches)
    batches = [body for body in http_log if isinstance(body, list)]
    assert len(batches) == (10 if ranged else 8) + header_batches
    assert all(len(body) == 3 for body in batches if body[0]["method"] == "eth_call")
    header_posts = [body for body in batches if body[0]["method"] == "eth_getBlockByNumber"]
    assert [len(body) for body in header_posts] == [3] * (header_batches - 1) + [len(env["headers"]) % 3 or 3]
    if mode == "multi_block_headers":
        assert env["range_receipt"]["blocks"] == 100
        assert env["headers"] == [{"number": block, "hash": "0x" + format(block + 1, "064x"),
            "parent_hash": "0x" + format(block, "064x"), "timestamp": 1_600_000_000 + block * 12}
            for block in range(99, 200)]
        assert len(header_posts) == 34 < len(env["headers"]) == 101
        assert [int(req["params"][0], 16) for req in request_log
                if req["method"] == "eth_getBlockByNumber"] == [*range(99, 200), 99, 199]
        assert env["http_request_count"] == 48  # Previously 115, with identical 131 logical reads.
    assert env["rpc_response_hashes"] == [
        {"request": req, "request_sha256": digest(req), "response_sha256": digest(doc)}
        for req, doc in zip(request_log, response_log)]
    assert env["request_budget"] == (168 if timed else 135 if ranged else 131)
    assert env["request_count"] <= env["request_budget"] < 170
    assert env["checkpoints"]["initial"]["block_number"] == (100 if timed else 99)
    assert env["checkpoints"]["terminal"]["block_number"] == args.get("end_block", 101)
    assert env["rows_sha256"] == digest(rows)
    evidence = deepcopy(env)
    assert evidence.pop("evidence_sha256") == digest({"rows": rows, "_envelope": evidence})
    assert [row["raw_log"] for row in rows] == logs
    if timed:
        assert env["original_request_args"] == args
        assert env["request_args"] == {**{k: v for k, v in args.items() if k != "at_utc"},
                                       "start_block": 101, "end_block": 101}
        obs = env["observation_time"]
        assert obs["requested_utc"] == args["at_utc"] and obs["timestamp"] == target
        assert obs["floor_block"] == 100 and obs["successor_block"] == 101
        assert obs["floor_timestamp"] <= target < obs["successor_timestamp"]
        assert obs["floor_block_hash"] == env["headers"][0]["hash"]
        assert obs["successor_block_hash"] == env["headers"][1]["hash"]
        assert 0 < obs["resolver_reads"] <= 32 and env["range_receipt"]["blocks"] == 1
        assert obs["resolver_anchor_used"] is anchored
        if anchored:
            assert obs["resolver_reads"] < global_reads
            assert obs["resolver_anchor_block"] == anchor["block_number"] == 75
            assert obs["resolver_anchor_hash"] == anchor["block_hash"]
            headers_read = [int(r["params"][0], 16) for r in request_log if r["method"] == "eth_getBlockByNumber"]
            assert headers_read[:2] == [75, 76] and headers_read[-2:] == [100, 101]
            assert 1000 not in headers_read and 999 not in headers_read
            assert "anchor" not in env["request_args"] and "anchor" not in env["original_request_args"]
    else:
        assert env["request_args"] == args and "observation_time" not in env
    if ranged:
        tick_reads = [req for req in request_log if selected(req, "ticks(int24)")]
        assert [req["params"][1] for req in tick_reads] == (["0x64"] * 2 if timed else ["0x63"] * 2) + ["0x65"] * 2
        continuity = env["range_continuity"]
        assert continuity["verified"] is (timed or mode in ("range_unchanged", "range_continuous", "range_zero_burn"))
        assert bool(continuity["reason_codes"]) is not continuity["verified"]
        for checkpoint in env["checkpoints"].values():
            assert checkpoint["range"]["tick_lower"] == -120
            assert checkpoint["range"]["tick_upper"] == 120
            for side in ("lower", "upper"):
                bound = checkpoint["range"][side]
                assert set(bound) == {"liquidity_gross", "liquidity_net",
                    "fee_growth_outside0_x128", "fee_growth_outside1_x128", "initialized"}
                assert all(type(value) is str for key, value in bound.items() if key != "initialized")
                assert bound["initialized"] is (mode != "range_uninitialized")
                assert bound["fee_growth_outside0_x128"] == ("0" if mode == "range_uninitialized" else str(2**256 - 21))
                assert bound["fee_growth_outside1_x128"] == ("0" if mode == "range_uninitialized" else str(2**256 - 22))
        if mode == "range_reset":
            for side in ("lower", "upper"):
                assert continuity[side]["terminal_match"] is True
                assert continuity[side]["minimum_liquidity_gross"] == "0"
                assert continuity[side]["reason_codes"] == ["boundary_cleared", "boundary_reinitialized"]
        elif mode == "range_continuous":
            assert continuity["lower"]["expected_liquidity_net"] == "-12"
            assert continuity["upper"]["expected_liquidity_net"] == "-6"
        elif mode == "range_unknown":
            assert continuity["reason_codes"] == ["unknown_pool_event"]
    else:
        assert "range_continuity" not in env
        assert all("range" not in checkpoint for checkpoint in env["checkpoints"].values())
    if mode in ("unknown_after_swap", "range_unknown"):
        assert comparison["price_tick_match"] is None
        assert comparison["liquidity_match"] is comparison["checkpoint_match"] is None
        assert comparison["unknown_events"] and comparison["core_reconstruction_verified"] is False
    elif mode == "price_mismatch":
        assert comparison["price_tick_match"] is comparison["checkpoint_match"] is False
    else:
        assert comparison["checkpoint_match"] is (mode != "no_swap_changed")
        if mode == "post_mint_burn":
            assert comparison["expected"]["liquidity"] == "999"
            assert comparison["core_reconstruction_verified"] is True


@pytest.mark.parametrize("mode,retryable", [
    ("timeout", True), ("connection", True), ("httpx_timeout", True), ("httpx_connection", True),
    ("unknown_transport", False), ("malformed_json", False),
    ("http_429", True), ("http_500", True), ("http_599", True),
    ("http_401", False), ("http_403", False), ("http_400", False), ("http_bool", False),
    ("http_quota", False), ("http_auth", False),
    ("rpc_transient", True), ("rpc_rate_limit", True), ("rpc_unknown_30", False),
    ("rpc_denied", False), ("rpc_invalid", False), ("rpc_quota", False),
    ("rpc_reorg", False), ("rpc_source_changed", False), ("rpc_invalid_code", False),
    ("rpc_malformed", False), ("rpc_missing_message", False), ("rpc_bad_code", False), ("rpc_long_denial", False),
    ("batch_denied", False), ("resolver_timeout", True),
    *(("rpc_drpc_" + str(code), False) for code in (4, 32, 9, 10, 53, 52)),
])
def test_event_read_failures_keep_safe_diagnostics_and_known_transience(monkeypatch, mode, retryable):
    import json
    from httpx import ConnectError, ReadTimeout

    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    calls, failed = [], []
    private = "https://user:password@private.invalid/?token=private-secret sk-abcdefghijklmnopqrstuvwx api_key=short-secret"
    messages = {
        "rpc_transient": "backend request failed", "rpc_rate_limit": "rate limit exceeded",
        "rpc_unknown_30": "unclassified upstream failure", "rpc_denied": "timeout: access denied",
        "rpc_invalid": "invalid request: backend request failed", "rpc_quota": "quota exceeded: rate limit",
        "rpc_reorg": "reorg during backend request failed", "rpc_source_changed": "source changed: timeout",
        "rpc_invalid_code": "backend request failed", "rpc_malformed": "backend request failed",
        "rpc_missing_message": "backend request failed",
        "rpc_bad_code": "backend request failed",
        "rpc_long_denial": "backend request failed " + "x " * 300 + "access denied",
        "batch_denied": "backend request failed",
        **{"rpc_drpc_" + str(code): "backend request failed" for code in (4, 32, 9, 10, 53, 52)},
    }

    class Rpc(Fake):
        def request(self, method, url, **kwargs):
            body = kwargs["body"]
            calls.append(deepcopy(body))
            if ((mode == "resolver_timeout" and isinstance(body, dict) and body["method"] == "eth_getBlockByNumber")
                    or (isinstance(body, list) and body[0]["method"] == "eth_call")):
                failed.append(deepcopy(body))
                errors = {"timeout": TimeoutError, "connection": ConnectionError,
                    "httpx_timeout": ReadTimeout, "httpx_connection": ConnectError,
                    "unknown_transport": RuntimeError, "resolver_timeout": TimeoutError}
                if mode in errors:
                    raise errors[mode](private)
                if mode == "malformed_json":
                    raise json.JSONDecodeError(private, private, 0)
                if mode in {"http_quota", "http_auth"}:
                    return (429 if mode == "http_quota" else 503), {"error": {
                        "code": 10 if mode == "http_quota" else 4,
                        "message": "backend request failed " + private, "data": "private-body"}}
                if mode.startswith("http_"):
                    return (True if mode == "http_bool" else int(mode.split("_")[1])), {"raw_body": private}
                documents = [{"jsonrpc": "2.0", "id": item["id"], "result": "0x"} for item in body]
                error = {"code": -32602 if mode == "rpc_invalid_code" else 30,
                         "message": messages[mode] + " " + private, "data": "private-body"}
                if mode.startswith("rpc_drpc_"):
                    error["code"] = int(mode.rsplit("_", 1)[1])
                documents[-1] = {"jsonrpc": "2.0", "id": body[-1]["id"], "error": error}
                if mode == "rpc_malformed":
                    documents[-1]["result"] = "0x"
                if mode == "rpc_missing_message":
                    error.pop("message")
                if mode == "rpc_bad_code":
                    error["code"] = []
                if mode == "batch_denied":
                    documents[0] = {**documents[-1], "id": body[0]["id"]}
                    documents[-1] = {"jsonrpc": "2.0", "id": body[-1]["id"],
                                    "error": {"code": 4, "message": "authorization denied"}}
                return 200, list(reversed(documents))
            return super().request(method, url, **kwargs)

    def header(req, doc, transport):
        if req["method"] == "eth_getBlockByNumber":
            doc["result"]["parentHash"] = "0x" + format(int(req["params"][0], 16), "064x")
        return 200, doc

    rpc = Rpc(mutate=header, rpc_url=events.RPC_ORIGINS["drpc"])
    args = dict(pool_address=POOL, rpc_provider="drpc", start_block=100, end_block=101)
    if mode == "resolver_timeout":
        args = dict(pool_address=POOL, rpc_provider="drpc", at_utc="2020-09-13T12:46:40Z")
    original = deepcopy(args)
    with pytest.raises(DataApiError) as caught:
        events.UniswapEventProvider(rpc).pool_event_slice(DataApiContext(), args)
    error = caught.value
    assert args == original and len(failed) == 1 and calls[-1] == failed[0]  # Producer never retries itself.
    assert error.retryable is error.detail["safe_to_retry"] is retryable
    assert error.detail["coverage_complete"] is False
    assert error.detail["rpc_method"] == ("eth_getBlockByNumber" if mode == "resolver_timeout" else "eth_call")
    if mode in {"http_quota", "http_auth"}:
        assert error.detail["rpc_error_code"] == (10 if mode == "http_quota" else 4)
        assert error.detail["rpc_error_message"].startswith("backend request failed")
    if mode in messages:
        assert error.detail["failure_stage"] == ("response_envelope" if mode in {"rpc_malformed", "rpc_missing_message", "rpc_bad_code"} else "rpc_error")
        if mode == "rpc_bad_code":
            assert "rpc_error_code" not in error.detail
        else:
            assert error.detail["rpc_error_code"] == (int(mode.rsplit("_", 1)[1]) if mode.startswith("rpc_drpc_")
                else 4 if mode == "batch_denied" else -32602 if mode == "rpc_invalid_code" else 30)
        if mode != "rpc_missing_message":
            assert len(error.detail["rpc_error_message"]) <= 500
            assert error.detail["rpc_error_message"].startswith("authorization denied" if mode == "batch_denied" else messages[mode][:20])
    for secret in (private, "private.invalid", "password", "private-secret", "short-secret", "sk-abcdefghijklmnopqrstuvwx", "private-body", "raw_body"):
        assert secret not in str(error.detail) + str(error)
@pytest.mark.parametrize("missed_slot", [False, True])
def test_nearby_timestamp_uses_verified_bracket_before_binary_search(missed_slot):
    from nerya.data_api.uniswap_history import floor_block_at
    reads = []

    def header(number):
        reads.append(number)
        return {"timestamp": number * 12 + (12 if missed_slot and number >= 40 else 0)}

    cache = {0: {"timestamp": 0}, 1: {"timestamp": 12}}
    floor, _ = floor_block_at(900, 100000, header, cache, anchor=0)
    assert floor == (74 if missed_slot else 75)
    assert cache[floor]["timestamp"] <= 900 < cache[floor + 1]["timestamp"]
    assert len(reads) <= 3
