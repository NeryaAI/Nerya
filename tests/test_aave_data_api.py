from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json

import pytest

from nerya.data_api.aave import (AaveYieldProvider, CATALOG_ORIGIN, HISTORY_QUERY,
                                 METADATA_QUERY, ORIGIN, RPC_ORIGINS, aave_specs)
from nerya.data_api.builtins import build_data_api_registry
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataApiContext, DataApiError

pytestmark = pytest.mark.smoke
PID = "11111111-2222-4333-8444-555555555555"
MARKET, TOKEN, ATOKEN = ("0x" + c * 40 for c in ("1", "2", "3"))


@pytest.mark.parametrize("case", ["exact", "gapped_truncated", "uuid", "atoken", "market", "ambiguous"])
def test_official_history_bound_to_catalog_uuid_offline(monkeypatch, case):
    def denied(*args, **kwargs):
        pytest.fail("Network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    # Synthetic identity/dates ensure the producer has no acceptance-pool/window constants.
    start = datetime(2025, 2, 3, tzinfo=timezone.utc)
    dates = [(start + timedelta(days=d)).isoformat().replace("+00:00", "Z") for d in range(-1, 4)]
    config = {"config_id": PID, "pool": ATOKEN + "-not-a-market-mapping", "token": ATOKEN,
              "project": "aave-v3", "chain": "Ethereum", "symbol": "USDX", "underlyingTokens": [TOKEN]}
    official = {"name": "Explicit market", "address": MARKET, "chain": {"name": "Ethereum", "chainId": 1},
                "reserves": [{"underlyingToken": {"address": TOKEN, "symbol": "USDX"},
                              "aToken": {"address": ATOKEN, "symbol": "aUSDX"}}]}
    markets = [official]
    if case == "uuid":
        config["config_id"] = "99999999-2222-4333-8444-555555555555"
    if case == "atoken":
        official["reserves"][0]["aToken"]["address"] = TOKEN
    if case == "market":
        official["address"] = TOKEN
    if case == "ambiguous":
        markets.append(deepcopy(official))
    raw = [{"date": d, "avgRate": {"raw": "999999999999999999999999999", "decimals": 27,
                                      "value": "0.0325", "formatted": "3.25%"}} for d in dates]
    if case == "gapped_truncated":
        raw = [r for r in raw if r["date"] != dates[2]]
    calls = []

    class Fake:
        def request(self, method, url, **kwargs):
            assert kwargs["timeout"] <= 15
            calls.append((method, url, kwargs.get("body")))
            if url == CATALOG_ORIGIN + "/configPool/" + PID:
                assert method == "GET"
                return 200, {"status": "success", "data": [config]}
            assert method == "POST" and url == ORIGIN + "/graphql"
            body = kwargs["body"]
            if body["query"] == METADATA_QUERY:
                assert body["variables"] == {"request": {"chainIds": [1]}}
                return 200, {"data": {"markets": markets}}
            assert body["query"] == HISTORY_QUERY
            assert body["variables"] == {"request": {"market": MARKET, "underlyingToken": TOKEN,
                                                        "chainId": 1, "window": "LAST_YEAR"}}
            return 200, {"data": {"supplyAPYHistory": list(reversed(raw))}}

    args = {"pool_id": PID, "chain_id": 1, "market": MARKET, "underlying_token": TOKEN,
            "start": dates[1], "end": dates[-1], "window": "LAST_YEAR",
            "limit": 1 if case == "gapped_truncated" else 500}
    registry = DataApiRegistry()
    for spec in aave_specs(Fake()):
        registry.register_action(spec)
    schema = build_data_api_registry().schema("aave", "supply_apy_history")
    assert not schema.get("replay_input", False)
    assert "availability time are unverified" in schema["description"]
    with pytest.raises(DataApiError) as blocked:
        registry.call_replay_input("aave", "supply_apy_history", args)
    assert blocked.value.kind == "permission_denied" and not calls
    provider = AaveYieldProvider(Fake())
    if case not in {"exact", "gapped_truncated"}:
        with pytest.raises(DataApiError):
            provider.supply_apy_history(DataApiContext(), args)
        assert len(calls) <= 2  # Never fetch history after an identity failure.
        return
    out = provider.supply_apy_history(DataApiContext(), args)
    env = out["_envelope"]
    assert len(calls) == 3
    assert all(r["pool_id"] == PID and r["deposit_apy_percent"] == 3.25 for r in out["rows"])
    assert env["pool"]["pool_id"] == PID and env["pool_identity_scope"] == "current_catalog_metadata"
    assert env["pool"]["canonical_identity"] == {"market": "AAVE_V3:ethereum-USDX", "protocol": "aave_v3",
        "component_id": "builtin:aave", "chain": "ethereum", "asset": "USDX", "pool_id": PID}
    assert env["mock"] is False and env["observation_kind"] == "historical_rate_observations"
    assert env["availability_scope"] == "unverified" and env["point_in_time_ready"] is False
    assert env["replay_input"] is False and env["averaging_scope"] == "upstream_average_interval_unknown"
    assert all(r["available_at"] is None and r["observation_date"] == r["timestamp"] for r in out["rows"])
    assert env["boundary_observations"]["before"]["timestamp"] == dates[0]
    assert env["boundary_observations"]["before"]["pool_id"] == PID
    assert env["boundary_observations"]["after"] is None
    assert env["requested_window"] == {"start": dates[1], "end": dates[-1], "inclusive": True}
    assert env["source_observed_range"] == {"start": dates[0], "end": dates[-1]}
    assert len(env["sources"]) == 3 and all(len(s["sha256"]) == 64 for s in env["sources"])
    assert len({s["sha256"] for s in env["sources"]}) == 3
    coverage = env["coverage"]
    assert coverage["completeness"] == "not_asserted"
    assert coverage["leading_gap_seconds"] == coverage["trailing_gap_seconds"] == 0
    if case == "exact":
        assert len(out["rows"]) == 4 and not env["provider_truncated"]
        assert coverage["max_gap_seconds"] == 86400 and not coverage["missing_utc_days"]
        assert not coverage["gaps_over_24h"]
    else:
        assert len(out["rows"]) == 1 and env["provider_truncated"]
        assert coverage["observations"] == 3 and coverage["max_gap_seconds"] == 172800
        assert coverage["missing_utc_days"] == [dates[2][:10]]
        assert coverage["gaps_over_24h"] == [{"start": dates[1], "end": dates[3], "seconds": 172800}]
    for change in ({"start": "2020-01-01"}, {"window": "GUESS"}, {"url": "https://invalid.example"}):
        with pytest.raises(DataApiError):
            provider.supply_apy_history(DataApiContext(), {**args, **change})
    assert len(calls) == 3


@pytest.mark.parametrize("case", ["publicnode", "drpc", "tenderly", "utc", "catalog_identity", "chain",
                                  "rpc_error", "http_error", "access_denied", "transport", "header", "reorg",
                                  "abi_length", "abi_padding", "historical_atoken", "last_update",
                                  "normalized_abi_length"])
def test_reserve_state_history_offline(monkeypatch, case):
    from eth_abi import encode

    def denied(*args, **kwargs):
        pytest.fail("Network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    config = {"config_id": PID, "pool": ATOKEN + "-opaque", "token": ATOKEN,
              "project": "aave-v3", "chain": "Ethereum", "symbol": "USDX", "underlyingTokens": [TOKEN]}
    official = {"name": "Explicit market", "address": MARKET, "chain": {"chainId": 1},
                "reserves": [{"underlyingToken": {"address": TOKEN, "symbol": "USDX"},
                              "aToken": {"address": ATOKEN}}]}
    provider = case if case in RPC_ORIGINS else "tenderly"
    blocks = [901] if case == "utc" else [901, 907]  # Synthetic identity/blocks, no production constants.
    spacing = 12 if case == "utc" else 1
    rates = {901: 89971710913012676227270026, 907: 2**128 - 1}
    index = 1191757687114757388449170114
    normalized = {901: index + 1234567890123456789, 907: 2**256 - 1}
    calls, headers = [], {}

    class Fake:
        def request(self, method, url, **kwargs):
            assert kwargs["timeout"] <= 15
            body = kwargs.get("body")
            calls.append((method, url, deepcopy(body)))
            if url == CATALOG_ORIGIN + "/configPool/" + PID:
                assert method == "GET"
                return 200, {"status": "success", "data": [{**config, "config_id": "wrong"}
                                                           if case == "catalog_identity" else config]}
            if url == ORIGIN + "/graphql":
                assert method == "POST" and body["query"] == METADATA_QUERY
                assert body["variables"] == {"request": {"chainIds": [1]}}
                return 200, {"data": {"markets": [official]}}
            assert url == RPC_ORIGINS[provider] and method == "POST"
            result = {"jsonrpc": "2.0", "id": body["id"]}
            if body["method"] == "eth_chainId":
                assert body["params"] == []
                return 200, {**result, "result": "0x2" if case == "chain" else "0x1"}
            if body["method"] == "eth_blockNumber":
                assert case == "utc" and body["params"] == []
                return 200, {**result, "result": hex(1000)}
            number = int(body["params"][-1] if body["method"] == "eth_call" else body["params"][0], 16)
            assert 0 <= number <= 1000 if case == "utc" else number in blocks
            if body["method"] == "eth_getBlockByNumber":
                assert body["params"] == [hex(number), False]
                headers[number] = headers.get(number, 0) + 1
                block_hash = "0x" + f"{number:064x}"
                if case == "reorg" and headers[number] > 1:
                    block_hash = "0x" + "f" * 64
                return 200, {**result, "result": {"number": hex(number + (case == "header")),
                    "hash": block_hash, "timestamp": hex(1710000000 + number * spacing)}}
            assert body["method"] == "eth_call"
            assert number in blocks
            # Verify selector and argument independently of the production calldata helper.
            if body["params"][0]["data"].startswith("0xd15e0053"):
                assert body["params"] == [{"to": MARKET, "data": "0xd15e0053" + "0" * 24 + TOKEN[2:]}, hex(number)]
                raw = f"{normalized[number]:064x}"
                if case == "normalized_abi_length":
                    raw += "00"  # Reject trailing bytes rather than silently accepting a prefix.
                return 200, {**result, "result": "0x" + raw}
            assert body["params"] == [{"to": MARKET, "data": "0x35ea6a75" + "0" * 24 + TOKEN[2:]}, hex(number)]
            if case == "transport":
                raise OSError("PRIVATE_BODY_DO_NOT_RETURN")
            if case in {"rpc_error", "http_error", "access_denied"}:
                return (403 if case == "access_denied" else 429 if case == "http_error" else 200), {**result, "error": {
                    "code": -32000, "message": "PRIVATE_BODY_DO_NOT_RETURN", "data": {"huge": "x" * 1000}}}
            values = [7, index, rates[number], 11, 13, 17,
                      1710000000 + number + 1 if case == "last_update" else 1710000000,
                      4, TOKEN if case == "historical_atoken" else ATOKEN, TOKEN, TOKEN, MARKET, 23, 29, 31]
            raw = encode(["uint256", "uint128", "uint128", "uint128", "uint128", "uint128",
                          "uint40", "uint16", "address", "address", "address", "address",
                          "uint128", "uint128", "uint128"], values).hex()
            if case == "abi_length":
                raw = raw[:-64]
            if case == "abi_padding":
                raw = raw[:64] + "f" * 64 + raw[128:]  # uint128 padding must be zero.
            return 200, {**result, "result": "0x" + raw}

    args = {"pool_id": PID, "chain_id": 1, "market": MARKET, "underlying_token": TOKEN,
            "rpc_provider": provider, "block_numbers": blocks}
    registry = DataApiRegistry()
    for spec in aave_specs(Fake()):
        registry.register_action(spec)
    schema = build_data_api_registry().schema("aave", "reserve_state_history")
    assert not schema.get("replay_input", False)
    assert "rpc_provider" in schema["input_schema"]["required"]
    with pytest.raises(DataApiError) as blocked:
        registry.call_replay_input("aave", "reserve_state_history", args)
    assert blocked.value.kind == "permission_denied" and not calls
    for change in ({"block_numbers": []}, {"block_numbers": [True]}, {"block_numbers": [1, 1]},
                   {"block_numbers": list(range(17))}, {"block_numbers": [-1]},
                   {"block_numbers": [2**64]}, {"block_numbers": ["901"]}, {"chain_id": 10},
                   {"rpc_provider": "https://untrusted.invalid"}, {"rpc_url": "https://untrusted.invalid"}):
        with pytest.raises(DataApiError) as invalid:
            registry.call("aave", "reserve_state_history", {**args, **change}, context=DataApiContext())
        assert invalid.value.kind == "schema_validation" and not calls
    if case not in {*RPC_ORIGINS, "utc"}:
        with pytest.raises(DataApiError) as failed:
            registry.call("aave", "reserve_state_history", args, context=DataApiContext())
        assert "PRIVATE_BODY_DO_NOT_RETURN" not in json.dumps(failed.value.__dict__)
        assert len(calls) <= 7  # Stop at first failure, no retries/fallback or partial table.
        if case == "access_denied":
            assert failed.value.kind == "configuration_required" and failed.value.retryable is False
            assert failed.value.detail["reason"] == "historical_rpc_authorization_required"
            assert failed.value.detail["http_status"] == 403
            assert failed.value.detail["rpc_error_code"] == -32000
            assert "another supported public read-only source" in failed.value.message
        if case in {"rpc_error", "http_error", "transport"}:
            detail = failed.value.detail
            assert len(detail["request_sha256"]) == 64 and detail["rpc_method"] == "eth_call"
            if case != "transport":
                assert detail["rpc_error_code"] == -32000 and len(detail["response_sha256"]) == 64
        return
    if case == "utc":
        args.pop("block_numbers")
        args["at_utc"] = datetime.fromtimestamp(1710000000 + 901 * spacing + 5, timezone.utc).isoformat().replace("+00:00", "Z")
    out = registry.call("aave", "reserve_state_history", args, context=DataApiContext())
    env = out["_envelope"]
    assert len(calls) <= 41 if case == "utc" else len(calls) == 11
    assert len(out["rows"]) == len(blocks)
    for row, number in zip(out["rows"], blocks):
        assert row["block_number"] == number and row["block_hash"] == "0x" + f"{number:064x}"
        assert row["timestamp"] == 1710000000 + number * spacing
        assert headers[number] == (3 if case == "utc" else 2)
        assert row["liquidity_index"] == str(index) and row["current_liquidity_rate_ray"] == str(rates[number])
        assert row["normalized_income_ray"] == str(normalized[number])
        assert row["last_update_timestamp"] == "1710000000" and row["a_token"] == ATOKEN
        with localcontext() as precision:
            precision.prec = 80
            assert Decimal(row["base_supply_rate_percent"]) == Decimal(rates[number]) / 10**25
        assert "deposit_apy_percent" not in row
        refs = [env["sources"][i] for i in row["source_refs"]]
        assert [s["request"]["method"] for s in refs] == ["eth_getBlockByNumber", "eth_call", "eth_call", "eth_getBlockByNumber"]
        income_source = refs[2]
        assert income_source["url"] == RPC_ORIGINS[provider]
        assert income_source["request"]["params"] == [
            {"to": MARKET, "data": "0xd15e0053" + "0" * 24 + TOKEN[2:]}, hex(number)]
        response = {"jsonrpc": "2.0", "id": income_source["request"]["id"],
                    "result": "0x" + f"{normalized[number]:064x}"}
        for value, key in ((income_source["request"], "request_sha256"), (response, "sha256")):
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            assert income_source[key] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert out["rows"][0]["base_supply_rate_percent"] == "8.9971710913012676227270026"
    assert env["replay_input"] is env["point_in_time_ready"] is False
    assert env["coverage"]["completeness"] == "not_asserted" and not env["provider_truncated"]
    assert env["requested_block_numbers"] == args.get("block_numbers") and env["official_identity"]["pool_id"] == PID
    if case == "utc":
        proof = env["resolver_proof"]
        assert env["requested_at_utc"] == args["at_utc"] and env["resolved_block_numbers"] == [901]
        assert proof["floor"]["timestamp"] == out["rows"][0]["timestamp"] < proof["requested_timestamp"]
        assert proof["requested_timestamp"] < proof["next"]["timestamp"]
        assert proof["next"]["block_number"] == proof["floor"]["block_number"] + 1
        assert proof["head"]["block_number"] == 1000 and headers[902] == 2
        assert 0 < proof["resolver_header_reads"] <= 32
        refs = [env["sources"][i] for i in proof["source_refs"]]
        assert refs[0]["request"]["method"] == "eth_blockNumber"
        assert refs[-1]["request"]["params"] == [hex(902), False]
    assert "APR" in env["rate_semantics"] and "excludes rewards" in env["rate_semantics"]
    assert "not_finality_proof" in env["canonicality"]
    assert all(len(s["sha256"]) == len(s["request_sha256"]) == 64 for s in env["sources"])
    assert all("response" not in s for s in env["sources"])


@pytest.mark.parametrize("case", ["daily", "terminal", "same_block", "single", "max_points",
                                  "max_window", "identity", "missing", "reorg", "identity_drift"])
def test_reserve_index_history_full_grid_offline(monkeypatch, case):
    from eth_abi import encode

    def denied(*args, **kwargs):
        pytest.fail("Network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    base = 1710000000
    start = datetime.fromtimestamp(base + 901 * 12 + 5, timezone.utc)
    durations = {"daily": 30 * 86400, "terminal": 2 * 86400 + 7, "same_block": 1,
                 "single": 0, "max_points": 499, "max_window": 366 * 86400}
    end = start + timedelta(seconds=durations.get(case, 2 * 86400 + 7))
    iso = lambda stamp: stamp.isoformat().replace("+00:00", "Z")
    args = {"pool_id": PID, "chain_id": 1, "market": MARKET, "underlying_token": TOKEN,
            "rpc_provider": "drpc", "start": iso(start), "end": iso(end)}
    if case == "max_points":
        args["interval_seconds"] = 1
    config = {"config_id": PID, "pool": ATOKEN + "-opaque", "token": ATOKEN,
              "project": "aave-v3", "chain": "Ethereum", "symbol": "USDX", "underlyingTokens": [TOKEN]}
    official = {"address": MARKET, "chain": {"chainId": 1},
                "reserves": [{"underlyingToken": {"address": TOKEN, "symbol": "USDX"},
                              "aToken": {"address": ATOKEN}}]}
    index, rate, income = 2**128 - 1, 89971710913012676227270026, 2**256 - 1
    calls, records, headers = [], {}, {}
    catalogs = 0

    class Fake:
        def request(self, method, url, **kwargs):
            nonlocal catalogs
            body = kwargs.get("body")
            calls.append((method, url, deepcopy(body)))
            assert kwargs["timeout"] <= 15
            if url == CATALOG_ORIGIN + "/configPool/" + PID:
                assert method == "GET"
                catalogs += 1
                return 200, {"status": "success", "data": [config]}
            if url == ORIGIN + "/graphql":
                assert method == "POST" and body["query"] == METADATA_QUERY
                assert body["variables"] == {"request": {"chainIds": [1]}}
                metadata = deepcopy(official)
                if case == "identity":
                    metadata["reserves"][0]["underlyingToken"]["symbol"] = "HUMAN_LABEL"
                return 200, {"data": {"markets": [metadata]}}
            assert url == RPC_ORIGINS["drpc"] and method == "POST"
            rpc_method, params = body["method"], body["params"]
            if rpc_method == "eth_chainId":
                value = "0x1"
            elif rpc_method == "eth_blockNumber":
                value = hex(3_000_000)
            elif rpc_method == "eth_getBlockByNumber":
                number = int(params[0], 16)
                assert params == [hex(number), False]
                headers[number] = headers.get(number, 0) + 1
                value = {"number": hex(number), "hash": "0x" + f"{number:064x}",
                         "timestamp": hex(base + number * 12)}
                if case == "reorg" and number == 901 and headers[number] > 1:
                    value["hash"] = "0x" + "f" * 64
            else:
                assert rpc_method == "eth_call"
                number = int(params[-1], 16)
                assert params[0]["to"] == MARKET and params[0]["data"][10:] == "0" * 24 + TOKEN[2:]
                if case == "missing" and catalogs == 2:
                    return 200, {"jsonrpc": "2.0", "id": body["id"], "result": None}
                if params[0]["data"].startswith("0xd15e0053"):
                    value = "0x" + f"{income:064x}"
                else:
                    assert params[0]["data"].startswith("0x35ea6a75")
                    value = "0x" + encode(
                        ["uint256", "uint128", "uint128", "uint128", "uint128", "uint128",
                         "uint40", "uint16", "address", "address", "address", "address",
                         "uint128", "uint128", "uint128"],
                        [7, index, rate, 11, 13, 17, base, 4, ATOKEN, TOKEN, TOKEN, MARKET, 23, 29, 31]).hex()
            response = {"jsonrpc": "2.0", "id": body["id"], "result": value}
            key = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            records[key] = deepcopy(response)
            return 200, response

    registry = DataApiRegistry()
    for spec in aave_specs(Fake()):
        registry.register_action(spec)
    schema = build_data_api_registry().schema("aave", "reserve_index_history")
    assert schema["replay_input"] is True
    assert schema["input_schema"]["properties"]["interval_seconds"]["default"] == 86400
    assert schema["input_schema"]["additionalProperties"] is False
    for action in ("reserve_state_history", "supply_apy_history"):
        assert not registry.schema("aave", action).get("replay_input", False)
        with pytest.raises(DataApiError) as blocked:
            registry.call_replay_input("aave", action, args)
        assert blocked.value.kind == "permission_denied" and not calls
    invalid_changes = [
        {"interval_seconds": value} for value in (0, -1, True, 1.5, "86400")
    ] + [{"end": iso(start + timedelta(days=367))}, {"end": iso(start - timedelta(seconds=1))},
         {"end": "2099-01-01"}, {"start": iso(start + timedelta(microseconds=1))},
         {"start": "2025-01-01T00:00:00+01:00"}, {"symbol": "USDC"},
         {"chain_id": 10}, {"rpc_provider": "https://untrusted.invalid"},
         {"rpc_url": "https://untrusted.invalid"}, {"limit": 1},
         {"end": iso(start + timedelta(seconds=500)), "interval_seconds": 1},
         # The exact terminal point counts toward the cap even when it is off-grid.
         {"end": iso(start + timedelta(seconds=999)), "interval_seconds": 2}]
    for change in invalid_changes:
        with pytest.raises(DataApiError) as invalid:
            registry.call_replay_input("aave", "reserve_index_history", {**args, **change})
        assert invalid.value.kind == "schema_validation" and not calls
    for missing in ("start", "end", "rpc_provider"):
        with pytest.raises(DataApiError):
            registry.call_replay_input("aave", "reserve_index_history",
                                       {key: value for key, value in args.items() if key != missing})
        assert not calls
    if case == "identity_drift":
        # Each point still matches its own official metadata, but cross-point identity must be stable.
        original = AaveYieldProvider.reserve_state_history

        def drift(self, context, point_args):
            if catalogs == 1:
                config["symbol"] = official["reserves"][0]["underlyingToken"]["symbol"] = "OTHER"
            return original(self, context, point_args)
        monkeypatch.setattr(AaveYieldProvider, "reserve_state_history", drift)
    if case in {"identity", "missing", "reorg", "identity_drift"}:
        with pytest.raises(DataApiError) as failed:
            registry.call_replay_input("aave", "reserve_index_history", args)
        assert failed.value.detail["action"] == "reserve_index_history"
        assert failed.value.detail["coverage_complete"] is False
        expected_failed_at = start + timedelta(days=1) if case in {"missing", "identity_drift"} else start
        assert failed.value.detail["requested_at_utc"] == iso(expected_failed_at)
        assert catalogs == (2 if case in {"missing", "identity_drift"} else 1)
        return

    out = registry.call_replay_input("aave", "reserve_index_history", args)
    env = out["_envelope"]
    step = args.get("interval_seconds", 86400)
    duration = int((end - start).total_seconds())
    expected = [iso(start + timedelta(seconds=s)) for s in range(0, duration + 1, step)]
    if expected[-1] != iso(end):
        expected.append(iso(end))
    assert [row["timestamp"] for row in out["rows"]] == expected
    assert len(set(expected)) == len(expected) == catalogs
    assert env["requested_window"] == {"start": iso(start), "end": iso(end)}
    assert env["observation_interval_seconds"] == step
    assert env["observation_kind"] == "historical_reserve_index_observations"
    assert env["pool"] == {"pool_id": PID, "project": "aave-v3", "chain": "Ethereum", "symbol": "USDX"}
    assert env["official_identity"] == {"pool_id": PID, "chain_id": 1, "market": MARKET,
                                        "underlying_token": TOKEN, "a_token": ATOKEN}
    assert env["coverage"] == {"scope": "requested_observation_grid", "expected_count": len(expected),
                               "observations": len(expected), "complete": True, "missing_utc_days": [],
                               "null_counts": {"normalized_income_ray": 0, "base_supply_rate_percent": 0}}
    assert env["replay_scope"] == "historical_reserve_index_policy_simulation" and env["replay_input"] is True
    assert env["point_in_time_ready"] is env["performance_evidence"] is env["financial_execution_allowed"] is False
    assert env["availability_scope"] == "unverified" and not env["provider_truncated"]
    assert "instantaneous observation" in env["simulation_assumption"]
    assert "confirmation latency ignored" in env["simulation_assumption"]
    for row, point in zip(out["rows"], env["point_sources"], strict=True):
        decision = int(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp())
        number = (decision - base) // 12
        assert row["block_number"] == number and row["block_hash"] == "0x" + f"{number:064x}"
        assert row["block_timestamp"] == base + number * 12 <= decision
        assert row["source_timestamp"] == iso(datetime.fromtimestamp(base + number * 12, timezone.utc))
        assert row["pool_id"] == PID and row["a_token"] == ATOKEN
        assert row["liquidity_index"] == str(index) and row["normalized_income_ray"] == str(income)
        assert row["current_liquidity_rate_ray"] == str(rate) and row["last_update_timestamp"] == str(base)
        assert row["base_supply_rate_percent"] == "8.9971710913012676227270026"
        assert not any("apy" in key.lower() for key in row)
        refs = [env["sources"][i] for i in row["source_refs"]]
        assert [s["request"]["method"] for s in refs] == ["eth_getBlockByNumber", "eth_call", "eth_call", "eth_getBlockByNumber"]
        assert refs[2]["request"]["params"] == [
            {"to": MARKET, "data": "0xd15e0053" + "0" * 24 + TOKEN[2:]}, hex(number)]
        proof = point["resolver_proof"]
        assert point["requested_at_utc"] == row["timestamp"] and proof["requested_timestamp"] == decision
        assert proof["floor"]["timestamp"] == row["block_timestamp"] <= decision < proof["next"]["timestamp"]
        assert proof["floor"]["block_number"] == number and proof["next"]["block_number"] == number + 1
        assert env["sources"][point["chain_source_ref"]]["request"]["method"] == "eth_chainId"
        assert [env["sources"][i]["url"] for i in point["identity_source_refs"]] == [
            CATALOG_ORIGIN + "/configPool/" + PID, ORIGIN + "/graphql"]
        proof_refs = [env["sources"][i] for i in proof["source_refs"]]
        assert proof_refs[0]["request"]["method"] == "eth_blockNumber"
        assert proof_refs[-1]["request"]["params"] == [hex(number + 1), False]
    assert out["rows"][0]["timestamp"] != out["rows"][0]["source_timestamp"]
    if case == "same_block":
        assert out["rows"][0]["block_hash"] == out["rows"][1]["block_hash"]
    for source in env["sources"]:
        assert len(source["sha256"]) == len(source["request_sha256"]) == 64 and "response" not in source
        if source["url"] != RPC_ORIGINS["drpc"]:
            continue
        canonical = json.dumps(source["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert source["request_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
        response = json.dumps(records[canonical], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert source["sha256"] == hashlib.sha256(response.encode()).hexdigest()
    canonical_rows = json.dumps(out["rows"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert env["rows_sha256"] == env["returned_rows_sha256"] == hashlib.sha256(canonical_rows.encode()).hexdigest()
    assert len(env["sources"]) <= 41 * len(expected)  # Getter has at most 32 resolver header reads per point.
    json.dumps(out, allow_nan=False)
