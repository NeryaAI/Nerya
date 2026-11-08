"""Bounded Aave rate and reserve reads, bound to an existing catalog UUID."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from ..connectors.chains import ChainRegistry
from ..connectors.http import HttpTransport, UrllibHttp
from ..core.errors import TradingError
from ..harness.cancellation import CancelledError, raise_if_cancelled
from .defillama import _hash, _iso, _limit, _result, _timestamp, _uuid
from .types import DataActionSpec, DataApiContext, DataApiError
from .uniswap_history import RPC_ORIGINS, floor_block_at

ORIGIN = "https://api.v3.aave.com"
CATALOG_ORIGIN = "https://yields.llama.fi"
MAX_BYTES = 2 * 1024 * 1024
MAX_HISTORY = 10_000
WINDOWS = ("LAST_DAY", "LAST_WEEK", "LAST_MONTH", "LAST_SIX_MONTHS", "LAST_YEAR")
ADDRESS = r"^0x[0-9a-fA-F]{40}$"
MAX_RESERVE_BLOCKS = 16
MAX_INDEX_POINTS = 500
# aave-v3-origin DataTypes.ReserveDataLegacy, returned by Pool.getReserveData.
RESERVE_TYPES = ["uint256", "uint128", "uint128", "uint128", "uint128", "uint128",
                 "uint40", "uint16", "address", "address", "address", "address",
                 "uint128", "uint128", "uint128"]
METADATA_QUERY = """query($request: MarketsRequest!) {
  markets(request: $request) { name address chain { name chainId }
    reserves { underlyingToken { address symbol } aToken { address symbol } } }
}"""
HISTORY_QUERY = """query($request: SupplyAPYHistoryRequest!) {
  supplyAPYHistory(request: $request) { date avgRate { raw decimals value formatted } }
}"""


def aave_specs(http: HttpTransport | None = None) -> list[DataActionSpec]:
    provider = AaveYieldProvider(http)
    supply_spec = DataActionSpec(
        provider="aave", action="supply_apy_history", title="Official Aave average supply APY history",
        description=(
            "Read official average supply APY, converted from fraction to percent, for an existing "
            "DefiLlama aave-v3 UUID. Exact UUID config aToken must uniquely match official metadata. "
            "Discover the UUID with defillama.yield_pools; resolve its aToken via public "
            "https://yields.llama.fi/configPool/<UUID>, then match aToken in official GraphQL "
            "markets(request:{chainIds:[chain_id]}) reserves to obtain market address and "
            "underlyingToken.address. Never infer a market from chain/symbol or a pool suffix. "
            "Supply explicit UTC start/end (inclusive, <=366 days) and a documented rolling "
            "window large enough to contain them. No window expansion, filling, rewards, "
            "realized returns or inferred completeness. APYSample.date bucket start/end and "
            "availability time are unverified: research/chart data only, not point-in-time "
            "policy replay input. Do not infer availability from timestamp. No wallet or credentials."
        ),
        input_schema={
            "type": "object", "additionalProperties": False,
            "required": ["pool_id", "chain_id", "market", "underlying_token", "start", "end", "window"],
            "properties": {
                "pool_id": {"type": "string", "description": "Exact existing DefiLlama pool UUID; preserved in output."},
                "chain_id": {"type": "integer", "minimum": 1},
                "market": {"type": "string", "pattern": ADDRESS,
                           "description": "Explicit official markets.address owning the catalog aToken."},
                "underlying_token": {"type": "string", "pattern": ADDRESS,
                                     "description": "Explicit official reserve underlyingToken.address."},
                **{k: {"type": "string", "maxLength": 40,
                       "description": "Inclusive UTC RFC3339 (Z/+00:00) or YYYY-MM-DD at UTC midnight."}
                   for k in ("start", "end")},
                "window": {"type": "string", "enum": list(WINDOWS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
            },
        },
        handler=provider.supply_apy_history, replay_input=False, output_kind="table",
        tags=("aave", "defi", "supply", "apy", "history"),
        docs_url="https://aave.com/docs/aave-v3/markets/data#apy-history",
    )
    return [supply_spec, DataActionSpec(
        provider="aave", action="reserve_state_history", title="Aave historical reserve state",
        description=(
            "Ethereum chain 1 only. Supply the same exact catalog UUID, official market and underlying "
            "identity as supply_apy_history, either unique block_numbers (1..16 samples) or a single "
            "at_utc (UTC date/RFC3339 whole seconds), and "
            "rpc_provider publicnode/drpc/tenderly. Each call uses one fixed public read-only RPC. "
            "After a source denial, investigate another supported public source for the same identity/window. "
            "UTC resolves to the floor block using the same RPC head and adjacent header timestamps; "
            "future/pre-genesis targets and targets without a next-block proof are rejected. "
            "Decode 15-word ReserveDataLegacy and Pool.getReserveNormalizedIncome(address) at the "
            "same block, then recheck its header hash. normalized_income_ray is the block-time "
            "income index in RAY; liquidity_index remains the stored reserve index. Raw uint reserve "
            "fields and base_supply_rate_percent are decimal strings. The rate is annualized base "
            "supply APR (currentLiquidityRate / 1e25), excluding rewards, not average or total APY, "
            "realized yield or a replacement for deposit_apy_percent. Research samples only: "
            "replay_input=False, point_in_time_ready=False, coverage not asserted."
        ),
        input_schema={
            "type": "object", "additionalProperties": False,
            "required": ["pool_id", "chain_id", "market", "underlying_token", "rpc_provider"],
            "oneOf": [{"required": ["block_numbers"]}, {"required": ["at_utc"]}],
            "properties": {
                **{k: supply_spec.input_schema["properties"][k]
                   for k in ("pool_id", "market", "underlying_token")},
                "chain_id": {"type": "integer", "enum": [1]},
                "block_numbers": {"type": "array", "minItems": 1, "maxItems": MAX_RESERVE_BLOCKS,
                                  "uniqueItems": True,
                                  "items": {"type": "integer", "minimum": 0, "maximum": 2**64 - 1}},
                "at_utc": {"type": "string", "maxLength": 25,
                           "description": "UTC YYYY-MM-DD or RFC3339 whole seconds Z/+00:00; mutually exclusive with block_numbers."},
                "rpc_provider": {"type": "string", "enum": list(RPC_ORIGINS)},
            },
        },
        handler=provider.reserve_state_history, replay_input=False, output_kind="table",
        tags=("aave", "defi", "reserve", "apr", "history"),
    ), DataActionSpec(
        provider="aave", action="reserve_index_history", title="Aave reserve index observation grid",
        description=(
            "Ethereum chain 1, exact catalog UUID and official reserve identity, fixed rpc_provider only. "
            "Explicit whole-second UTC start/end, <=366 days; interval_seconds defaults to 86400. "
            "Acquire start + k * interval and the exact terminal end once, at most 500 points; "
            "reject oversized grids and fail on any missing point without partial coverage or filling. "
            "Each decision instant uses canonical floor-block reserve state and normalized income, "
            "with identity, source hashes and adjacent-block proofs retained. Timestamp is the requested "
            "decision instant; source_timestamp is actual block time. Base supply APR remains APR, "
            "never APY. Replay admission is ONLY for historical_reserve_index_policy_simulation: "
            "instantaneous observation and confirmation latency ignored, actual availability unverified, "
            "point_in_time_ready=False. No live/paper execution or finalized performance evidence."
        ),
        input_schema={
            "type": "object", "additionalProperties": False,
            "required": ["pool_id", "chain_id", "market", "underlying_token", "rpc_provider", "start", "end"],
            "properties": {
                **{k: supply_spec.input_schema["properties"][k]
                   for k in ("pool_id", "market", "underlying_token", "start", "end")},
                "chain_id": {"type": "integer", "enum": [1]},
                "rpc_provider": {"type": "string", "enum": list(RPC_ORIGINS)},
                "interval_seconds": {"type": "integer", "minimum": 1, "default": 86400},
            },
        },
        handler=provider.reserve_index_history, replay_input=True, output_kind="table",
        tags=("aave", "defi", "reserve", "index", "history", "simulation"),
    )]


def _error(message: str, kind: str = "provider_error") -> DataApiError:
    return DataApiError(message, kind=kind, detail={"provider": "aave", "action": "supply_apy_history"}, retryable=False)


def _address(value: Any, kind: str = "provider_error") -> str:
    if not isinstance(value, str) or not re.fullmatch(ADDRESS, value):
        raise _error("Expected an EVM address", kind)
    return value.lower()


def _identity_args(args):
    pool_id = _uuid(args["pool_id"], kind="schema_validation")
    if pool_id != args["pool_id"]:
        raise _error("pool_id must use its canonical catalog UUID spelling", "schema_validation")
    market = _address(args["market"], "schema_validation")
    token = _address(args["underlying_token"], "schema_validation")
    chain_id = args["chain_id"]
    if type(chain_id) is not int or chain_id <= 0:
        raise _error("Invalid chain_id", "schema_validation")
    return pool_id, chain_id, market, token


class AaveYieldProvider:
    def __init__(self, http: HttpTransport | None = None) -> None:
        self.http = http if http is not None else UrllibHttp(
            max_response_bytes=MAX_BYTES,
            allowed_origins=(ORIGIN, CATALOG_ORIGIN, *RPC_ORIGINS.values()), strict_json=True)

    def _read(self, context, url, body=None):
        raise_if_cancelled(context.cancel_token)
        try:
            status, doc = self.http.request(
                "POST" if body is not None else "GET", url, body=body,
                headers={"Accept": "application/json"}, timeout=15.0)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _error("Invalid or oversized public source response") from exc
        except Exception as exc:
            raise DataApiError("Aave history source transport failed", retryable=True) from exc
        raise_if_cancelled(context.cancel_token)
        if status != 200:
            raise DataApiError("Aave history source HTTP failure",
                               detail={"source_url": url, "http_status": status},
                               retryable=status == 429 or status >= 500)
        if not isinstance(doc, dict) or doc.get("errors") or "data" not in doc:
            raise _error("Public source returned an invalid response or GraphQL errors")
        source = {"url": url, "retrieved_at": _iso(datetime.now(timezone.utc)),
                  "sha256": _hash(doc, max_bytes=MAX_BYTES),
                  "request_sha256": _hash({"method": "POST" if body is not None else "GET",
                                           "url": url, "body": body}),
                  "hash_basis": "canonical_parsed_json_utf8_sorted_keys"}
        if body is not None:
            source["request"] = body
        elif doc.get("status") != "success":
            raise _error("Catalog response must have status=success")
        return doc["data"], source

    def _resolve_identity(self, context, pool_id, chain_id, market, token):
        catalog, catalog_source = self._read(context, CATALOG_ORIGIN + "/configPool/" + pool_id)
        if not isinstance(catalog, list) or len(catalog) != 1 or not isinstance(catalog[0], dict):
            raise _error("Exact UUID config must resolve to one catalog row")
        pool = catalog[0]
        if pool.get("config_id") != pool_id or pool.get("project") != "aave-v3":
            raise _error("Catalog UUID/project mismatch")
        try:
            chain = ChainRegistry().get(pool.get("chain", ""))
        except (TradingError, AttributeError) as exc:
            raise _error("Catalog chain is not in the reviewed chain registry") from exc
        tokens = pool.get("underlyingTokens")
        symbol = pool.get("symbol")
        if (chain.chain_id != chain_id or not isinstance(tokens, list) or len(tokens) != 1
                or _address(tokens[0]) != token or not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Za-z0-9_.]{1,256}", symbol)):
            raise _error("Catalog chain/token/symbol mismatch")
        # Upstream Aave adapter keys start with the aToken address. No market suffix mapping.
        key = pool.get("pool")
        if not isinstance(key, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}-[A-Za-z0-9_-]{1,80}", key):
            raise _error("Catalog lacks an address-qualified Aave reserve key")
        a_token = _address(key[:42])
        if pool.get("token") is not None and _address(pool["token"]) != a_token:
            raise _error("Catalog aToken identity conflict")
        metadata, metadata_source = self._read(context, ORIGIN + "/graphql",
            {"query": METADATA_QUERY, "variables": {"request": {"chainIds": [chain_id]}}})
        markets = metadata.get("markets") if isinstance(metadata, dict) else None
        if not isinstance(markets, list) or len(markets) > 100:
            raise _error("Official markets metadata missing or exceeds bound")
        matches = []
        for item in markets:
            if not isinstance(item, dict) or not isinstance(item.get("reserves"), list) or len(item["reserves"]) > 1000:
                raise _error("Invalid or oversized official market reserves")
            for reserve in item["reserves"]:
                if not isinstance(reserve, dict) or not isinstance(reserve.get("aToken"), dict):
                    raise _error("Invalid official reserve metadata")
                if _address(reserve["aToken"].get("address")) == a_token:
                    matches.append((item, reserve))
        if len(matches) != 1:
            raise _error("Catalog aToken must uniquely identify one official market/reserve")
        official, reserve = matches[0]
        underlying = reserve.get("underlyingToken")
        if (_address(official.get("address")) != market or not isinstance(official.get("chain"), dict)
                or official["chain"].get("chainId") != chain_id or not isinstance(underlying, dict)
                or _address(underlying.get("address")) != token or underlying.get("symbol") != symbol):
            raise _error("Official market/chain/reserve differs from requested catalog identity")
        return pool, chain, official, a_token, [catalog_source, metadata_source]

    def supply_apy_history(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        required = {"pool_id", "chain_id", "market", "underlying_token", "start", "end", "window"}
        if not required <= args.keys() or args.keys() - required - {"limit"}:
            raise _error("Supply exact identity, UTC boundaries and rolling window; no endpoint overrides", "schema_validation")
        pool_id, chain_id, market, token = _identity_args(args)
        if args["window"] not in WINDOWS:
            raise _error("Invalid documented rolling window", "schema_validation")
        start, end = (_timestamp(args[k], kind="schema_validation") for k in ("start", "end"))
        if not timedelta(0) <= end - start <= timedelta(days=366):
            raise _error("Require start <= end with a window <=366 days", "schema_validation")
        limit = _limit({"limit": args.get("limit", 500)})
        pool, chain, official, a_token, sources = self._resolve_identity(context, pool_id, chain_id, market, token)
        symbol, tokens = pool["symbol"], pool["underlyingTokens"]
        request = {"market": market, "underlyingToken": token, "chainId": chain_id, "window": args["window"]}
        history, history_source = self._read(context, ORIGIN + "/graphql",
            {"query": HISTORY_QUERY, "variables": {"request": request}})
        raw = history.get("supplyAPYHistory") if isinstance(history, dict) else None
        if not isinstance(raw, list) or len(raw) > MAX_HISTORY:
            raise _error("Official APY history missing or exceeds row bound")
        by_time = {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("avgRate"), dict):
                raise _error("Invalid official APY observation")
            stamp = _timestamp(item.get("date"))
            value = item["avgRate"].get("value")
            if not isinstance(value, str) or not 0 < len(value) <= 128:
                raise _error("Official avgRate.value must be a bounded decimal fraction")
            try:
                apy = float(Decimal(value) * 100)
            except (InvalidOperation, OverflowError, ValueError) as exc:
                raise _error("Invalid official supply APY fraction") from exc
            if not math.isfinite(apy) or apy < 0:
                raise _error("Official supply APY must be finite and nonnegative")
            row = {"timestamp": _iso(stamp), "observation_date": item["date"],
                   "available_at": None, "pool_id": pool_id, "deposit_apy_percent": apy}
            if stamp in by_time and by_time[stamp] != row:
                raise _error("Conflicting duplicate APY observation")
            by_time[stamp] = row
        stamps = sorted(by_time)
        selected = [by_time[t] for t in stamps if start <= t <= end]
        times = [t for t in stamps if start <= t <= end]
        days = [(start.date() + timedelta(days=i)).isoformat() for i in range((end.date() - start.date()).days + 1)]
        observed_days = {r["timestamp"][:10] for r in selected}
        return _result(selected, limit, {
            "provider": "aave", "mode": "public_read_only", "mock": False,
            "observation_kind": "historical_rate_observations", "apy_unit": "percent_per_year",
            "rate_semantics": "Official supplyAPYHistory average supply APY fraction (avgRate.value) times 100; excludes rewards and borrow rates, not raw RAY APR.",
            "averaging_scope": "upstream_average_interval_unknown",
            "timestamp_semantics": "APYSample.date normalized to UTC; bucket start/end unknown, not an availability timestamp.",
            "availability_scope": "unverified", "point_in_time_ready": False, "replay_input": False,
            "return_semantics": "Annualized observations, not realized yield or index accrual; no filling or resampling.",
            "pool_identity_scope": "current_catalog_metadata",
            "pool": {"pool_id": pool_id, "project": "aave-v3", "chain": pool["chain"], "symbol": symbol,
                     "underlying_tokens": tokens, "pool_meta": pool.get("poolMeta"),
                     "canonical_identity": {"market": f"AAVE_V3:{chain.name}-{symbol}", "protocol": "aave_v3",
                         "component_id": "builtin:aave", "chain": chain.name, "asset": symbol, "pool_id": pool_id}},
            "official_identity": {"chain_id": chain_id, "market": market, "market_name": official.get("name"),
                                  "underlying_token": token, "a_token": a_token},
            "sources": [*sources, {**history_source, "source_row_count": len(raw)}],
            "rolling_window": args["window"],
            "requested_window": {"start": _iso(start), "end": _iso(end), "inclusive": True},
            "source_observed_range": {"start": _iso(stamps[0]) if stamps else None, "end": _iso(stamps[-1]) if stamps else None},
            "boundary_observations": {"before": next((by_time[t] for t in reversed(stamps) if t < start), None),
                                      "after": next((by_time[t] for t in stamps if t > end), None)},
            "duplicate_timestamps_removed": len(raw) - len(stamps),
            "coverage": {
                "scope": "all_matching_observations_before_limits", "observations": len(selected),
                "observed_range": {"start": _iso(times[0]) if times else None, "end": _iso(times[-1]) if times else None},
                "observed_utc_days": len(observed_days), "requested_utc_days": len(days),
                "missing_utc_days": [d for d in days if d not in observed_days],
                "gaps_over_24h": [{"start": _iso(a), "end": _iso(b), "seconds": (b-a).total_seconds()}
                                   for a, b in zip(times, times[1:]) if b-a > timedelta(days=1)],
                "max_gap_seconds": max(((b-a).total_seconds() for a, b in zip(times, times[1:])), default=None),
                "leading_gap_seconds": (times[0]-start).total_seconds() if times else None,
                "trailing_gap_seconds": (end-times[-1]).total_seconds() if times else None,
                "null_counts": {"deposit_apy_percent": 0}, "completeness": "not_asserted",
                "note": "Observed timestamps only; no inferred cadence, retention, filling or realized returns.",
            },
        })

    def reserve_state_history(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        required = {"pool_id", "chain_id", "market", "underlying_token", "rpc_provider"}
        if (not required <= args.keys() or args.keys() - required - {"block_numbers", "at_utc"}
                or ("block_numbers" in args) == ("at_utc" in args)):
            raise DataApiError("Supply exact identity, rpc_provider and exactly one of block_numbers/at_utc",
                               kind="schema_validation", retryable=False)
        target = None
        try:
            pool_id, chain_id, market, token = _identity_args(args)
            if "at_utc" in args:
                stamp = _timestamp(args["at_utc"], kind="schema_validation")
                if stamp.microsecond:
                    raise DataApiError("at_utc requires whole UTC seconds", kind="schema_validation", retryable=False)
                if stamp > datetime.now(timezone.utc):
                    raise DataApiError("at_utc is in the future", kind="schema_validation", retryable=False)
                target = int(stamp.timestamp())
        except DataApiError as exc:
            exc.detail.update(provider="aave", action="reserve_state_history")
            raise
        blocks, provider = args.get("block_numbers"), args["rpc_provider"]
        if (chain_id != 1 or not isinstance(provider, str) or provider not in RPC_ORIGINS
                or (target is None and (not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_RESERVE_BLOCKS
                    or any(type(n) is not int or not 0 <= n < 2**64 for n in blocks)
                    or len(set(blocks)) != len(blocks)))):
            raise DataApiError("Require Ethereum, fixed RPC provider and 1..16 unique uint64 block numbers",
                               kind="schema_validation", retryable=False)
        try:
            pool, _, _, a_token, sources = self._resolve_identity(context, pool_id, chain_id, market, token)
        except DataApiError as exc:
            exc.detail.update(provider="aave", action="reserve_state_history")
            raise
        from eth_abi import decode
        from eth_abi.exceptions import DecodingError
        from ..financial.defi.calls import calldata

        url = RPC_ORIGINS[provider]
        call = {"to": market, "data": calldata("getReserveData(address)", ["address"], [token])}
        income_call = {"to": market, "data": calldata("getReserveNormalizedIncome(address)", ["address"], [token])}

        def rpc(method, params):
            raise_if_cancelled(context.cancel_token)
            body = {"jsonrpc": "2.0", "id": len(sources), "method": method, "params": params}
            detail = {"provider": "aave", "action": "reserve_state_history", "source_url": url,
                      "rpc_method": method, "request_sha256": _hash(body)}
            try:
                status, doc = self.http.request("POST", url, body=body,
                    headers={"Accept": "application/json"}, timeout=15.0)
            except CancelledError:
                raise
            except Exception:
                raise_if_cancelled(context.cancel_token)
                raise DataApiError("Aave reserve RPC transport failed", detail=detail, retryable=False) from None
            raise_if_cancelled(context.cancel_token)
            try:
                detail["response_sha256"] = _hash(doc, max_bytes=MAX_BYTES)
            except (DataApiError, UnicodeError):
                raise DataApiError("Invalid or oversized Aave RPC response", detail=detail, retryable=False) from None
            if type(status) is int:
                detail["http_status"] = status
            code = doc.get("error", {}).get("code") if isinstance(doc, dict) and isinstance(doc.get("error"), dict) else None
            if type(code) is int and -(2**31) <= code < 2**31:
                detail["rpc_error_code"] = code
            if status in {401, 403}:
                raise DataApiError(
                    "Historical reserve RPC access was denied; no reserve state was accepted. "
                    "Investigate another supported public read-only source for the same pool and window. "
                    "This action accepts no arbitrary credentials or custom endpoint.",
                    kind="configuration_required", retryable=False,
                    detail={**detail, "reason": "historical_rpc_authorization_required",
                            "access_status": "denied", "archive_access_established": False,
                            "arbitrary_credentials_supported": False, "custom_endpoint_supported": False})
            if (status != 200 or not isinstance(doc, dict) or doc.get("jsonrpc") != "2.0"
                    or type(doc.get("id")) is not int or doc["id"] != body["id"]
                    or "result" not in doc or "error" in doc or doc["result"] is None):
                raise DataApiError("Aave reserve RPC read failed", detail=detail, retryable=False)
            sources.append({"url": url, "request": body, "request_sha256": detail["request_sha256"],
                            "sha256": detail["response_sha256"],
                            "retrieved_at": _iso(datetime.now(timezone.utc)),
                            "hash_basis": "canonical_parsed_json_utf8_sorted_keys"})
            return doc["result"]

        def quantity(value):
            if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})", value):
                raise DataApiError("Invalid Aave RPC block quantity", retryable=False)
            return int(value, 16)

        def header(number):
            doc = rpc("eth_getBlockByNumber", [hex(number), False])
            if (not isinstance(doc, dict) or quantity(doc.get("number")) != number
                    or not isinstance(doc.get("hash"), str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", doc["hash"])):
                raise DataApiError("Aave block header identity mismatch", retryable=False)
            return {"block_number": number, "block_hash": doc["hash"].lower(), "timestamp": quantity(doc.get("timestamp"))}

        if quantity(rpc("eth_chainId", [])) != chain_id:
            raise DataApiError("Aave RPC chain differs from requested identity", retryable=False)
        resolution = {}
        resolved_headers = {}
        if target is not None:
            resolver_source_start = len(sources)
            head = quantity(rpc("eth_blockNumber", []))
            try:
                number, reads = floor_block_at(target, head, header, resolved_headers)
            except DataApiError as exc:
                exc.detail.update(provider="aave", action="reserve_state_history")
                raise
            if number == head:
                raise DataApiError("at_utc is at or beyond the current RPC head timestamp; next-block proof unavailable",
                                   kind="schema_validation", retryable=False,
                                   detail={"provider": "aave", "action": "reserve_state_history",
                                           "requested_timestamp": target, "head_timestamp": resolved_headers[head]["timestamp"]})
            # The resolver's final bracket is adjacent, retained in its caller-owned cache.
            if not resolved_headers[number]["timestamp"] <= target < resolved_headers[number + 1]["timestamp"]:
                raise DataApiError("Aave UTC floor proof is invalid", retryable=False)
            blocks = [number]
        rows = []
        for number in blocks:
            source_start = len(sources)
            block = header(number)
            if target is not None and block != resolved_headers[number]:
                raise DataApiError("Aave resolved floor block changed before reserve read", retryable=False)
            raw = rpc("eth_call", [call, hex(number)])
            if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{960}", raw):
                raise DataApiError("Expected exactly 15 ABI words of Aave ReserveDataLegacy", retryable=False)
            try:
                values = decode(RESERVE_TYPES, bytes.fromhex(raw[2:]))
            except (DecodingError, ValueError):
                raise DataApiError("Invalid Aave ReserveDataLegacy ABI encoding", retryable=False) from None
            if values[8].lower() != a_token or values[6] > block["timestamp"]:
                raise DataApiError("Historical Aave aToken or last-update identity mismatch", retryable=False)
            raw_income = rpc("eth_call", [income_call, hex(number)])
            if not isinstance(raw_income, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw_income):
                raise DataApiError("Expected exactly one ABI uint256 word of Aave normalized income", retryable=False)
            normalized_income = int(raw_income, 16)
            if header(number) != block:
                raise DataApiError("Aave canonical block changed during reserve read", retryable=False)
            with localcontext() as precision:
                precision.prec = 80
                rate = format(Decimal(values[2]) / Decimal(10**25), "f")
            rows.append({**block, "pool_id": pool_id, "liquidity_index": str(values[1]),
                         "normalized_income_ray": str(normalized_income),
                         "current_liquidity_rate_ray": str(values[2]), "last_update_timestamp": str(values[6]),
                         "a_token": values[8].lower(), "base_supply_rate_percent": rate,
                         "source_refs": list(range(source_start, len(sources)))})
        if target is not None:
            if header(number + 1) != resolved_headers[number + 1]:
                raise DataApiError("Aave UTC next-block proof changed during reserve read", retryable=False)
            resolution = {
                "requested_at_utc": args["at_utc"], "resolved_block_numbers": blocks,
                "resolver_proof": {
                    "policy": "floor_timestamp_lte_target_lt_next_timestamp", "requested_timestamp": target,
                    "head": resolved_headers[head], "floor": resolved_headers[number],
                    "next": resolved_headers[number + 1], "resolver_header_reads": reads,
                    "source_refs": list(range(resolver_source_start, len(sources))),
                },
            }
        return _result(rows, len(blocks), {
            "provider": "aave", "action": "reserve_state_history", "mode": "public_read_only", "mock": False,
            "pool": {"pool_id": pool_id, "project": "aave-v3", "chain": "Ethereum", "symbol": pool["symbol"]},
            "official_identity": {"pool_id": pool_id, "chain_id": chain_id, "market": market,
                                  "underlying_token": token, "a_token": a_token},
            "pool_identity_scope": "current_catalog_metadata_checked_against_historical_atoken",
            "requested_block_numbers": args.get("block_numbers"), "rpc_provider": provider, **resolution,
            "rate_semantics": "Annualized base supply APR: currentLiquidityRate / 1e25 percent; excludes rewards, not average/total APY or realized yield. Does not replace deposit_apy_percent.",
            "index_semantics": "liquidity_index is the stored reserve index at last_update_timestamp; normalized_income_ray is Pool.getReserveNormalizedIncome(address) at the requested block, an exact uint256 RAY index (1e27).",
            "timestamp_semantics": "Canonical block timestamp, UTC Unix seconds; last_update_timestamp is the reserve update time, not availability time.",
            "uint_encoding": "Reserve uint fields are exact decimal strings; block number/timestamp are integers.",
            "base_supply_rate_percent_encoding": "exact_decimal_string",
            "canonicality": "single_rpc_block_hash_rechecked_not_finality_proof",
            "point_in_time_ready": False, "replay_input": False,
            "coverage": {"scope": "utc_floor_sample_only" if target is not None else "explicit_block_samples_only",
                         "observations": len(rows), "completeness": "not_asserted"},
            "sources": sources, "source_ref_semantics": "zero-based indices into sources; row refs: header, reserve call, normalized income call, header recheck; resolver refs: head, resolver and verification reads",
        })

    def reserve_index_history(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        required = {"pool_id", "chain_id", "market", "underlying_token", "rpc_provider", "start", "end"}
        try:
            if not required <= args.keys() or args.keys() - required - {"interval_seconds"}:
                raise _error("Supply exact identity, rpc_provider and explicit UTC start/end", "schema_validation")
            _identity_args(args)
            start, end = (_timestamp(args[k], kind="schema_validation") for k in ("start", "end"))
            interval = args.get("interval_seconds", 86400)
            if (start.microsecond or end.microsecond or not timedelta(0) <= end - start <= timedelta(days=366)
                    or end > datetime.now(timezone.utc) or type(interval) is not int or interval < 1
                    or args["chain_id"] != 1 or not isinstance(args["rpc_provider"], str)
                    or args["rpc_provider"] not in RPC_ORIGINS):
                raise _error("Require Ethereum, fixed RPC, whole UTC seconds, start <= end <= now, "
                             "<=366 days and a positive integer interval_seconds", "schema_validation")
            duration = int((end - start).total_seconds())
            count = duration // interval + 1 + bool(duration % interval)
            if count > MAX_INDEX_POINTS:
                raise _error("Requested observation grid exceeds 500 points including terminal end", "schema_validation")
        except DataApiError as exc:
            exc.detail.update(provider="aave", action="reserve_index_history")
            raise

        rows, sources, points = [], [], []
        identity_args = {k: args[k] for k in required - {"start", "end"}}
        first = None
        for k in range(count):
            instant = _iso(start + timedelta(seconds=min(k * interval, duration)))
            raise_if_cancelled(context.cancel_token)
            try:
                sample = self.reserve_state_history(context, {**identity_args, "at_utc": instant})
                env = sample["_envelope"]
                if len(sample["rows"]) != 1 or env["provider_truncated"]:
                    raise _error("Reserve observation missing; full grid was not acquired")
                if first is not None and (env["pool"] != first["pool"]
                                          or env["official_identity"] != first["official_identity"]):
                    raise _error("Reserve identity changed during observation grid acquisition")
            except DataApiError as exc:
                exc.detail.update(provider="aave", action="reserve_index_history",
                                  requested_at_utc=instant, coverage_complete=False)
                raise
            if first is None:
                first = env
            offset = len(sources)
            sources.extend(env["sources"])
            row = sample["rows"][0]
            rows.append({**row, "timestamp": instant,
                         "source_timestamp": _iso(datetime.fromtimestamp(row["timestamp"], timezone.utc)),
                         "block_timestamp": row["timestamp"],
                         "source_refs": [offset + ref for ref in row["source_refs"]]})
            points.append({"requested_at_utc": instant,
                           "identity_source_refs": [offset, offset + 1],
                           "chain_source_ref": offset + 2,
                           "resolver_proof": {**env["resolver_proof"],
                               "source_refs": [offset + ref for ref in env["resolver_proof"]["source_refs"]]}})
        return _result(rows, count, {
            "provider": "aave", "action": "reserve_index_history", "mode": "public_read_only", "mock": False,
            "observation_kind": "historical_reserve_index_observations",
            **{key: first[key] for key in ("pool", "official_identity", "pool_identity_scope", "rpc_provider",
                                          "rate_semantics", "index_semantics", "uint_encoding",
                                          "base_supply_rate_percent_encoding", "canonicality")},
            "requested_window": {"start": _iso(start), "end": _iso(end)},
            "observation_interval_seconds": interval,
            "timestamp_semantics": "timestamp is requested UTC decision time; source_timestamp/block_timestamp "
                                   "are canonical floor-block UTC ISO/Unix seconds, not availability times.",
            "simulation_assumption": "Canonical-floor-block state with instantaneous observation; confirmation latency ignored.",
            "availability_scope": "unverified", "point_in_time_ready": False, "replay_input": True,
            "replay_scope": "historical_reserve_index_policy_simulation",
            "performance_evidence": False, "financial_execution_allowed": False,
            "coverage": {"scope": "requested_observation_grid", "expected_count": count,
                         "observations": len(rows), "complete": len(rows) == count, "missing_utc_days": [],
                         "null_counts": {"normalized_income_ray": 0, "base_supply_rate_percent": 0}},
            "sources": sources, "point_sources": points,
            "source_ref_semantics": "All refs are zero-based indices into sources, shifted per point; "
                                    "row refs retain header/reserve/income/recheck order; point_sources retains "
                                    "catalog/official identity, chain check and adjacent floor-block proofs.",
        })
