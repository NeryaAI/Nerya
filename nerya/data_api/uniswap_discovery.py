"""Bounded, current-only Uniswap v3 pool identity discovery on Ethereum."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from eth_abi import decode

from ..connectors.http import HttpTransport, UrllibHttp
from ..financial.defi.calls import calldata
from ..harness.cancellation import CancelledError, raise_if_cancelled
from .types import DataActionSpec, DataApiContext, DataApiError
from .uniswap_history import (
    CATALOG_URL, FACTORY, MAX_CATALOG_BYTES, RPC_ORIGINS, TIMEOUT_SECONDS,
)

CHAIN_ID = 1
CHAIN_NAME = "Ethereum"
STANDARD_FEE_TIERS = (100, 500, 3000, 10000)
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUESTS = 64
DEADLINE_SECONDS = 120.0
MAX_CANDIDATES = len(STANDARD_FEE_TIERS)
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "base": {"type": "string", "minLength": 1, "maxLength": 42},
        "quote": {"type": "string", "minLength": 1, "maxLength": 42},
        "rpc_provider": {"type": "string", "enum": list(RPC_ORIGINS), "default": "publicnode"},
    },
    "required": ["base", "quote"],
    "additionalProperties": False,
}


def uniswap_discovery_specs(
    http: HttpTransport | None = None, *, catalog_http: HttpTransport | None = None,
) -> list[DataActionSpec]:
    return [DataActionSpec(
        provider="uniswap", action="pool_discover", title="Current Uniswap v3 pool identities",
        description=(
            "Ethereum chain 1 only. Resolve exact official Uniswap token catalog symbols or token "
            "addresses, then scan documented standard v3 fee tiers (100, 500, 3000, 10000); "
            "this fixed scan is explicitly non-exhaustive and returns every verified current candidate. "
            "No ETH=WETH alias, historical coverage, replay input, or execution permission."
        ),
        input_schema=INPUT_SCHEMA, handler=UniswapPoolDiscoveryProvider(
            http, catalog_http=catalog_http).pool_discover,
        tags=("uniswap", "defi", "ethereum", "pool", "discovery"), output_kind="table",
        docs_url="https://docs.uniswap.org/contracts/v3/reference/core/interfaces/IUniswapV3Factory",
        replay_input=False,
    )]


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise _error("Invalid provider JSON evidence", stage="response_envelope") from None


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _error(message: str, *, kind: str = "provider_error", **detail: Any) -> DataApiError:
    return DataApiError(message, kind=kind, retryable=False, detail={
        "provider": "uniswap", "action": "pool_discover", "coverage_complete": False, **detail,
    })


def _deadline_error(request_count: int, rpc_method: str | None = None) -> DataApiError:
    detail: dict[str, Any] = {"stage": "deadline", "request_count": request_count}
    if rpc_method is not None:
        detail["rpc_method"] = rpc_method
    return _error("Discovery deadline exhausted", **detail)


def _address(value: Any, *, field: str = "address") -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value) or int(value, 16) == 0:
        raise _error("Invalid token address", kind="schema_validation", input_field=field)
    return value.lower()


def _rpc_quantity(value: Any, *, label: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise _error("Malformed RPC quantity", stage="response_envelope", rpc_field=label)
    return int(value, 16)


class UniswapPoolDiscoveryProvider:
    def __init__(self, http: HttpTransport | None = None, *, catalog_http: HttpTransport | None = None) -> None:
        self.http = http if http is not None else UrllibHttp(
            max_response_bytes=MAX_RESPONSE_BYTES, allowed_origins=tuple(RPC_ORIGINS.values()),
            strict_json=True, rate_limit_per_sec=0,
        )
        self.catalog_http = catalog_http if catalog_http is not None else UrllibHttp(
            max_response_bytes=MAX_CATALOG_BYTES, allowed_origins=(CATALOG_URL,),
            strict_json=True, rate_limit_per_sec=0,
        )

    def _catalog(self, args: dict[str, Any], deadline: float, cancel_token: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        raise_if_cancelled(cancel_token)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _deadline_error(0)
        try:
            status, document = self.catalog_http.request(
                "GET", CATALOG_URL, headers={"Accept": "application/json"},
                timeout=min(TIMEOUT_SECONDS, remaining),
            )
        except CancelledError:
            raise
        except Exception:
            raise_if_cancelled(cancel_token)
            if time.monotonic() >= deadline:
                raise _deadline_error(0) from None
            raise _error("Token catalog transport failed", stage="transport") from None
        raise_if_cancelled(cancel_token)
        if time.monotonic() > deadline:
            raise _deadline_error(0)
        if status != 200 or not isinstance(document, dict):
            raise _error("Token catalog unavailable or invalid", stage="http_status")
        raw = _canonical(document)
        if len(raw) > MAX_CATALOG_BYTES:
            raise _error("Token catalog response exceeds byte limit", stage="response_size")
        tokens = document.get("tokens")
        if (not isinstance(tokens, list) or not tokens or len(tokens) > 100_000
                or not isinstance(document.get("name"), str)
                or not isinstance(document.get("version"), dict)
                or not isinstance(document.get("timestamp"), str)):
            raise _error("Token catalog metadata invalid", stage="response_envelope")
        by_address: dict[str, tuple[str, int]] = {}
        by_symbol: dict[str, str] = {}
        for token in tokens:
            if not isinstance(token, dict) or type(token.get("chainId")) is not int or token["chainId"] != CHAIN_ID:
                continue
            address = token.get("address")
            symbol = token.get("symbol")
            decimals = token.get("decimals")
            if (not isinstance(address, str) or not _ADDRESS.fullmatch(address)
                    or int(address, 16) == 0 or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                    or type(decimals) is not int or not 0 <= decimals <= 255):
                raise _error("Token catalog identity invalid", stage="response_envelope")
            address = address.lower()
            if address in by_address and by_address[address] != (symbol, decimals):
                raise _error("Token catalog address is ambiguous", reason="ambiguous_catalog_address")
            key = symbol
            if key in by_symbol and by_symbol[key] != address:
                by_symbol[key] = ""
            else:
                by_symbol[key] = address
            by_address[address] = (symbol, decimals)
        resolved: dict[str, Any] = {}
        for field in ("base", "quote"):
            value = args[field]
            if not isinstance(value, str):
                raise _error("Token input must be a symbol or address", kind="schema_validation", input_field=field)
            if _ADDRESS.fullmatch(value):
                address = _address(value, field=field)
                if address not in by_address:
                    raise _error("Token address is absent from official catalog", kind="schema_validation",
                                 reason="unknown_catalog_token", input_field=field, input_kind="address")
            else:
                address = by_symbol.get(value)
                if address == "":
                    raise _error("Token symbol is ambiguous in official catalog", kind="schema_validation",
                                 reason="ambiguous_catalog_symbol", input_field=field, input_kind="symbol")
                if not address:
                    raise _error("Token symbol is absent from official catalog", kind="schema_validation",
                                 reason="unknown_catalog_token", input_field=field, input_kind="symbol")
            resolved[field] = {"address": address, "symbol": by_address[address][0],
                               "decimals": by_address[address][1]}
        if resolved["base"]["address"] == resolved["quote"]["address"]:
            raise _error("Base and quote tokens must differ", kind="schema_validation", reason="same_token")
        source = {"url": CATALOG_URL, "sha256": _hash(document),
                  "hash_basis": "canonical_parsed_json_utf8_sorted_keys", "request_count": 1,
                  "identity_scope": "current_official_catalog"}
        return resolved, source

    def pool_discover(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        cancel_token = context.cancel_token if isinstance(context, DataApiContext) else None
        raise_if_cancelled(cancel_token)
        if not isinstance(args, dict) or set(args) - set(INPUT_SCHEMA["properties"]):
            raise _error("Invalid pool_discover arguments", kind="schema_validation", reason="invalid_envelope")
        if "base" not in args or "quote" not in args:
            raise _error("base and quote are required", kind="schema_validation", reason="missing_input")
        provider = args.get("rpc_provider", "publicnode")
        if not isinstance(provider, str) or provider not in RPC_ORIGINS:
            raise _error("rpc_provider must name a supported public RPC provider", kind="schema_validation", reason="invalid_rpc_provider")
        deadline = time.monotonic() + DEADLINE_SECONDS
        tokens, catalog_source = self._catalog(args, deadline, cancel_token)
        rpc_url = RPC_ORIGINS[provider]
        calls: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        count = 0

        def rpc(method: str, params: list[Any]) -> Any:
            nonlocal count
            raise_if_cancelled(cancel_token)
            if count >= MAX_REQUESTS:
                raise _error("Discovery request budget exhausted", stage="request_budget", request_count=count)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _deadline_error(count, method)
            request = {"jsonrpc": "2.0", "id": count + 1, "method": method, "params": params}
            count += 1
            try:
                status, response = self.http.request("POST", rpc_url, headers={"Accept": "application/json"},
                                                     body=request, timeout=min(TIMEOUT_SECONDS, remaining))
            except CancelledError:
                raise
            except Exception:
                raise_if_cancelled(cancel_token)
                if time.monotonic() >= deadline:
                    raise _deadline_error(count, method) from None
                raise _error("RPC transport failed", stage="transport", rpc_method=method,
                             request_count=count) from None
            raise_if_cancelled(cancel_token)
            if time.monotonic() > deadline:
                raise _deadline_error(count, method)
            try:
                encoded = _canonical(response)
            except DataApiError:
                raise
            if len(encoded) > MAX_RESPONSE_BYTES or not isinstance(status, int) or not 100 <= status <= 599:
                raise _error("Malformed RPC response", stage="response_envelope", rpc_method=method,
                             request_count=count)
            if (status != 200 or not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                    or response.get("id") != request["id"]
                    or ("result" in response) == ("error" in response)):
                code = response.get("error", {}).get("code") if isinstance(response, dict) and isinstance(response.get("error"), dict) else None
                detail = {"stage": "rpc_error" if status == 200 and type(code) is int else "response_envelope",
                          "rpc_method": method, "request_count": count}
                if type(code) is int and -(2**31) <= code < 2**31:
                    detail["rpc_error_code"] = code
                raise _error("RPC evidence failure", **detail)
            calls.append(request)
            responses.append(response)
            return response["result"]

        if _rpc_quantity(rpc("eth_chainId", []), label="chainId") != CHAIN_ID:
            raise _error("RPC chain is not Ethereum chain 1", stage="identity")
        block = _rpc_quantity(rpc("eth_blockNumber", []), label="blockNumber")
        tag = hex(block)
        first_header = _header(rpc("eth_getBlockByNumber", [tag, False]), block)
        rows: list[dict[str, Any]] = []
        base, quote = tokens["base"]["address"], tokens["quote"]["address"]
        for fee_tier in STANDARD_FEE_TIERS:
            raw_pool = _decode(rpc("eth_call", [{"to": FACTORY, "data": calldata(
                "getPool(address,address,uint24)", ["address", "address", "uint24"], [base, quote, fee_tier])}, tag]),
                ["address"])[0]
            if not isinstance(raw_pool, str) or not _ADDRESS.fullmatch(raw_pool):
                raise _error("Malformed pool address", stage="response_envelope")
            if int(raw_pool, 16) == 0:
                continue
            pool = _address(raw_pool, field="pool")
            factory = _address(_decode(rpc("eth_call", [{"to": pool, "data": calldata("factory()", [], [])}, tag]), ["address"])[0])
            token0 = _address(_decode(rpc("eth_call", [{"to": pool, "data": calldata("token0()", [], [])}, tag]), ["address"])[0])
            token1 = _address(_decode(rpc("eth_call", [{"to": pool, "data": calldata("token1()", [], [])}, tag]), ["address"])[0])
            fee = _decode(rpc("eth_call", [{"to": pool, "data": calldata("fee()", [], [])}, tag]), ["uint24"])[0]
            spacing = _decode(rpc("eth_call", [{"to": pool, "data": calldata("tickSpacing()", [], [])}, tag]), ["int24"])[0]
            decimals = [_decode(rpc("eth_call", [{"to": token, "data": calldata("decimals()", [], [])}, tag]), ["uint8"])[0]
                        for token in (token0, token1)]
            factory_spacing = _decode(rpc("eth_call", [{"to": FACTORY, "data": calldata(
                "feeAmountTickSpacing(uint24)", ["uint24"], [fee_tier])}, tag]), ["int24"])[0]
            if (factory != FACTORY or {token0, token1} != {base, quote} or fee != fee_tier
                    or spacing != factory_spacing or decimals != [tokens["base"]["decimals"] if token0 == base else tokens["quote"]["decimals"],
                                                                   tokens["quote"]["decimals"] if token1 == quote else tokens["base"]["decimals"]]):
                raise _error("Pool identity verification failed", stage="identity", fee_tier=fee_tier)
            rows.append({"chain_id": CHAIN_ID, "pool_address": pool, "factory": factory,
                         "token0": token0, "token1": token1, "symbols": [tokens["base"]["symbol"] if token0 == base else tokens["quote"]["symbol"],
                                                                            tokens["quote"]["symbol"] if token1 == quote else tokens["base"]["symbol"]],
                         "decimals": decimals, "fee": fee, "tick_spacing": spacing,
                         "block_number": block, "block_hash": first_header["hash"]})
        final_header = _header(rpc("eth_getBlockByNumber", [tag, False]), block)
        if final_header["hash"] != first_header["hash"]:
            raise _error("Pinned block hash changed during discovery", stage="reorg")
        for row in rows:
            row["row_sha256"] = _hash(row)
        transcript = [{"request": request, "response": response,
                       "request_sha256": _hash(request), "response_sha256": _hash(response)}
                      for request, response in zip(calls, responses)]
        return {"rows": rows, "source_url": rpc_url, "_envelope": {
            "provider": "uniswap", "action": "pool_discover", "chain": CHAIN_NAME, "chain_id": CHAIN_ID,
            "rpc_provider": provider, "request_args": {**args, "rpc_provider": provider},
            "pool_identity_scope": "current_only", "historical_data_verified": False,
            "financial_execution_allowed": False, "fee_tier_policy": "standard_documented_non_exhaustive",
            "fee_tiers_scanned": list(STANDARD_FEE_TIERS), "candidate_count": len(rows),
            "sources": [{"url": rpc_url, "sha256": _hash(transcript), "request_count": count,
                         "response_hashes": transcript}, catalog_source],
            "budget": {"request_count": count, "request_budget": MAX_REQUESTS,
                       "candidate_bound": MAX_CANDIDATES, "deadline_seconds": DEADLINE_SECONDS,
                       "deadline_unix": time.time()},
            "block": first_header, "rows_sha256": _hash(rows), "truncated": False,
        }}


def _decode(value: Any, types: list[str]) -> tuple[Any, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", value) or len(value) % 2:
        raise _error("Malformed ABI result", stage="response_envelope")
    try:
        return decode(types, bytes.fromhex(value[2:]))
    except Exception:
        raise _error("Malformed ABI result", stage="response_envelope") from None


def _header(value: Any, block: int) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("number") != hex(block):
        raise _error("Malformed pinned block header", stage="response_envelope")
    block_hash = value.get("hash")
    if not isinstance(block_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash):
        raise _error("Malformed pinned block header", stage="response_envelope")
    timestamp = _rpc_quantity(value.get("timestamp"), label="timestamp")
    return {"number": block, "hash": block_hash.lower(), "timestamp": timestamp}

