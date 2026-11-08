"""All producer acceptance is offline; no RPC availability claim."""

import hashlib
import json
from datetime import datetime, timezone
from copy import deepcopy
from io import BytesIO

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from nerya.connectors.http import UrllibHttp
from nerya.data_api import uniswap_history as history
from nerya.data_api.registry import DataApiRegistry, compact_data_result
from nerya.data_api.types import DataApiContext, DataApiError

pytestmark = pytest.mark.smoke
POOL = "0x" + "ab" * 20
TOKEN0, TOKEN1 = "0x" + "11" * 20, "0x" + "22" * 20
ARGS = dict(pool_address=POOL, start_block=100, end_block=110, step_blocks=6,
            max_samples=64, tick_lower=-120, tick_upper=120)
BIG = 2**256 - 19


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class Fake:
    def __init__(self, mutate=None, rpc_url=history.RPC_URL):
        self.mutate = mutate
        self.rpc_url = rpc_url
        self.calls = []
        self.responses = []
        self.posts = []

    def request(self, method, url, **kwargs):
        assert method == "POST" and url == self.rpc_url
        assert 0 < kwargs["timeout"] <= history.TIMEOUT_SECONDS
        assert kwargs["headers"] == {"Accept": "application/json"}
        assert "params" not in kwargs
        req = deepcopy(kwargs["body"])
        self.posts.append(req)
        if isinstance(req, list):
            responses = []
            for item in req:
                try:
                    status, document = self.request(method, url, **{**kwargs, "body": item})
                finally:
                    self.posts.pop()  # Nested fixture evaluations are not separate HTTP requests.
                if status != 200:
                    return status, document
                responses.append(document)
            return 200, responses
        self.calls.append(req)
        rpc, params = req["method"], req["params"]
        if rpc == "eth_chainId":
            assert params == []
            result = "0x1"
        elif rpc == "eth_blockNumber":
            assert params == []
            result = hex(1000)
        elif rpc == "eth_getBlockByNumber":
            block = int(params[0], 16)
            assert params == [hex(block), False]
            result = {"number": hex(block), "hash": "0x" + format(block + 1, "064x"),
                      "timestamp": hex(1_600_000_000 + block * 12)}
        else:
            assert rpc == "eth_call"
            tx, tag = params
            assert set(tx) == {"to", "data"} and tag == hex(int(tag, 16))
            target, data = tx["to"], tx["data"]
            assert target in (POOL, TOKEN0, TOKEN1, history.FACTORY)
            outputs = {
                "token0()": (["address"], [TOKEN0]),
                "token1()": (["address"], [TOKEN1]),
                "factory()": (["address"], [history.FACTORY]),
                "fee()": (["uint24"], [3000]),
                "tickSpacing()": (["int24"], [60]),
                "decimals()": (["uint8"], [6 if target == TOKEN0 else 18]),
                "getPool(address,address,uint24)": (["address"], [POOL]),
                "feeAmountTickSpacing(uint24)": (["int24"], [60]),
                "slot0()": (history.SLOT_TYPES, [2**96, 0, 1, 2, 2, 0, True]),
                "liquidity()": (["uint128"], [2**128 - 1]),
                "feeGrowthGlobal0X128()": (["uint256"], [BIG]),
                "feeGrowthGlobal1X128()": (["uint256"], [BIG - 1]),
                "ticks(int24)": (history.TICK_TYPES, [99, -10, BIG - 2, BIG - 3, -1, 2**159, 7, True]),
            }
            signature = next(s for s in outputs if data[2:10] == keccak(text=s)[:4].hex())
            if signature == "getPool(address,address,uint24)":
                assert target == history.FACTORY
                assert decode(["address", "address", "uint24"], bytes.fromhex(data[10:])) == (TOKEN0, TOKEN1, 3000)
            elif signature == "feeAmountTickSpacing(uint24)":
                assert target == history.FACTORY
                assert decode(["uint24"], bytes.fromhex(data[10:])) == (3000,)
            elif signature == "ticks(int24)":
                assert decode(["int24"], bytes.fromhex(data[10:]))[0] in (-120, 120)
            elif signature != "decimals()":
                assert target == POOL
            types, values = outputs[signature]
            result = "0x" + encode(types, values).hex()
        doc = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        status = 200
        if self.mutate:
            status, doc = self.mutate(req, doc, self)
        self.responses.append(deepcopy(doc))
        return status, doc


class CatalogFake:
    def __init__(self, doc=None, status=200, rpc=None, effect=None):
        self.doc = deepcopy(doc) if doc is not None else {
            "name": "Uniswap Labs Default", "version": {"major": 22, "minor": 32, "patch": 0},
            "timestamp": "2026-10-28T00:00:00.000Z",
            "tokens": [dict(chainId=1, address=TOKEN0, symbol="USDC", decimals=6),
                       dict(chainId=1, address=TOKEN1, symbol="WETH", decimals=18)],
        }
        self.status, self.rpc, self.effect = status, rpc, effect
        self.calls = []

    def request(self, method, url, **kwargs):
        assert method == "GET" and url == history.CATALOG_URL
        assert set(kwargs) == {"headers", "timeout"}
        assert kwargs["headers"] == {"Accept": "application/json"}
        assert 0 < kwargs["timeout"] <= 10
        if self.rpc is not None:
            assert len(self.rpc.calls) == 2 + 17 * 3  # Includes all final reorg reads.
        self.calls.append((method, url, kwargs))
        if self.effect:
            self.effect()
        return self.status, deepcopy(self.doc)


FakeCatalog = CatalogFake  # Public fixture spelling for scope integration callers.


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)


def run(fake=None, *, catalog_http=None, **args):
    return history.UniswapHistoryProvider(fake or Fake(), catalog_http=catalog_http or CatalogFake()).pool_history(
        DataApiContext(), {**ARGS, **args})


@pytest.mark.parametrize('status', [401,403])
@pytest.mark.parametrize("provider", ["publicnode", "drpc"])
def test_archive_authorization_gap_retains_scope_and_never_leaks_raw_provider_body(status, provider):
    fake=Fake(lambda req,doc,transport:(status,{'jsonrpc':'2.0','id':req['id'],
        'error':{'code':-32602,'message':'private raw provider body'}}), history.RPC_ORIGINS[provider])
    with pytest.raises(DataApiError) as caught:
        run(fake, rpc_provider=provider)
    error=caught.value
    assert error.kind == 'configuration_required'
    assert error.retryable is False
    assert error.detail['reason'] == 'historical_rpc_authorization_required'
    assert error.detail['safe_to_retry'] is False
    assert error.detail['coverage_complete'] is False
    assert 'private raw' not in str(error)
    assert len(fake.calls)==1


def selected(req, signature):
    return req["method"] == "eth_call" and req["params"][0]["data"][2:10] == keccak(text=signature)[:4].hex()


def changed_state(signature, types, values, block=None):
    def mutate(req, doc, fake):
        if selected(req, signature) and (block is None or req["params"][1] == hex(block)):
            doc["result"] = "0x" + encode(types, values).hex()
        return 200, doc
    return mutate


@pytest.mark.parametrize("provider", [None, "publicnode", "drpc", "tenderly"])
def test_stable_flat_rows_exact_blocks_units_and_auditable_hashes(provider):
    rpc_url = history.RPC_ORIGINS[provider or "publicnode"]
    fake = Fake(rpc_url=rpc_url)
    args = {} if provider is None else {"rpc_provider": provider}
    out = run(fake, **args)
    rows, env = out["rows"], out["_envelope"]
    assert out["source_url"] == env["sources"][0]["url"] == rpc_url
    assert env["rpc_provider"] == (provider or "publicnode")
    assert env["request_args"] == {**ARGS, "rpc_provider": provider or "publicnode"}
    assert [r["block_number"] for r in rows] == [100, 106, 110]
    row = rows[0]
    assert row["chain_id"] == 1 and row["pool_address"] == POOL
    assert (row["token0"], row["token1"], row["decimals"]) == (TOKEN0, TOKEN1, [6, 18])
    assert row["fee"] == 3000 and row["tick_spacing"] == 60 and row["tick"] == 0
    assert row["sqrt_price_x96"] == str(2**96) and row["liquidity"] == str(2**128 - 1)
    assert type(row["timestamp"]) is int and env["timestamp_unit"] == "unix_seconds_utc"
    for index in (0, 1):
        assert row[f"fee_growth_global{index}_x128"] == str(BIG - index)
        for side in ("lower", "upper"):
            assert row[f"tick_{side}_fee_growth_outside{index}_x128"] == str(BIG - 2 - index)
            assert row[f"tick_{side}_initialized"] is True
    assert row["tick_lower"] == -120 and row["tick_upper"] == 120
    assert env["sampled_blocks"] == [100, 106, 110]
    assert [(r["start_block"], r["end_block"]) for r in env["sampled_intervals"]] == [(100, 106), (106, 110)]
    assert env["coverage"]["completeness"] == "not_asserted"
    assert not env["coverage"]["requested_window_complete"]
    assert env["source_row_count"] == 3 and env["provider_truncated"] is False
    assert env["limitations"] == history.LIMITATIONS
    assert env["rows_sha256"] == digest(rows)
    assert env["request_count"] == 2 + 17 * 3 == len(fake.calls)
    assert len(env["sources"]) == 2 <= 8
    assert env["catalog_request_count"] == env["catalog_request_budget"] == 1
    assert env["total_request_count"] == len(fake.calls) + 1
    assert env["total_request_budget"] == history.MAX_REQUESTS + 1
    transcript = hashlib.sha256()
    for source, req, doc in zip(env["rpc_response_hashes"], fake.calls, fake.responses):
        assert source["url"] == rpc_url
        assert source["request"] == req and source["request_sha256"] == digest(req)
        assert source["response_sha256"] == digest(doc)
        pair = json.dumps({"request": req, "response": doc}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        transcript.update(pair + b"\n")
    assert env["sources"][0]["sha256"] == transcript.hexdigest()
    assert env["sources"][0]["request_count"] == len(fake.calls)
    assert env["observation_kind"] == "historical_lp_pool_state"
    for row in rows:
        assert row["row_sha256"] == digest({k: v for k, v in row.items() if k != "row_sha256"})
        assert len(row["source_response_sha256"]) == 17
    assert {req["params"][1] for req in fake.calls if req["method"] == "eth_call"} == {"0x64", "0x6a", "0x6e"}
    assert "latest" not in str(fake.calls)
    compact = compact_data_result("uniswap", "pool_history", out, limit=1)
    assert compact["row_count"] == 3 and compact["truncated"] is True
    assert compact["_envelope"] == env


@pytest.mark.parametrize("start,end,step,expected", [(100, 100, 6, [100]), (100, 112, 6, [100, 106, 112]),
                                                    (100, 110, 99, [100, 110])])
def test_unique_endpoints(start, end, step, expected):
    out = run(start_block=start, end_block=end, step_blocks=step)
    assert [r["block_number"] for r in out["rows"]] == expected


def test_bounded_batches_preserve_64_sample_evidence_and_fail_closed(monkeypatch):
    from nerya.harness.cancellation import CancelToken, CancelledError

    clock = [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    head, now = 26_000_000, int(datetime(2026, 11, 1, 3, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(history.time, "time", lambda: now)
    base = now - head * 12
    def historical_headers(req, doc, fake):
        if req["method"] == "eth_blockNumber":
            doc["result"] = hex(head)
        elif req["method"] == "eth_getBlockByNumber":
            doc["result"]["timestamp"] = hex(base + int(req["params"][0], 16) * 12)
        return 200, doc

    fake = Fake(historical_headers, history.RPC_ORIGINS["drpc"])
    original_request = fake.request
    nested = [False]
    def reordered(*args, **kwargs):
        if nested[0]:
            return original_request(*args, **kwargs)
        clock[0] += 0.3  # Network round trips, not individual batch members.
        nested[0] = True
        try:
            status, doc = original_request(*args, **kwargs)
        finally:
            nested[0] = False
        return status, list(reversed(doc)) if isinstance(doc, list) else doc
    fake.request = reordered
    request = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    request.update(start="2026-10-01T03:00:00Z", end="2026-10-31T03:00:00Z",
                   step_blocks=3430, rpc_provider="drpc")
    original = deepcopy(request)
    out = history.UniswapHistoryProvider(fake, catalog_http=CatalogFake()).pool_history(DataApiContext(), request)
    env, rows = out["_envelope"], out["rows"]
    blocks = list(range(head - 31 * 7200, head - 7200 + 1, 3430))
    if blocks[-1] != head - 7200:
        blocks.append(head - 7200)
    assert request == original and env["request_args"] == original
    assert env["sampled_blocks"] == blocks and len(rows) == 64
    assert env["deadline_seconds"] == history.DEADLINE_SECONDS
    assert clock[0] < history.DEADLINE_SECONDS
    batches = [post for post in fake.posts if isinstance(post, list)]
    # 5*64 sampling waves plus 21 reorg-recheck batches of 3 (64 recheck calls).
    assert [len(batch) for batch in batches] == [3] * (5 * 64 + 21)
    resolver_reads = env["window_resolution"]["start_resolver_reads"] + env["window_resolution"]["end_resolver_reads"]
    assert len(fake.posts) == 408 + resolver_reads
    assert len(fake.calls) == env["request_count"] == 1090 + resolver_reads
    assert env["request_count"] <= env["request_budget"] == history.MAX_REQUESTS
    transcript = hashlib.sha256()
    for evidence, req, doc in zip(env["rpc_response_hashes"], fake.calls, fake.responses, strict=True):
        assert evidence["request"] == req and evidence["request_sha256"] == digest(req)
        assert evidence["response_sha256"] == digest(doc)
        transcript.update(json.dumps({"request": req, "response": doc}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n")
    assert env["sources"][0]["sha256"] == transcript.hexdigest()
    for row in rows:
        per_block = [(req, doc) for req, doc in zip(fake.calls, fake.responses)
                     if req["method"] in ("eth_call", "eth_getBlockByNumber")
                     and req["params"][1 if req["method"] == "eth_call" else 0] == hex(row["block_number"])]
        assert sum(req["method"] == "eth_call" for req, _ in per_block) == 15
        assert row["source_response_sha256"] == [digest(doc) for _, doc in per_block[-17:]]
        assert row["row_sha256"] == digest({k: v for k, v in row.items() if k != "row_sha256"})
    assert env["rows_sha256"] == digest(rows)

    for damage in ("missing", "duplicate", "unknown", "bool_id", "error", "null", "oversize",
                   "cancel", "deadline", "budget", "identity", "malformed_state", "reorg"):
        clock[0] = 0.0
        token = CancelToken()
        broken = Fake()
        transport = broken.request
        nested = [False]
        def corrupt(*args, **kwargs):
            if nested[0]:
                return transport(*args, **kwargs)
            nested[0] = True
            try:
                status, doc = transport(*args, **kwargs)
            finally:
                nested[0] = False
            if isinstance(doc, list):
                body = kwargs.get("body")
                if (damage == "reorg" and isinstance(body, list) and len(broken.calls) > 50
                        and all(request.get("method") == "eth_getBlockByNumber" for request in body)):
                    doc[0]["result"]["hash"] = "0x" + "ff" * 32
                if damage == "missing": doc.pop()
                if damage == "duplicate": doc[-1] = doc[0]
                if damage == "unknown": doc[-1]["id"] = 9999
                if damage == "bool_id": doc[0]["id"] = True
                if damage == "error": doc[-1] = dict(jsonrpc="2.0", id=doc[-1]["id"], error={"code": -32000, "message": "PRIVATE"})
                if damage == "null": doc[-1]["result"] = None
                if damage == "oversize": doc[-1]["extra"] = "x" * history.MAX_RESPONSE_BYTES
                if damage == "cancel": token.cancel("batch_cancel")
                if damage == "deadline": clock[0] = history.DEADLINE_SECONDS + 1
                if damage == "budget": monkeypatch.setattr(history, "MAX_REQUESTS", len(broken.calls))
                if damage == "identity" and len(broken.posts) == 11:
                    doc[0]["result"] = "0x" + encode(["uint8"], [8]).hex()
                if damage == "malformed_state" and len(broken.posts) == 6:
                    doc[1]["result"] = "0x"
            return status, doc
        broken.request = corrupt
        with pytest.raises(CancelledError if damage == "cancel" else DataApiError) as caught:
            history.UniswapHistoryProvider(broken, catalog_http=CatalogFake()).pool_history(
                DataApiContext(cancel_token=token), deepcopy(ARGS))
        assert "PRIVATE" not in str(caught.value)
        if isinstance(caught.value, DataApiError):
            assert caught.value.retryable is False and caught.value.detail["coverage_complete"] is False
        monkeypatch.setattr(history, "MAX_REQUESTS", 2 + 17 * history.MAX_SAMPLES + 2 * history.MAX_RESOLVER_READS)


def test_drpc_free_plan_batch_cap_preserves_evidence(monkeypatch):
    # The comparison covers provenance too, including the retrieval timestamp.
    monkeypatch.setattr(history.time, "time", lambda: 1793404800)
    fake = Fake(rpc_url=history.RPC_ORIGINS["drpc"])
    request = fake.request
    nested, posts = [False], []
    def free_plan(*args, **kwargs):
        if nested[0]:
            return request(*args, **kwargs)
        body = deepcopy(kwargs["body"])
        posts.append(body)
        if isinstance(body, list) and len(body) > 3:
            return 500, [dict(jsonrpc="2.0", id=item["id"], error={"code": 31,
                "message": "Batch of more than 3 requests are not allowed on free plan"})
                for item in body]
        nested[0] = True
        try:
            return request(*args, **kwargs)
        finally:
            nested[0] = False
    fake.request = free_plan
    baseline = run(Fake(rpc_url=history.RPC_ORIGINS["drpc"]), rpc_provider="drpc")
    out = run(fake, rpc_provider="drpc")
    assert out == baseline
    assert all(not isinstance(body, list) or len(body) <= 3 for body in posts)
    assert len(posts) == 21  # chain/head + 3 * (2 + 4 chunks) + 1 batched recheck.
    assert out["_envelope"]["request_count"] == 53


def test_owned_transport_overlaps_only_two_chunks_with_identical_evidence(monkeypatch):
    import threading
    from nerya.connectors.http import KeepAliveHttp
    from nerya.harness.cancellation import CancelToken, CancelledError

    baseline = run(end_block=163, step_blocks=1)
    clock, mode = [0.0], ["normal"]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    def elapsed():
        clock[0] += 0.45
    lock, barrier = threading.Lock(), threading.Barrier(2, action=elapsed)
    active, peak, closed, bodies = [0], [0], [], []
    lane_active = {}
    def enter(self):
        self.deadline = history.time.monotonic() + history.DEADLINE_SECONDS
        return self
    def request(self, method, url, **kwargs):
        body = kwargs["body"]
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            bodies.append(deepcopy(body))
            lane_active[id(self)] = lane_active.get(id(self), 0) + 1
            assert lane_active[id(self)] == 1
        try:
            pure_header_batch = (isinstance(body, list) and body
                                 and all(request["method"] == "eth_getBlockByNumber" for request in body))
            needs_barrier = (not pure_header_batch if isinstance(body, list)
                             else body["method"] == "eth_call")
            if needs_barrier:
                assert not isinstance(body, list) or len(body) <= 3
                barrier.wait(timeout=0.5)  # Serial chunking fails here.
            else:
                elapsed()
            status, doc = Fake().request(method, url, **kwargs)
            if isinstance(body, list) and body[0]["id"] == 6:
                if mode[0] == "id": doc[-1]["id"] = 9999
                if mode[0] == "rpc": doc[-1] = dict(jsonrpc="2.0", id=body[-1]["id"], error={"code": 31})
                if mode[0] == "transport": raise OSError("PRIVATE")
                if mode[0] == "cancel": token.cancel("stop")
                if mode[0] == "deadline": clock[0] = history.DEADLINE_SECONDS + 1
            return status, doc
        finally:
            with lock:
                active[0] -= 1
                lane_active[id(self)] -= 1
    monkeypatch.setattr(KeepAliveHttp, "__init__", lambda self, **kwargs: None)
    monkeypatch.setattr(KeepAliveHttp, "__enter__", enter)
    monkeypatch.setattr(KeepAliveHttp, "__exit__", lambda self, *args: closed.append(id(self)))
    monkeypatch.setattr(KeepAliveHttp, "request", request)
    result = history.UniswapHistoryProvider(catalog_http=CatalogFake()).pool_history(
        DataApiContext(), {**ARGS, "end_block": 163, "step_blocks": 1})
    assert peak[0] == 2 and active[0] == 0
    assert len(closed) == len(set(closed)) == 2
    assert all(count == 0 for count in lane_active.values())
    assert result["rows"] == baseline["rows"]
    assert result["_envelope"]["rpc_response_hashes"] == baseline["_envelope"]["rpc_response_hashes"]
    assert result["_envelope"]["request_count"] == 1090
    assert len(bodies) == 408  # Batched reorg recheck replaces 64 singles with 22 waves.
    assert clock[0] == pytest.approx(216 * 0.45)
    for failure in ("id", "rpc", "transport", "cancel", "deadline"):
        clock[0], mode[0] = 0.0, failure
        bodies.clear()
        closed.clear()
        token = CancelToken()
        with pytest.raises(CancelledError if failure == "cancel" else DataApiError) as caught:
            history.UniswapHistoryProvider(catalog_http=CatalogFake()).pool_history(
                DataApiContext(cancel_token=token), ARGS)
        assert len(bodies) == 4  # chain/head and only the first pair, no next wave.
        assert len(closed) == 2 and active[0] == 0
        assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("args", [
    {"start_block": True}, {"end_block": "110"}, {"step_blocks": 0}, {"step_blocks": -1},
    {"max_samples": 65}, {"max_samples": True}, {"max_samples": 0}, {"max_samples": 2},
    {"start_block": -1}, {"start_block": 111}, {"end_block": 2**64},
    {"end_block": 2**64 - 1, "step_blocks": 1}, {"tick_lower": -887273},
    {"tick_upper": 887273}, {"tick_lower": 120}, {"tick_lower": -120.0},
    {"pool_address": "0x" + "00" * 20}, {"pool_address": "https://evil.invalid"},
    {"rpc_url": "http://127.0.0.1"}, {"chain_id": 10}, {"endpoint": history.RPC_URL},
    *[{"rpc_provider": value} for value in ("unknown", "DRPC", "", None, True, 1, [], {},
        "https://eth.drpc.org", "https://ethereum-rpc.publicnode.com", "http://127.0.0.1")],
])
def test_bad_arguments_fail_before_io(args):
    fake = Fake()
    with pytest.raises(DataApiError) as exc:
        run(fake, **args)
    assert exc.value.kind == "schema_validation" and not fake.calls


def test_missing_argument_fails_before_io():
    fake = Fake()
    with pytest.raises(DataApiError):
        history.UniswapHistoryProvider(fake).pool_history(DataApiContext(), {})
    assert not fake.calls


@pytest.mark.parametrize("mutate", [
    changed_state("factory()", ["address"], [TOKEN0]),
    changed_state("getPool(address,address,uint24)", ["address"], [TOKEN0]),
    changed_state("getPool(address,address,uint24)", ["address"], ["0x" + "00" * 20]),
    changed_state("token1()", ["address"], [TOKEN0]),
    changed_state("token0()", ["address"], ["0x" + "00" * 20]),
    changed_state("token1()", ["address"], ["0x" + "33" * 20], block=106),
    changed_state("decimals()", ["uint8"], [8], block=106),
    changed_state("fee()", ["uint24"], [500], block=106),
    changed_state("fee()", ["uint24"], [1_000_000]),
    changed_state("tickSpacing()", ["int24"], [0]),
    changed_state("tickSpacing()", ["int24"], [-60]),
    changed_state("feeAmountTickSpacing(uint24)", ["int24"], [10]),
    changed_state("slot0()", history.SLOT_TYPES, [0, 0, 0, 0, 0, 0, False]),
    changed_state("slot0()", history.SLOT_TYPES, [2**96, 887273, 0, 0, 0, 0, True]),
    changed_state("ticks(int24)", history.TICK_TYPES, [0, 0, 10, 0, 0, 0, 0, False]),
    changed_state("ticks(int24)", history.TICK_TYPES, [1, 2, 0, 0, 0, 0, 0, True]),
])
@pytest.mark.parametrize("provider", ["publicnode", "drpc"])
def test_wrong_or_inconsistent_identity_and_state_fail_closed(mutate, provider):
    with pytest.raises(DataApiError) as exc:
        run(Fake(mutate, history.RPC_ORIGINS[provider]), rpc_provider=provider)
    assert exc.value.detail["coverage_complete"] is False


def test_spacing_validated_and_uninitialized_ticks_retained_without_fabrication():
    with pytest.raises(DataApiError, match="align"):
        run(tick_lower=-119)
    out = run(Fake(changed_state("ticks(int24)", history.TICK_TYPES, [0, 0, 0, 0, 0, 0, 0, False])))
    assert out["rows"][0]["tick_lower_initialized"] is False
    assert out["rows"][0]["tick_lower_fee_growth_outside0_x128"] == "0"


@pytest.mark.parametrize("rpc,result", [("eth_chainId", "0xa"), ("eth_chainId", "0x01"),
    ("eth_chainId", True), ("eth_blockNumber", "0x65"), ("eth_getBlockByNumber", None),
    ("eth_getBlockByNumber", {}), ("eth_call", "0x"), ("eth_call", "0xZZ"),
    ("eth_call", "0x" + "ff" * 32)])
@pytest.mark.parametrize("provider", ["publicnode", "drpc"])
def test_chain_future_missing_malformed_state(rpc, result, provider):
    def mutate(req, doc, fake):
        if req["method"] == rpc:
            doc["result"] = result
        return 200, doc
    with pytest.raises(DataApiError):
        run(Fake(mutate, history.RPC_ORIGINS[provider]), rpc_provider=provider)


@pytest.mark.parametrize("mode", ["number", "hash", "timestamp", "future_timestamp", "reorg"])
def test_block_identity_order_and_reorg(mode):
    def mutate(req, doc, fake):
        if req["method"] == "eth_getBlockByNumber":
            if mode == "number": doc["result"]["number"] = "0x1"
            if mode == "hash": doc["result"]["hash"] = "0x00"
            if mode == "timestamp": doc["result"]["timestamp"] = "0x1"
            if mode == "future_timestamp": doc["result"]["timestamp"] = hex(2**63)
            if mode == "reorg" and len(fake.calls) > 2 + 16 * 3:
                doc["result"]["hash"] = "0x" + "ff" * 32
        return 200, doc
    with pytest.raises(DataApiError): run(Fake(mutate))


@pytest.mark.parametrize("mode", ["http", "rpc", "id", "version", "exception", "oversize", "nan"])
def test_errors_redact_bodies_and_fail_closed(mode):
    secret = "DO_NOT_EXPOSE_RAW_BODY_OR_EXCEPTION"
    def mutate(req, doc, fake):
        if mode == "exception": raise RuntimeError(secret)
        if mode == "http": return 500, {"raw": secret}
        if mode == "rpc": doc["error"] = {"message": secret}
        if mode == "id": doc["id"] = 99
        if mode == "version": doc["jsonrpc"] = "1.0"
        if mode == "oversize": doc["extra"] = secret * history.MAX_RESPONSE_BYTES
        if mode == "nan": doc["extra"] = float("nan")
        return 200, doc
    with pytest.raises(DataApiError) as exc: run(Fake(mutate))
    assert secret not in str(exc.value) + str(exc.value.detail)
    assert exc.value.retryable is False


@pytest.mark.parametrize("provider", ["publicnode", "drpc"])
def test_request_budget_preflight_and_runtime(monkeypatch, provider):
    monkeypatch.setattr(history, "MAX_REQUESTS", 18)
    fake = Fake(rpc_url=history.RPC_ORIGINS[provider])
    with pytest.raises(DataApiError, match="budget"):
        run(fake, start_block=100, end_block=100, rpc_provider=provider)
    assert fake.calls == []
    monkeypatch.setattr(history, "MAX_REQUESTS", 1090)
    def exhaust(req, doc, fake):
        if len(fake.calls) == 2: monkeypatch.setattr(history, "MAX_REQUESTS", 2)
        return 200, doc
    fake = Fake(exhaust, history.RPC_ORIGINS[provider])
    with pytest.raises(DataApiError, match="budget"): run(fake, rpc_provider=provider)
    assert len(fake.calls) == 2


@pytest.mark.parametrize("during_request", [False, True])
def test_deadline_is_total_not_per_request(monkeypatch, during_request):
    clock = [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    def expire(req, doc, fake):
        clock[0] = history.DEADLINE_SECONDS + 1
        return 200, doc
    fake = Fake(expire)
    if not during_request: monkeypatch.setattr(history, "DEADLINE_SECONDS", 0)
    with pytest.raises(DataApiError, match="deadline"): run(fake)
    assert len(fake.calls) == int(during_request)


def test_maximum_samples_request_ceiling():
    fake = Fake()
    out = run(fake, start_block=100, end_block=163, step_blocks=1)
    assert len(out["rows"]) == 64
    assert len(fake.calls) == 1090 <= history.MAX_REQUESTS


def test_transport_caps_and_no_custom_origin():
    http = history.UniswapHistoryProvider().http
    assert isinstance(http, UrllibHttp)
    assert history.RPC_ORIGINS == {"publicnode": "https://ethereum-rpc.publicnode.com",
                                   "drpc": "https://eth.drpc.org",
                                   "tenderly": "https://mainnet.gateway.tenderly.co"}
    assert http.allowed_origins == tuple(history.RPC_ORIGINS.values()) and http.strict_json
    assert http.rate_limit_per_sec == 0
    for url in history.RPC_ORIGINS.values():
        http._check_origin(url)
    assert http.max_response_bytes == history.MAX_RESPONSE_BYTES
    with pytest.raises(ValueError): http._read_json(BytesIO(b"x" * (history.MAX_RESPONSE_BYTES + 1)))
    for url in ("http://ethereum-rpc.publicnode.com", "https://user@ethereum-rpc.publicnode.com",
                "http://eth.drpc.org", "https://user@eth.drpc.org", "https://evil.invalid", "http://127.0.0.1"):
        with pytest.raises(ValueError): http._check_origin(url)


def test_history_rpc_keepalive_is_per_run_bounded_strict_and_cookie_free(monkeypatch):
    import httpx
    from nerya.connectors.http import KeepAliveHttp
    from nerya.harness.cancellation import CancelToken, CancelledError

    fake, requests, transports, closed, recorded = Fake(), [], [], [], []
    mode = ['normal']
    token = CancelToken()
    clock = [0.0]
    monkeypatch.setattr(history.time, 'monotonic', lambda: clock[0])
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if mode[0] == 'cancel': token.cancel('test')
            if mode[0] == 'deadline': clock[0] = 181.0
            yield b'{"ok":true}'
        def close(self): pass
    def handle(request):
        requests.append(request)
        assert 'cookie' not in request.headers
        if mode[0] == 'redirect':
            return httpx.Response(307, headers={'Location': 'https://evil.invalid/rpc'})
        if mode[0] == 'duplicate': return httpx.Response(200, content=b'{"x":1,"x":2}')
        if mode[0] == 'oversize': return httpx.Response(200, content=b'x' * 65)
        if mode[0] in ('cancel', 'deadline'): return httpx.Response(200, stream=Stream())
        status, doc = fake.request('POST', str(request.url), body=json.loads(request.content),
            timeout=10, headers={'Accept': 'application/json'})
        return httpx.Response(status, json=doc, headers={'Set-Cookie': 'secret=value'})
    class Transport(httpx.MockTransport):
        def close(self): closed.append(self); super().close()
    def transport(**kwargs):
        assert kwargs['retries'] == 0 and kwargs['limits'].max_connections == 1
        instance = Transport(handle); transports.append(instance); return instance
    monkeypatch.setattr(httpx, 'HTTPTransport', transport)
    monkeypatch.setattr('nerya.connectors.http.devmode.record_http', lambda **kw: recorded.append(kw))
    provider = history.UniswapHistoryProvider(catalog_http=CatalogFake())
    assert len(provider.pool_history(DataApiContext(), ARGS)['rows']) == 3
    assert len(transports) == len(closed) == 2 and len(requests) == len(fake.posts) == 21
    assert len(fake.calls) == 53 and len(recorded) == 21
    for failure in ('redirect', 'duplicate', 'oversize', 'cancel', 'deadline'):
        mode[0] = failure
        token.reset()
        clock[0] = 0.0
        before = len(requests)
        with pytest.raises((ValueError, TimeoutError, CancelledError)):
            with KeepAliveHttp(max_response_bytes=64, strict_json=True, rate_limit_per_sec=0,
                    allowed_origins=(history.RPC_URL,), deadline=180.0, cancel_token=token) as http:
                http.request('POST', history.RPC_URL, body={}, timeout=10)
        assert len(requests) == before + 1
        assert closed[-1] is transports[-1]


def test_discovery_registration_and_context_free_replay(monkeypatch):
    from nerya.data_api import build_data_api_registry
    monkeypatch.setattr(UrllibHttp, "request", lambda *a, **kw: pytest.fail("discovery performed IO"))
    reg = build_data_api_registry()
    schema = reg.schema("uniswap", "pool_history")
    assert schema["replay_input"] is True and schema["input_schema"] == history.INPUT_SCHEMA
    listed = reg.list(provider="uniswap")
    assert listed["count"] == 3
    assert {(row["action"], row.get("replay_input", False)) for row in listed["actions"]} == {
        ("pool_history", True), ("pool_discover", False), ("pool_event_slice", False),
    }
    isolated = DataApiRegistry()
    isolated.register_action(history.uniswap_specs(Fake(), catalog_http=CatalogFake())[0])
    out = isolated.call_replay_input("uniswap", "pool_history", ARGS)
    assert len(out["rows"]) == 3
    class ForbiddenContext:
        def __getattribute__(self, name): pytest.fail("context access forbidden")
    assert history.UniswapHistoryProvider(Fake(), catalog_http=CatalogFake()).pool_history(ForbiddenContext(), ARGS)["rows"]


def test_optional_abi_dependency_failure_is_structured_and_offline(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def without_abi(name, *args, **kwargs):
        if name == "eth_abi": raise ImportError("private import detail")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_abi)
    fake = Fake()
    with pytest.raises(DataApiError, match="ABI dependencies") as exc: run(fake)
    assert not fake.calls and "private import detail" not in str(exc.value)


def dates(fake=None, **changes):
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update({"start": iso(1_600_001_205), "end": iso(1_600_001_327), **changes})
    return history.UniswapHistoryProvider(fake or Fake(), catalog_http=CatalogFake()).pool_history(DataApiContext(), args)


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("provider", ["publicnode", "drpc"])
def test_utc_floor_resolution_preserves_original_window_and_gaps(provider):
    fake = Fake(rpc_url=history.RPC_ORIGINS[provider])
    out = dates(fake, rpc_provider=provider)
    env = out["_envelope"]
    assert env["rpc_provider"] == env["request_args"]["rpc_provider"] == provider
    assert env["request_args"] == {**{k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")},
                                    "start": iso(1_600_001_205), "end": iso(1_600_001_327),
                                    "rpc_provider": provider}
    assert env["requested_window"] == dict(start=iso(1_600_001_205), end=iso(1_600_001_327), step_blocks=6)
    assert env["sampled_blocks"] == [100, 106, 110]
    resolution = env["window_resolution"]
    assert resolution["start_gap_seconds"] == 5 and resolution["end_gap_seconds"] == 7
    assert resolution["start_timestamp"] == out["rows"][0]["timestamp"]
    assert resolution["end_timestamp"] == out["rows"][-1]["timestamp"]
    assert not resolution["exact_start"] and not resolution["exact_end"]
    assert resolution["start_resolver_reads"] <= 32 and resolution["end_resolver_reads"] <= 32
    assert len(fake.calls) <= 2 + 64 + 17 * 3
    assert "latest" not in str(fake.calls)


def test_recent_utc_resolution_does_not_probe_old_fixed_midpoint():
    head_block = 26_000_000
    head_timestamp = int(history.time.time()) - 60
    base_timestamp = head_timestamp - head_block * 12
    target_start = head_timestamp - 30 * 86400
    target_end = head_timestamp - 60

    def reject_old_midpoint(req, doc, fake):
        if req["method"] == "eth_blockNumber":
            doc["result"] = hex(head_block)
        if req["method"] == "eth_getBlockByNumber":
            block = int(req["params"][0], 16)
            doc["result"]["timestamp"] = hex(base_timestamp + block * 12)
            if block == head_block // 2:
                return 200, {"jsonrpc": "2.0", "id": req["id"],
                             "error": {"code": 4444, "message": "old archive midpoint"}}
        return 200, doc

    fake = Fake(reject_old_midpoint)
    out = dates(fake, start=iso(target_start), end=iso(target_end),
                step_blocks=3429, max_samples=64)
    resolution = out["_envelope"]["window_resolution"]
    assert resolution["start_block"] == head_block - 216000
    assert resolution["end_block"] == head_block - 5
    assert resolution["start_timestamp"] == target_start
    assert resolution["end_timestamp"] == target_end
    assert resolution["start_gap_seconds"] == 0
    assert resolution["end_gap_seconds"] == 0
    assert resolution["start_resolver_reads"] <= history.MAX_RESOLVER_READS
    assert resolution["end_resolver_reads"] <= history.MAX_RESOLVER_READS
    assert all(
        not (call["method"] == "eth_getBlockByNumber"
             and call["params"][0] == hex(head_block // 2))
        for call in fake.calls
    )


def test_date_only_midnight_and_exact_endpoint():
    # The fake genesis is September 13; September 14 midnight has floor block 3733.
    def larger_head(req, doc, fake):
        if req["method"] == "eth_blockNumber": doc["result"] = hex(10000)
        return 200, doc
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update(start="2020-09-14", end="2020-09-14")
    out = history.UniswapHistoryProvider(Fake(larger_head), catalog_http=CatalogFake()).pool_history(DataApiContext(), args)
    assert out["_envelope"]["requested_window"]["start"] == "2020-09-14"
    assert len(out["rows"]) == 1
    out = dates(end=iso(1_600_001_320))
    assert out["_envelope"]["window_resolution"]["exact_end"] is True


@pytest.mark.parametrize("change", [{"start": "2020-01-01T00:00:00+08:00"}, {"start": "2020-02-30"},
    {"start": "2099-01-01"}, {"start": True}, {"start": "2020-10-01"},
    {"start_block": 100}, {"end_block": 110}, {"rpc_url": "http://localhost"}])
def test_invalid_utc_arguments_fail_before_io(change):
    fake = Fake()
    with pytest.raises(DataApiError): dates(fake, **change)
    assert not fake.calls


def test_utc_before_genesis_and_shared_sample_budget():
    with pytest.raises(DataApiError, match="genesis"): dates(start="2020-01-01")
    with pytest.raises(DataApiError, match="budget"): dates(max_samples=2)


def test_binary_search_ceiling_and_nonchronological_evidence(monkeypatch):
    monkeypatch.setattr(history, "MAX_RESOLVER_READS", 2)
    fake = Fake()
    with pytest.raises(DataApiError, match="resolver read budget"): dates(fake)
    assert len(fake.calls) == 4  # chain, head, genesis, head header; no fallback/guess.
    monkeypatch.setattr(history, "MAX_RESOLVER_READS", 32)
    def unordered(req, doc, fake):
        if req["method"] == "eth_getBlockByNumber" and req["params"][0] == hex(100):
            doc["result"]["timestamp"] = hex(1_600_020_000)
        return 200, doc
    with pytest.raises(DataApiError, match="Nonchronological"): dates(Fake(unordered))


def test_utc_resolver_uses_shared_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    def expire(req, doc, fake):
        if req["method"] == "eth_getBlockByNumber": clock[0] = history.DEADLINE_SECONDS + 1
        return 200, doc
    fake = Fake(expire)
    with pytest.raises(DataApiError, match="deadline"): dates(fake)
    assert len(fake.calls) == 3


def test_utc_replay_entry_and_missing_pair():
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(Fake(), catalog_http=CatalogFake())[0])
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update(start=iso(1_600_001_205), end=iso(1_600_001_327))
    assert len(registry.call_replay_input("uniswap", "pool_history", args)["rows"]) == 3
    del args["end"]
    with pytest.raises(DataApiError): registry.call_replay_input("uniswap", "pool_history", args)


def test_native_handler_preserves_utc_resolution_and_rows():
    from nerya.tools.native.data_api import data_api_handler
    from nerya.tools.types import ToolCall
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(Fake(), catalog_http=CatalogFake())[0])
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update(start=iso(1_600_001_205), end=iso(1_600_001_327))
    result = data_api_handler(ToolCall(name="data_api", arguments={
        "op": "call", "provider": "uniswap", "action": "pool_history", "args": args,
        "limit": 64}), registry=registry)
    assert not result.is_error, result.text()
    data = result.content[0].data
    assert data["row_count"] == 3 and not data["truncated"]
    assert data["_envelope"]["requested_window"]["start"] == args["start"]
    assert data["_envelope"]["window_resolution"]["start_gap_seconds"] == 5


def test_actual_prepare_inputs_accepts_aggregated_source_and_binds_snapshot(monkeypatch, tmp_path):
    from nerya.skills.builtin.backtest.scripts import replay_inputs
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(Fake(), catalog_http=CatalogFake())[0])
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    args.update(start=iso(1_600_001_205), end=iso(1_600_001_327))
    manifest = {"version": 1, "datasets": [{"id": "pool", "provider": "uniswap",
                 "action": "pool_history", "args": args}]}
    monkeypatch.setattr(replay_inputs, "read_manifest", lambda root: deepcopy(manifest))
    prepared = replay_inputs.prepare_inputs(tmp_path, registry=registry)
    blob, receipt = prepared["files"]["pool.json"], prepared["receipts"][0]
    assert hashlib.sha256(blob).hexdigest() == receipt["sha256"]
    assert len(receipt["sources"]) == 2 <= 8
    assert receipt["observation_kind"] == "historical_lp_pool_state"
    assert receipt["requested_window_complete"] is not True
    data = json.loads(blob)
    assert data["_envelope"]["rows_sha256"] == digest(data["rows"])
    assert data["_envelope"]["sources"] == receipt["sources"]
    assert receipt["requested_window"]["start"] == args["start"]


def test_catalog_identity_is_once_after_rpc_exact_ordered_current_and_auditable():
    fake = Fake()
    catalog = CatalogFake(rpc=fake)
    # Catalog order and address case do not determine token0/token1 ordering.
    catalog.doc["tokens"].reverse()
    catalog.doc["tokens"][0]["address"] = "0x" + TOKEN1[2:].upper()
    out = run(fake, catalog_http=catalog)
    env = out["_envelope"]
    assert len(catalog.calls) == 1
    assert env["token_identities"] == [
        dict(chain_id=1, address=address, symbol=symbol, decimals=decimals,
             catalog_source_sha256=digest(catalog.doc), identity_scope="current_official_catalog")
        for address, symbol, decimals in ((TOKEN0, "USDC", 6), (TOKEN1, "WETH", 18))]
    source = env["sources"][1]
    assert source["url"] == history.CATALOG_URL and source["sha256"] == digest(catalog.doc)
    assert source["catalog_name"] == catalog.doc["name"]
    assert source["catalog_version"] == catalog.doc["version"]
    assert source["catalog_timestamp"] == catalog.doc["timestamp"]
    assert type(source["retrieved_at"]) is int
    assert source["hash_basis"] == "canonical_parsed_json_utf8_sorted_keys"
    assert env["pool_identity_scope"] == "historical_onchain_pool_with_current_token_catalog"
    assert env["token_identity_time_scope"] == "current_catalog_not_historical_token_metadata"
    assert env["token_identity_status"] == "verified"
    assert "tokens" not in source and "catalog" not in out
    assert not env["coverage"]["requested_window_complete"]
    assert env["rows_sha256"] == digest(out["rows"])
    assert all("symbol" not in row and "token_identities" not in row for row in out["rows"])
    assert "ETH" not in [token["symbol"] for token in env["token_identities"]]


@pytest.mark.parametrize("damage", ["missing", "wrong_chain", "bool_chain", "wrong_address",
    "decimals", "bool_decimals", "duplicate_address", "conflicting_address", "symbol_collision",
    "symbol_case_collision", "same_pair_symbol", "unicode", "prose", "long_symbol", "missing_symbol"])
def test_unknown_catalog_identity_preserves_history_without_scope_or_coverage_claim(damage):
    catalog = CatalogFake()
    tokens = catalog.doc["tokens"]
    if damage == "missing": tokens.pop()
    if damage == "wrong_chain": tokens[1]["chainId"] = 10
    if damage == "bool_chain": tokens[1]["chainId"] = True
    if damage == "wrong_address": tokens[1]["address"] = "0x" + "44" * 20
    if damage == "decimals": tokens[1]["decimals"] = 6
    if damage == "bool_decimals": tokens[1]["decimals"] = True
    if damage == "duplicate_address": tokens.append(deepcopy(tokens[0]))
    if damage == "conflicting_address": tokens.append({**tokens[0], "symbol": "OTHER"})
    if damage in {"symbol_collision", "symbol_case_collision"}:
        tokens.append({**tokens[0], "address": "0x" + "33" * 20,
                       "symbol": "USDC" if damage == "symbol_collision" else "usdc"})
    if damage == "same_pair_symbol": tokens[1]["symbol"] = "USDC"
    if damage == "unicode": tokens[1]["symbol"] = "WΕTH"
    if damage == "prose": tokens[1]["symbol"] = "WETH / ETH"
    if damage == "long_symbol": tokens[1]["symbol"] = "W" * 33
    if damage == "missing_symbol": tokens[1].pop("symbol")
    out = run(catalog_http=catalog)
    env = out["_envelope"]
    assert env["token_identities"] == [] and env["token_identity_status"] == "unknown"
    assert env["pool_identity_scope"] == "historical_onchain_pool_token_identity_unknown"
    assert len(out["rows"]) == 3 and len(env["sources"]) == 2
    assert env["sources"][1]["sha256"] == digest(catalog.doc)
    assert env["rows_sha256"] == digest(out["rows"])
    assert env["coverage"]["completeness"] == "not_asserted"
    assert env["coverage"]["requested_window_complete"] is False


def test_cross_chain_symbols_are_separate_and_no_token_addresses_are_builtin():
    catalog = CatalogFake()
    catalog.doc["tokens"].append({**catalog.doc["tokens"][0], "chainId": 10})
    catalog.doc["tokens"][0]["symbol"] = "OTHER0"
    catalog.doc["tokens"][1]["symbol"] = "OTHER1"
    env = run(catalog_http=catalog)["_envelope"]
    assert [t["symbol"] for t in env["token_identities"]] == ["OTHER0", "OTHER1"]


@pytest.mark.parametrize("mode", ["http", "exception", "oversize", "nan", "version", "name", "timestamp", "tokens"])
def test_catalog_failures_are_bounded_redacted_and_never_return_success(mode):
    secret = "PRIVATE_CATALOG_BODY"
    def fail():
        raise RuntimeError(secret)
    catalog = CatalogFake(effect=fail if mode == "exception" else None)
    if mode == "http": catalog.status, catalog.doc = 403, {"raw": secret}
    if mode == "oversize": catalog.doc["extra"] = "x" * (history.MAX_CATALOG_BYTES + 1)
    if mode == "nan": catalog.doc["extra"] = float("nan")
    if mode == "version": catalog.doc["version"]["major"] = True
    if mode == "name": catalog.doc["name"] = ""
    if mode == "timestamp": catalog.doc["timestamp"] = "2026-02-30T00:00:00Z"
    if mode == "tokens": catalog.doc["tokens"] = []
    with pytest.raises(DataApiError) as caught:
        run(catalog_http=catalog)
    assert secret not in str(caught.value) + str(caught.value.detail)
    assert caught.value.retryable is False and caught.value.detail["coverage_complete"] is False
    assert len(catalog.calls) == 1


def test_catalog_not_fetched_when_historical_rpc_fails():
    catalog = CatalogFake()
    with pytest.raises(DataApiError) as caught:
        run(Fake(lambda *args: (403, {"private": "body"})), catalog_http=catalog)
    assert caught.value.kind == "configuration_required"
    assert catalog.calls == []


@pytest.mark.parametrize("expire_during_catalog", [False, True])
def test_catalog_uses_remaining_shared_deadline_without_reset(monkeypatch, expire_during_catalog):
    clock = [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    def near_deadline(req, doc, fake):
        if len(fake.calls) == 2 + 17 * 3:
            clock[0] = history.DEADLINE_SECONDS - 0.25
        return 200, doc
    def expire():
        clock[0] = history.DEADLINE_SECONDS + 1
    catalog = CatalogFake(effect=expire if expire_during_catalog else None)
    if expire_during_catalog:
        with pytest.raises(DataApiError, match="deadline"):
            run(Fake(near_deadline), catalog_http=catalog)
    else:
        assert run(Fake(near_deadline), catalog_http=catalog)["rows"]
    assert catalog.calls[0][2]["timeout"] == 0.25


def test_catalog_default_transport_is_separate_strict_bounded_and_credential_free():
    provider = history.UniswapHistoryProvider()
    catalog = provider.catalog_http
    assert isinstance(catalog, UrllibHttp) and catalog is not provider.http
    assert catalog.allowed_origins == ("https://tokens.uniswap.org",)
    assert catalog.max_response_bytes == 8 * 1024 * 1024 and catalog.strict_json
    assert catalog.rate_limit_per_sec == 0
    for url in ("http://tokens.uniswap.org", "https://user@tokens.uniswap.org", "https://evil.invalid"):
        with pytest.raises(ValueError): catalog._check_origin(url)
    for raw in (b'{"tokens":[],"tokens":[]}', b'{"x":NaN}', b'{"x":"\xff"}'):
        with pytest.raises((ValueError, UnicodeError)): catalog._read_json(BytesIO(raw))
    with pytest.raises(ValueError):
        catalog._read_json(BytesIO(b"x" * (history.MAX_CATALOG_BYTES + 1)))


@pytest.mark.parametrize("damage", [None, "native_alias", "unknown_identity", "blob", "pool"])
def test_real_producer_prepare_inputs_validate_prepared_contract(monkeypatch, tmp_path, damage):
    from nerya.skills.builtin.backtest.scripts import replay_inputs
    from nerya.strategies import request_scope as scope
    catalog = CatalogFake()
    if damage == "unknown_identity": catalog.doc["tokens"][1]["decimals"] = 6
    registry = DataApiRegistry()
    registry.register_action(history.uniswap_specs(Fake(), catalog_http=catalog)[0])
    args = {k: v for k, v in ARGS.items() if k not in ("start_block", "end_block")}
    start, end = 1_600_001_205, 1_600_001_327
    args.update(start=iso(start), end=iso(end))
    declaration = {"version": 1, "datasets": [{"id": "pool", "provider": "uniswap",
                   "action": "pool_history", "args": args}]}
    monkeypatch.setattr(replay_inputs, "read_manifest", lambda root: deepcopy(declaration))
    prepared = replay_inputs.prepare_inputs(tmp_path, registry=registry)
    market = "UNISWAP_V3:ETH/USDC" if damage == "native_alias" else "UNISWAP_V3:WETH/USDC"
    manifest = {"markets": [market]}
    contract = {"constraints": {"resources": [scope._market(market)], "start_utc": start, "end_utc": end}}
    receipt = prepared["receipts"][0]
    payload = json.loads(prepared["files"]["pool.json"])
    assert len(receipt["sources"]) == len(payload["_envelope"]["sources"]) == 2 <= 8
    assert receipt["sources"] == payload["_envelope"]["sources"]
    assert receipt["requested_window_complete"] is False
    if damage == "blob": prepared["files"]["pool.json"] += b" "
    if damage == "pool":
        declaration["datasets"][0]["args"]["pool_address"] = "0x" + "44" * 20
        prepared["manifest"] = deepcopy(declaration)
    if damage:
        with pytest.raises(scope.ScopeError):
            scope.validate_prepared(contract, manifest, declaration, prepared)
    else:
        assert scope.validate_prepared(contract, manifest, declaration, prepared) == {
            receipt["snapshot_file"]: receipt["sha256"]}
    assert len(catalog.calls) == 1
