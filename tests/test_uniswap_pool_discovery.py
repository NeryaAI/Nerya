"""Offline acceptance tests for current Uniswap v3 pool discovery."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from nerya.data_api import uniswap_history as history
from nerya.data_api.registry import compact_data_result
from nerya.data_api.types import DataApiContext, DataApiError
from nerya.data_api.uniswap_discovery import (
    STANDARD_FEE_TIERS, UniswapPoolDiscoveryProvider, uniswap_discovery_specs,
)
from nerya.harness.cancellation import CancelledError

pytestmark = pytest.mark.smoke
TOKEN0, TOKEN1 = "0x" + "11" * 20, "0x" + "22" * 20
POOL100, POOL3000 = "0x" + "aa" * 20, "0x" + "bb" * 20
POOL500, POOL10000 = "0x" + "cc" * 20, "0x" + "dd" * 20
ZERO = "0x" + "00" * 20


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class CatalogFake:
    def __init__(self, doc=None):
        self.calls = []
        self.doc = doc or {
            "name": "Uniswap Labs Default", "version": {"major": 22, "minor": 32, "patch": 0},
            "timestamp": "2026-10-28T00:00:00.000Z",
            "tokens": [
                {"chainId": 1, "address": TOKEN0, "symbol": "USDC", "decimals": 6},
                {"chainId": 1, "address": TOKEN1, "symbol": "WETH", "decimals": 18},
            ],
        }

    def request(self, method, url, **kwargs):
        assert method == "GET" and url == history.CATALOG_URL
        assert kwargs["headers"] == {"Accept": "application/json"}
        self.calls.append((method, url, kwargs))
        return 200, deepcopy(self.doc)


class RpcFake:
    def __init__(self, *, pools=None, mutate=None, rpc_url=None):
        self.pools = {100: POOL100, 500: ZERO, 3000: POOL3000, 10000: ZERO}
        self.pools.update(pools or {})
        self.mutate, self.calls, self.responses = mutate, [], []
        self.rpc_url = rpc_url or history.RPC_ORIGINS["publicnode"]

    def request(self, method, url, **kwargs):
        assert method == "POST" and url == self.rpc_url
        request = deepcopy(kwargs["body"])
        self.calls.append(request)
        name, params = request["method"], request["params"]
        if name == "eth_chainId":
            result = "0x1"
        elif name == "eth_blockNumber":
            result = "0x64"
        elif name == "eth_getBlockByNumber":
            block = int(params[0], 16)
            assert params == [hex(block), False]
            result = {"number": hex(block), "hash": "0x" + format(block + 1, "064x"),
                      "timestamp": hex(1_700_000_000)}
        else:
            assert name == "eth_call"
            tx, tag = params
            assert tag == "0x64"
            target, data = tx["to"], tx["data"]
            pool_fee = next((fee for fee, pool in self.pools.items() if pool == target), None)
            signatures = {
                "getPool(address,address,uint24)": ("address", ["address", "address", "uint24"]),
                "feeAmountTickSpacing(uint24)": ("spacing", ["uint24"]),
                "factory()": (history.FACTORY, []), "token0()": (TOKEN0, []), "token1()": (TOKEN1, []),
                "fee()": (pool_fee, []),
                "tickSpacing()": ({100: 1, 500: 10, 3000: 60, 10000: 200}.get(pool_fee), []),
                "decimals()": (6 if target == TOKEN0 else 18, []),
            }
            signature = next(item for item in signatures if data[2:10] == keccak(text=item)[:4].hex())
            value, input_types = signatures[signature]
            if signature == "getPool(address,address,uint24)":
                value = self.pools[decode(input_types, bytes.fromhex(data[10:]))[2]]
                types, values = ["address"], [value]
            elif signature == "feeAmountTickSpacing(uint24)":
                value = {100: 1, 500: 10, 3000: 60, 10000: 200}[decode(input_types, bytes.fromhex(data[10:]))[0]]
                types, values = ["int24"], [value]
            else:
                types, values = (["address"], [value]) if signature in {"factory()", "token0()", "token1()"} else (["uint24" if signature == "fee()" else "int24" if signature == "tickSpacing()" else "uint8"], [value])
            result = "0x" + encode(types, values).hex()
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        status = 200
        if self.mutate:
            status, response = self.mutate(request, response, self)
        self.responses.append(deepcopy(response))
        return status, response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)


def run(rpc=None, catalog=None, **changes):
    args = {"base": "USDC", "quote": "WETH", **changes}
    return UniswapPoolDiscoveryProvider(rpc or RpcFake(), catalog_http=catalog or CatalogFake()).pool_discover(
        DataApiContext(), args)


def test_schema_registry_and_non_replay_contract():
    spec = uniswap_discovery_specs()[0]
    assert (spec.provider, spec.action, spec.replay_input) == ("uniswap", "pool_discover", False)
    assert spec.input_schema["required"] == ["base", "quote"]
    assert set(spec.input_schema["properties"]) == {"base", "quote", "rpc_provider"}
    assert STANDARD_FEE_TIERS == (100, 500, 3000, 10000)


def test_returns_all_current_candidates_and_rechecks_pinned_block():
    rpc, catalog = RpcFake(), CatalogFake()
    out = run(rpc, catalog)
    rows, env = out["rows"], out["_envelope"]
    assert [row["fee"] for row in rows] == [100, 3000]
    assert {row["pool_address"] for row in rows} == {POOL100, POOL3000}
    assert all(row["block_number"] == 100 and row["block_hash"].endswith("65") for row in rows)
    assert all(row["decimals"] == [6, 18] for row in rows)
    assert env["chain_id"] == 1 and env["chain"] == "Ethereum"
    assert env["pool_identity_scope"] == "current_only"
    assert env["historical_data_verified"] is False and env["financial_execution_allowed"] is False
    assert env["fee_tier_policy"] == "standard_documented_non_exhaustive"
    assert env["budget"]["request_count"] == len(rpc.calls) <= env["budget"]["request_budget"]
    assert len(env["sources"]) == 2 and catalog.calls
    assert rpc.calls[0]["method"] == "eth_chainId" and rpc.calls[1]["method"] == "eth_blockNumber"
    assert rpc.calls[-1]["method"] == "eth_getBlockByNumber"
    assert all("latest" not in str(call) for call in rpc.calls)


def test_zero_pool_is_safe_and_address_inputs_are_catalog_bound():
    out = run(RpcFake(pools={100: ZERO, 3000: ZERO}))
    assert out["rows"] == [] and out["_envelope"]["candidate_count"] == 0
    assert out["_envelope"]["historical_data_verified"] is False
    assert run(base=TOKEN0, quote=TOKEN1)["_envelope"]["request_args"]["base"] == TOKEN0
    provider = UniswapPoolDiscoveryProvider(RpcFake(), catalog_http=CatalogFake())
    with pytest.raises(DataApiError) as caught:
        provider.pool_discover(DataApiContext(), {"base": "ETH", "quote": "USDC"})
    assert caught.value.kind == "schema_validation"
    assert caught.value.detail["reason"] == "unknown_catalog_token"
    assert "pool" not in str(caught.value).lower() and "history" not in str(caught.value).lower()


def test_duplicate_symbol_and_catalog_decimal_identity_fail_closed():
    catalog = CatalogFake()
    catalog.doc["tokens"].append({"chainId": 1, "address": "0x" + "33" * 20,
                                   "symbol": "USDC", "decimals": 6})
    with pytest.raises(DataApiError) as caught:
        run(catalog=catalog)
    assert caught.value.detail["reason"] == "ambiguous_catalog_symbol"
    catalog.doc["tokens"].append({"chainId": 1, "address": "0x" + "44" * 20,
                                   "symbol": "USDC", "decimals": 6})
    with pytest.raises(DataApiError) as caught:
        run(catalog=catalog)
    assert caught.value.detail["reason"] == "ambiguous_catalog_symbol"


@pytest.mark.parametrize("mode", ["status", "id", "chain", "header"])
def test_rpc_status_id_and_identity_headers_fail_closed(mode):
    def mutate(request, response, fake):
        if mode == "status" and request["method"] == "eth_chainId":
            return "200", response
        if mode == "id" and request["method"] == "eth_blockNumber":
            response["id"] = 999
        if mode == "chain" and request["method"] == "eth_chainId":
            response["result"] = "0x2"
        if mode == "header" and request["method"] == "eth_getBlockByNumber":
            response["result"]["hash"] = "0x00"
        return 200, response
    with pytest.raises(DataApiError) as caught:
        run(RpcFake(mutate=mutate))
    assert caught.value.retryable is False
    assert caught.value.detail["coverage_complete"] is False


def test_cancel_deadline_reorg_and_request_budget(monkeypatch):
    cancelled = SimpleNamespace(is_set=True)
    with pytest.raises(CancelledError):
        UniswapPoolDiscoveryProvider(RpcFake(), catalog_http=CatalogFake()).pool_discover(
            DataApiContext(cancel_token=cancelled), {"base": "USDC", "quote": "WETH"})

    clock = [0.0]
    monkeypatch.setattr("nerya.data_api.uniswap_discovery.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("nerya.data_api.uniswap_discovery.DEADLINE_SECONDS", 0.0)
    with pytest.raises(DataApiError) as caught:
        run()
    assert caught.value.detail["stage"] == "deadline"
    monkeypatch.setattr("nerya.data_api.uniswap_discovery.DEADLINE_SECONDS", 30.0)

    def reorg(request, response, fake):
        if request["method"] == "eth_getBlockByNumber" and sum(
                item["method"] == "eth_getBlockByNumber" for item in fake.calls) == 2:
            response["result"]["hash"] = "0x" + "ff" * 32
        return 200, response
    with pytest.raises(DataApiError) as caught:
        run(RpcFake(mutate=reorg))
    assert caught.value.detail["stage"] == "reorg"

    monkeypatch.setattr("nerya.data_api.uniswap_discovery.MAX_REQUESTS", 2)
    with pytest.raises(DataApiError) as caught:
        run()
    assert caught.value.detail["stage"] == "request_budget"


def test_catalog_deadline_reports_zero_rpc_count(monkeypatch):
    monkeypatch.setattr("nerya.data_api.uniswap_discovery.DEADLINE_SECONDS", 0.0)
    with pytest.raises(DataApiError) as caught:
        run()
    assert caught.value.detail == {
        "provider": "uniswap", "action": "pool_discover", "coverage_complete": False,
        "stage": "deadline", "request_count": 0,
    }


def test_cumulative_latency_budget_is_bounded_and_scales_to_four_candidates(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("nerya.data_api.uniswap_discovery.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("nerya.data_api.uniswap_discovery.DEADLINE_SECONDS", 30.0)

    class TimedCatalog(CatalogFake):
        def request(self, *args, **kwargs):
            clock[0] += 1.0
            return super().request(*args, **kwargs)

    all_pools = {100: POOL100, 500: POOL500, 3000: POOL3000, 10000: POOL10000}

    def one_second(request, response, fake):
        clock[0] += 1.0
        return 200, response

    with pytest.raises(DataApiError) as caught:
        run(RpcFake(pools=all_pools, mutate=one_second), TimedCatalog())
    assert caught.value.detail["stage"] == "deadline"
    assert 0 < caught.value.detail["request_count"] <= 64
    assert caught.value.detail["rpc_method"] in {
        "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_call",
    }

    monkeypatch.setattr("nerya.data_api.uniswap_discovery.DEADLINE_SECONDS", 120.0)
    clock[0] = 0.0
    rpc = RpcFake(pools=all_pools, mutate=one_second)
    out = run(rpc, TimedCatalog())
    assert len(out["rows"]) == 4
    assert len(rpc.calls) == 40
    assert out["_envelope"]["budget"]["request_count"] == 40


def test_malformed_rpc_is_redacted_and_compaction_keeps_evidence():
    secret = "PRIVATE_PROVIDER_BODY"
    def mutate(request, response, fake):
        if request["method"] == "eth_call":
            return 500, {"error": {"message": secret}}
        return 200, response
    with pytest.raises(DataApiError) as caught:
        run(RpcFake(mutate=mutate))
    assert secret not in str(caught.value) and caught.value.retryable is False
    compact = compact_data_result("uniswap", "pool_discover", run(), limit=1)
    assert compact["row_count"] == 2 and compact["truncated"] is True
    assert compact["_envelope"]["historical_data_verified"] is False
