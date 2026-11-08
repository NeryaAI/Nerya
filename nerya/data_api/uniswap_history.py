"""Bounded Ethereum-1 Uniswap v3 samples, never fills or realized yield.

Contract v1: UTC Unix seconds (integer), lower-case addresses and decimal
strings for raw uint128/uint160/uint256 values. No interpolation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any, Literal

from ..connectors.http import HttpTransport, UrllibHttp, KeepAliveHttp
from ..harness.cancellation import CancelledError, raise_if_cancelled
from .types import DataActionSpec, DataApiContext, DataApiError

RPC_URL = "https://ethereum-rpc.publicnode.com"
RPC_ORIGINS = {"publicnode": RPC_URL, "drpc": "https://eth.drpc.org",
               "tenderly": "https://mainnet.gateway.tenderly.co"}
FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
MAX_SAMPLES = 64
MAX_RESOLVER_READS = 32  # Per endpoint, including uncached boundary headers.
MAX_REQUESTS = 2 + 17 * MAX_SAMPLES + 2 * MAX_RESOLVER_READS
MAX_RESPONSE_BYTES = 256 * 1024
CATALOG_URL = "https://tokens.uniswap.org"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
DEADLINE_SECONDS = 360.0
RPC_RATE_LIMIT_PER_SEC = 0.75  # Per lane; anonymous public-origin courtesy pace.
TIMEOUT_SECONDS = 10.0
RpcMethod = Literal["eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_call"]
RpcFailureStage = Literal["http_status", "rpc_error", "response_envelope", "missing_result",
                          "transport", "deadline", "request_budget", "response_size"]
RPC_METHODS = ("eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_call")
MAX_BLOCK = 2**64 - 1
TICK_TYPES = ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]
SLOT_TYPES = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]
LIMITATIONS = ["sampled_counterfactual_only_no_fills", "no_tick_path_between_samples",
                "modeled_fee_growth_not_realized_yield"]
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "pool_address": {"type": "string", "pattern": r"^0x[0-9a-fA-F]{40}$"},
        "rpc_provider": {"type": "string", "enum": list(RPC_ORIGINS), "default": "publicnode"},
        **{key: {"type": "integer", "minimum": 0, "maximum": MAX_BLOCK}
           for key in ("start_block", "end_block")},
        **{key: {"type": "string", "maxLength": 25,
                 "description": "UTC YYYY-MM-DD midnight or RFC3339 seconds Z/+00:00. Use start/end together instead of block bounds; each resolves to floor block. Exact block delta and sample count are only known after UTC resolution; preserve both requested dates."}
           for key in ("start", "end")},
        "step_blocks": {"type": "integer", "minimum": 1, "maximum": MAX_BLOCK,
                        "description": "Block stride; both endpoints included once require ceil((end_block - start_block) / step_blocks) + 1 samples. Exact UTC count is only known after resolution. No automatic adjustment."},
        "max_samples": {"type": "integer", "minimum": 1, "maximum": MAX_SAMPLES,
                        "description": "Sample ceiling, not interval count: both endpoints included once require ceil((end_block - start_block) / step_blocks) + 1 samples. For a positive delta, at least 2 samples are needed; minimum stride is ceil(delta / (max_samples - 1)). No truncation or budget increase."},
        **{key: {"type": "integer", "minimum": -887272, "maximum": 887272}
           for key in ("tick_lower", "tick_upper")},
    },
    "required": ["pool_address", "step_blocks", "max_samples", "tick_lower", "tick_upper"],
    "oneOf": [{"required": ["start_block", "end_block"], "not": {"anyOf": [{"required": ["start"]}, {"required": ["end"]}]}},
              {"required": ["start", "end"], "not": {"anyOf": [{"required": ["start_block"]}, {"required": ["end_block"]}]}}],
    "additionalProperties": False,
}


def uniswap_specs(http: HttpTransport | None = None, *,
                  catalog_http: HttpTransport | None = None) -> list[DataActionSpec]:
    history_spec = DataActionSpec(
        provider="uniswap", action="pool_history", title="Uniswap v3 historical pool state",
        description=("Ethereum chain 1 only; fixed public credential-free RPC origins: publicnode (default) "
                     "or explicit drpc/tenderly, without retries or automatic fallback. Use paired start/end UTC "
                     "dates OR start_block/end_block. Dates resolve to floor blocks by bounded binary search "
                     "(32 header reads per endpoint); actual timestamp offsets are reported. Step from start "
                     "and include both endpoints once: ceil((end_block - start_block) / step_blocks) + 1 samples. "
                     "Exact UTC sample count is only known after block resolution. At most 64 samples; "
                     "oversized requests rejected, never truncated. UTC Unix-second timestamps. "
                     "Sampled counterfactual inputs only, no fills, tick path, complete-window "
                     "replay or realized yield. Archive state availability required: publicnode serves "
                     "recent state only and demands a personal token for archive depth, so multi-week "
                     "archive sampling can explicitly select rpc_provider='drpc' or 'tenderly'. Public capacity "
                     "is limited; requests are paced per lane and an HTTP 429 rate limit stays terminal "
                     "for this read. Token symbols are current official catalog identity, never "
                     "historical symbol evidence or aliases."),
        input_schema=INPUT_SCHEMA, handler=UniswapHistoryProvider(http, catalog_http=catalog_http).pool_history,
        tags=("uniswap", "lp", "defi", "ethereum", "history"), output_kind="table",
        docs_url="https://developers.uniswap.org/docs/ecosystem/subgraphs/concepts/v3/queries",
        # Audited: fixed public origins, no context/config/vault, bounded read-only RPCs.
        replay_input=True,
    )
    from .uniswap_discovery import uniswap_discovery_specs
    return [history_spec, *uniswap_discovery_specs(http, catalog_http=catalog_http)]


def _error(message: str, *, kind: str = "provider_error") -> DataApiError:
    return DataApiError(message, kind=kind, retryable=False,
                        detail={"provider": "uniswap", "action": "pool_history",
                                "coverage_complete": False})


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise _error("Invalid RPC JSON evidence") from None


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _address(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value) or int(value, 16) == 0:
        raise _error("Invalid or missing contract address")
    return value.lower()


def _quantity(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})", value):
        raise _error("Invalid RPC quantity")
    return int(value, 16)


def _utc(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+]00:00))?", value
    ):
        raise _error("Window requires UTC dates or RFC3339 whole seconds", kind="schema_validation")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        seconds = int(stamp.timestamp())
    except (ValueError, OverflowError, OSError):
        raise _error("Invalid UTC window", kind="schema_validation") from None
    if seconds < 0 or seconds > int(time.time()):
        raise _error("UTC window must be past or present", kind="schema_validation")
    return seconds


def floor_block_at(target, head, header, cache, max_reads=None, *, anchor=None):
    """Resolve a timestamp with caller-owned header IO/cache and a bounded read count."""
    if max_reads is None:
        max_reads = MAX_RESOLVER_READS
    reads = 0

    def at(number):
        nonlocal reads
        if number not in cache:
            if reads >= max_reads:
                raise _error("UTC block resolver read budget exhausted")
            cache[number] = header(number)
            reads += 1
        return cache[number]

    if anchor is not None:
        if type(anchor) is not int or not 0 <= anchor < head or at(anchor)["timestamp"] > target:
            raise _error("Invalid resolver anchor")
        low, high = anchor, anchor + 1
        interval = at(high)["timestamp"] - at(low)["timestamp"]
        if interval <= 0:
            raise _error("Nonchronological resolver block timestamps")
        step = max(1, (target - at(high)["timestamp"]) // interval + 1)
        while at(high)["timestamp"] <= target:
            if high == head:
                return head, reads
            low, high = high, min(head, high + step)
            if at(high)["timestamp"] <= at(low)["timestamp"]:
                raise _error("Nonchronological resolver block timestamps")
            step *= 2
    else:
        high = head
        high_header = at(high)
        if target >= high_header["timestamp"]:
            return head, reads
        recent = at(head - 1) if head else high_header
        interval = high_header["timestamp"] - recent["timestamp"]
        if interval <= 0:
            raise _error("Nonchronological resolver block timestamps")
        # Use the verified latest interval only to choose a probe. It
        # is not a certified block-time assumption; the probe header
        # must establish the actual bracket before binary search.
        distance = max(1, (high_header["timestamp"] - target + interval - 1) // interval)
        candidate = max(0, head - distance)
        candidate_header = at(candidate)
        if not candidate_header["timestamp"] < high_header["timestamp"]:
            raise _error("Nonchronological resolver block timestamps")
        if candidate_header["timestamp"] <= target:
            low = candidate
        else:
            high, high_header = candidate, candidate_header
            step = max(1, distance)
            while high > 0:
                candidate = max(0, high - step)
                candidate_header = at(candidate)
                if not candidate_header["timestamp"] < high_header["timestamp"]:
                    raise _error("Nonchronological resolver block timestamps")
                if candidate_header["timestamp"] <= target:
                    low = candidate
                    break
                high, high_header = candidate, candidate_header
                step *= 2
            else:
                raise _error("UTC window predates Ethereum genesis", kind="schema_validation")
    while high - low > 1:
        mid = (low + high) // 2
        if anchor is not None:
            # Probe the verified timestamp bracket once, then retain binary search.
            mid = min(high - 1, max(low + 1, low + (target - at(low)["timestamp"])
                * (high - low) // (at(high)["timestamp"] - at(low)["timestamp"])))
            anchor = None
        stamp = at(mid)["timestamp"]
        if not at(low)["timestamp"] < stamp < at(high)["timestamp"]:
            raise _error("Nonchronological resolver block timestamps")
        if stamp <= target:
            low = mid
        else:
            high = mid
    return low, reads


def _validate(args: dict[str, Any]) -> tuple[int, int] | None:
    required = set(INPUT_SCHEMA["required"])
    if (not isinstance(args, dict) or set(args) - set(INPUT_SCHEMA["properties"])
            or not required <= set(args)):
        raise _error("Required pool_history arguments missing; endpoint overrides forbidden", kind="schema_validation")
    rpc_provider = args.get("rpc_provider", "publicnode")
    if not isinstance(rpc_provider, str) or rpc_provider not in RPC_ORIGINS:
        raise _error("rpc_provider must be publicnode, drpc or tenderly; custom URLs forbidden", kind="schema_validation")
    window_keys = set(args) - required - {"rpc_provider"}
    if window_keys not in ({"start_block", "end_block"}, {"start", "end"}):
        raise _error("Provide either paired UTC dates or paired block bounds, never both", kind="schema_validation")
    for key in set(args) - {"pool_address", "start", "end", "rpc_provider"}:
        schema = INPUT_SCHEMA["properties"][key]
        value = args[key]
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise _error("Invalid integer bound: " + key, kind="schema_validation")
    try:
        _address(args["pool_address"])
    except DataApiError:
        raise _error("Invalid pool_address", kind="schema_validation") from None
    if args["tick_lower"] >= args["tick_upper"]:
        raise _error("Reversed tick bounds", kind="schema_validation")
    if "start" in args:
        start, end = _utc(args["start"]), _utc(args["end"])
        if start > end:
            raise _error("Reversed UTC window", kind="schema_validation")
        return start, end
    return None


def _blocks(args: dict[str, Any]) -> list[int]:
    start, end, step = (args[key] for key in ("start_block", "end_block", "step_blocks"))
    if start > end or args["tick_lower"] >= args["tick_upper"]:
        raise _error("Reversed block or tick bounds", kind="schema_validation")
    count = (end - start) // step + 1 + bool((end - start) % step)
    if count > args["max_samples"] or 2 + 17 * count > MAX_REQUESTS:
        maximum = args["max_samples"]
        delta = end - start
        minimum = (max(1, (delta + maximum - 2) // (maximum - 1))
                   if maximum > 1 else (1 if delta == 0 else None))
        message = (f"Requested window requires {count} samples including both endpoints; "
                   f"max_samples allows {maximum}. Requested step_blocks is {step}. ")
        message += (f"The minimum step_blocks to fit max_samples is {minimum}. " if minimum is not None
                    else "No step_blocks can fit both endpoints with max_samples=1. ")
        if 2 + 17 * count > MAX_REQUESTS:
            message += (f"This request also requires {2 + 17 * count} RPC requests; "
                        f"the request budget allows {MAX_REQUESTS}. A sample-only correction may not fit it. ")
        message += ("Preserve the requested pool, window and tick bounds; no request was changed. "
                    "Do not retry unchanged or raise budgets; any step change requires explicit caller review.")
        raise DataApiError(message, kind="schema_validation", retryable=False, detail={
            "provider": "uniswap", "action": "pool_history",
            "reason": "historical_sample_budget_exceeded",
            "required_samples": count, "max_samples": maximum,
            "resolved_start_block": start, "resolved_end_block": end, "step_blocks": step,
            "minimum_step_blocks_to_fit": minimum,
            "requested_window": {key: args[key] for key in (
                ("start", "end", "step_blocks") if "start" in args
                else ("start_block", "end_block", "step_blocks"))},
            "required_requests": 2 + 17 * count, "request_budget": MAX_REQUESTS,
            "coverage_complete": False, "safe_to_retry": False,
        })
    blocks = list(range(start, end + 1, step))
    if blocks[-1] != end:
        blocks.append(end)
    return blocks


class UniswapHistoryProvider:
    def __init__(self, http: HttpTransport | None = None, *,
                 catalog_http: HttpTransport | None = None) -> None:
        self._owns_http = http is None
        self.http = http if http is not None else UrllibHttp(
            max_response_bytes=MAX_RESPONSE_BYTES, allowed_origins=tuple(RPC_ORIGINS.values()),
            strict_json=True, rate_limit_per_sec=0,
        )
        self.catalog_http = catalog_http if catalog_http is not None else UrllibHttp(
            max_response_bytes=MAX_CATALOG_BYTES, allowed_origins=(CATALOG_URL,),
            strict_json=True, rate_limit_per_sec=0,
        )

    def _token_catalog(self, pool: dict[str, Any], deadline: float, *,
                       cancel_token: Any | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raise_if_cancelled(cancel_token)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _error("Token catalog shared deadline exhausted")
        try:
            status, doc = self.catalog_http.request("GET", CATALOG_URL,
                headers={"Accept": "application/json"}, timeout=min(TIMEOUT_SECONDS, remaining))
        except CancelledError:
            raise
        except Exception:
            raise_if_cancelled(cancel_token)
            raise _error("Token catalog transport failed") from None
        raise_if_cancelled(cancel_token)
        if time.monotonic() > deadline:
            raise _error("Token catalog shared deadline exhausted")
        if status != 200 or not isinstance(doc, dict):
            raise _error("Token catalog unavailable or invalid")
        canonical = _canonical(doc)
        if len(canonical) > MAX_CATALOG_BYTES:
            raise _error("Token catalog response exceeds byte limit")
        name, version, stamp, tokens = (doc.get(key) for key in ("name", "version", "timestamp", "tokens"))
        if (not isinstance(name, str) or not 1 <= len(name) <= 128 or not name.isprintable()
                or not isinstance(version, dict) or set(version) != {"major", "minor", "patch"}
                or any(type(v) is not int or not 0 <= v <= 2**31 - 1 for v in version.values())
                or not isinstance(stamp, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,9})?(?:Z|[+]00:00)", stamp)
                or not isinstance(tokens, list) or not tokens):
            raise _error("Token catalog metadata invalid")
        try:
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            raise _error("Token catalog timestamp invalid") from None
        source_hash = hashlib.sha256(canonical).hexdigest()
        source = {"url": CATALOG_URL, "sha256": source_hash, "retrieved_at": int(time.time()),
                  "hash_basis": "canonical_parsed_json_utf8_sorted_keys", "request_count": 1,
                  "catalog_name": name, "catalog_version": dict(version), "catalog_timestamp": stamp,
                  "identity_scope": "current_official_catalog"}
        # Current catalog ambiguity must not erase valid historical pool observations.
        by_address: dict[str, dict[str, Any]] = {}
        symbol_addresses: dict[str, set[str]] = {}
        for token in tokens:
            if not isinstance(token, dict) or type(token.get("chainId")) is not int:
                return [], source
            if token["chainId"] != pool["chain_id"]:
                continue
            try:
                address = _address(token.get("address"))
            except DataApiError:
                return [], source
            if address in by_address:
                return [], source
            by_address[address] = token
            symbol = token.get("symbol")
            if isinstance(symbol, str):
                symbol_addresses.setdefault(symbol.upper(), set()).add(address)
        identities = []
        for index in (0, 1):
            address = pool["token" + str(index)]
            token = by_address.get(address, {})
            symbol, decimals = token.get("symbol"), token.get("decimals")
            if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", symbol)
                    or len(symbol_addresses.get(symbol.upper(), ())) != 1
                    or type(decimals) is not int or not 0 <= decimals <= 255
                    or decimals != pool["decimals"][index]):
                return [], source
            identities.append({"chain_id": pool["chain_id"], "address": address,
                               "symbol": symbol, "decimals": decimals,
                               "catalog_source_sha256": source_hash,
                               "identity_scope": "current_official_catalog"})
        if time.monotonic() > deadline:
            raise _error("Token catalog shared deadline exhausted")
        return identities, source

    def pool_history(self, context: DataApiContext, args: dict[str, Any]) -> dict[str, Any]:
        token = context.cancel_token if issubclass(type(context), DataApiContext) else None
        if not self._owns_http:
            return self._pool_history(context, args, self.http)
        deadline = time.monotonic() + DEADLINE_SECONDS
        with ExitStack() as stack:
            # Each lane owns one cookie-free connection; no transport is shared
            # by concurrent requests. Executors close before their transports.
            transports = [stack.enter_context(KeepAliveHttp(max_response_bytes=MAX_RESPONSE_BYTES,
                allowed_origins=tuple(RPC_ORIGINS.values()), strict_json=True,
                rate_limit_per_sec=RPC_RATE_LIMIT_PER_SEC,
                deadline=deadline, cancel_token=token)) for _ in range(2)]
            lanes = [(stack.enter_context(ThreadPoolExecutor(max_workers=1)), transport)
                     for transport in transports]
            return self._pool_history(context, args, transports[0], lanes)

    def _pool_history(self, context, args, http, lanes=None):
        cancel_token = context.cancel_token if issubclass(type(context), DataApiContext) else None
        raise_if_cancelled(cancel_token)
        window = _validate(args)
        blocks = _blocks(args) if window is None else None
        rpc_provider = args.get("rpc_provider", "publicnode")
        rpc_url = RPC_ORIGINS[rpc_provider]
        request_args = {**args, "rpc_provider": rpc_provider}
        # Keep catalog discovery usable without the optional trading dependencies.
        try:
            from eth_abi import decode
            from ..financial.defi.calls import calldata
        except ImportError:
            raise _error("Historical pool reads require the trading ABI dependencies") from None
        pool = _address(args["pool_address"])
        lower, upper = args["tick_lower"], args["tick_upper"]
        deadline = http.deadline if isinstance(http, KeepAliveHttp) else time.monotonic() + DEADLINE_SECONDS
        sources: list[dict[str, Any]] = []
        transcript = hashlib.sha256()
        request_count = 0

        # ponytail: drpc's public free plan caps batches at three; use that
        # bound for all fixed origins, with no retries or automatic fallback.
        def rpc_many(calls: list[tuple[RpcMethod, list[Any]]]) -> list[Any]:
            nonlocal request_count
            if not 1 <= len(calls) <= 10 or request_count + len(calls) > MAX_REQUESTS:
                return rpc_chunk(calls)  # Existing bounded-input/budget diagnostic, no send.
            if lanes is None or len(calls) <= 3:
                if len(calls) > 3:
                    return [result for offset in range(0, len(calls), 3)
                            for result in rpc_many(calls[offset:offset + 3])]
                return rpc_chunk(calls)
            raise_if_cancelled(cancel_token)
            requests = [{"jsonrpc": "2.0", "id": len(sources) + i + 1,
                         "method": name, "params": params}
                        for i, (name, params) in enumerate(calls)]
            results = []
            for offset in range(0, len(calls), 6):
                raise_if_cancelled(cancel_token)
                if request_count + min(6, len(calls) - offset) > MAX_REQUESTS:
                    return rpc_chunk(calls[offset:offset + 6])  # Budget diagnostic before next wave.
                wave = []
                for lane, start in enumerate(range(offset, min(offset + 6, len(calls)), 3)):
                    raise_if_cancelled(cancel_token)
                    chunk = requests[start:start + 3]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return rpc_chunk(calls[start:start + 3])  # Raises deadline before sending.
                    request_count += len(chunk)
                    executor, transport = lanes[lane]
                    future = executor.submit(transport.request, "POST", rpc_url,
                        headers={"Accept": "application/json"},
                        body=chunk[0] if len(chunk) == 1 else chunk,
                        timeout=min(TIMEOUT_SECONDS, remaining))
                    wave.append((start, chunk, future))
                wait([future for _, _, future in wave])
                for start, chunk, future in wave:
                    results.extend(rpc_chunk(calls[start:start + 3], chunk, future))
            return results

        def rpc_chunk(calls, requests=None, future=None):
            nonlocal request_count
            raise_if_cancelled(cancel_token)
            if not 1 <= len(calls) <= 10:
                raise _error("Historical RPC batch size out of bounds")
            method = calls[0][0]

            def failure(stage: RpcFailureStage, *, http_status: int | None = None,
                        rpc_error_code: int | None = None,
                        reason: str | None = None) -> DataApiError:
                # Only bounded observations cross the error boundary, never provider text.
                detail = {"provider": "uniswap", "action": "pool_history",
                          "reason": reason or "historical_rpc_evidence_failure",
                          "failure_stage": stage, "rpc_provider": rpc_provider,
                          "rpc_method": method if method in RPC_METHODS else "unknown",
                          "request_count": request_count, "coverage_complete": False,
                          "safe_to_retry": False}
                if type(http_status) is int and 100 <= http_status <= 599:
                    detail["http_status"] = http_status
                if type(rpc_error_code) is int and -(2**31) <= rpc_error_code < 2**31:
                    detail["rpc_error_code"] = rpc_error_code
                return DataApiError("Historical RPC evidence failure: " + stage,
                                    retryable=False, detail=detail)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure("deadline")
            if future is None and request_count + len(calls) > MAX_REQUESTS:
                raise failure("request_budget")
            requests = requests or [{"jsonrpc": "2.0", "id": len(sources) + i + 1,
                         "method": name, "params": params}
                        for i, (name, params) in enumerate(calls)]
            body = requests[0] if len(requests) == 1 else requests
            if future is None:
                request_count += len(requests)
            try:
                status, doc = future.result() if future is not None else http.request("POST", rpc_url,
                    headers={"Accept": "application/json"}, body=body,
                    timeout=min(TIMEOUT_SECONDS, remaining))
            except CancelledError:
                raise
            except json.JSONDecodeError:
                raise_if_cancelled(cancel_token)
                raise failure("response_envelope") from None
            except Exception:
                raise_if_cancelled(cancel_token)
                # Transport wrappers may hide HTTP/parse details; do not guess from text.
                raise failure("deadline" if time.monotonic() > deadline else "transport") from None
            raise_if_cancelled(cancel_token)
            if time.monotonic() > deadline:
                raise failure("deadline")
            if type(status) is int and status in {401, 403}:
                raise DataApiError(
                    'Historical RPC access was denied (HTTP %s); no historical state was accepted. '
                    'This fixed pool_history action accepts only its supported public RPC providers and '
                    'has no arbitrary API-key, vault, custom-URL, or endpoint configuration. '
                    'Retrying the same request requires changed preconditions. You may autonomously select '
                    'another supported, verified public read-only source while preserving the candidate, '
                    'requested pool/window and evidence requirements. Do not seek credentials, bypass '
                    'access denial or trade.'
                    % status,
                    kind='configuration_required', retryable=False,
                    detail={**failure("http_status", http_status=status).detail,
                            'reason':'historical_rpc_authorization_required',
                            'access_status':'denied',
                            'archive_access_established':False,
                            'arbitrary_credentials_supported':False,
                            'custom_endpoint_supported':False})
            if type(status) is not int or not 100 <= status <= 599:
                raise failure("response_envelope")
            if status == 429:
                raise failure("http_status", http_status=status,
                              reason="historical_rpc_rate_limited")
            if status != 200:
                raise failure("http_status", http_status=status)
            try:
                canonical = _canonical(doc)
            except DataApiError:
                raise failure("response_envelope") from None
            if len(canonical) > MAX_RESPONSE_BYTES:
                raise failure("response_size")
            documents = [doc] if len(requests) == 1 else doc
            if not isinstance(documents, list) or len(documents) != len(requests):
                raise failure("response_envelope")
            by_id = {}
            for document in documents:
                if (not isinstance(document, dict) or document.get("jsonrpc") != "2.0"
                        or type(document.get("id")) is not int or document["id"] in by_id):
                    raise failure("response_envelope")
                by_id[document["id"]] = document
            if set(by_id) != {request["id"] for request in requests}:
                raise failure("response_envelope")
            # Validate the entire batch before accepting any evidence. JSON-RPC
            # permits reordered responses; hashes follow the original request IDs.
            ordered = [by_id[request["id"]] for request in requests]
            for request, document in zip(requests, ordered):
                raise_if_cancelled(cancel_token)
                method = request["method"]
                if "error" in document:
                    error = document["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    if "result" in document or type(code) is not int or not -(2**31) <= code < 2**31:
                        raise failure("response_envelope")
                    raise failure("rpc_error", rpc_error_code=code)
                if document.get("result") is None:
                    raise failure("missing_result")
            for request, document in zip(requests, ordered):
                transcript.update(_canonical({"request": request, "response": document}) + b"\n")
                sources.append({"url": rpc_url, "request": request, "sha256": _hash(document),
                                "request_sha256": _hash(request), "retrieved_at": int(time.time()),
                                "hash_basis": "canonical_parsed_json_utf8_sorted_keys"})
            return [document["result"] for document in ordered]

        def rpc(method: RpcMethod, params: list[Any]) -> Any:
            return rpc_many([(method, params)])[0]

        def call_request(target: str, signature: str, block: int,
                         types: list[str] | None = None, values: list[Any] | None = None) -> tuple[RpcMethod, list[Any]]:
            return ("eth_call", [{"to": target, "data": calldata(signature, types or [], values or [])}, hex(block)])

        def decode_call(raw: Any, result_types: list[str]) -> tuple[Any, ...]:
            if (not isinstance(raw, str) or len(raw) != 2 + 64 * len(result_types)
                    or not re.fullmatch(r"0x[0-9a-fA-F]+", raw)):
                raise _error("Missing or malformed historical contract state")
            try:
                return decode(result_types, bytes.fromhex(raw[2:]))
            except Exception:
                raise _error("Historical contract state ABI decode failed") from None

        def parse_header(doc: Any, block: int) -> dict[str, Any]:
            if (not isinstance(doc, dict) or _quantity(doc.get("number")) != block
                    or not isinstance(doc.get("hash"), str)
                    or not re.fullmatch(r"0x[0-9a-fA-F]{64}", doc["hash"]) or int(doc["hash"], 16) == 0):
                raise _error("Missing or mismatched historical block identity")
            stamp = _quantity(doc.get("timestamp"))
            if stamp > int(time.time()):
                raise _error("Future block timestamp forbidden")
            return {"block_number": block, "block_hash": doc["hash"].lower(), "timestamp": stamp}

        def header(block: int) -> dict[str, Any]:
            return parse_header(rpc("eth_getBlockByNumber", [hex(block), False]), block)

        if _quantity(rpc("eth_chainId", [])) != 1:
            raise _error("RPC chain mismatch; only Ethereum chain 1 is supported")
        head = _quantity(rpc("eth_blockNumber", []))
        resolution: dict[str, Any] = {}
        resolved_headers: dict[int, dict[str, Any]] = {}
        if window is not None:
            start_block, start_reads = floor_block_at(window[0], head, header, resolved_headers)
            end_block, end_reads = floor_block_at(window[1], head, header, resolved_headers)
            args = {**args, "start_block": start_block, "end_block": end_block}
            blocks = _blocks(args)
            if request_count + 17 * len(blocks) > MAX_REQUESTS:
                raise _error("Resolved window exceeds shared request budget")
            resolution = {
                "policy": "floor_block_timestamp_lte_requested_utc",
                "start_resolver_reads": start_reads, "end_resolver_reads": end_reads,
                "start_block": start_block, "end_block": end_block,
                "start_timestamp": resolved_headers[start_block]["timestamp"],
                "end_timestamp": resolved_headers[end_block]["timestamp"],
                "start_gap_seconds": window[0] - resolved_headers[start_block]["timestamp"],
                "end_gap_seconds": window[1] - resolved_headers[end_block]["timestamp"],
                "exact_start": window[0] == resolved_headers[start_block]["timestamp"],
                "exact_end": window[1] == resolved_headers[end_block]["timestamp"],
                "start_sample_may_precede_requested_window": True,
            }
        assert blocks is not None
        if blocks[-1] > head:
            raise _error("Future blocks forbidden", kind="schema_validation")
        rows: list[dict[str, Any]] = []
        identity: dict[str, Any] | None = None
        for block in blocks:
            first_source = len(sources)
            initial = rpc_many([("eth_getBlockByNumber", [hex(block), False]),
                *[call_request(pool, name + "()", block)
                  for name in ("token0", "token1", "factory", "fee", "tickSpacing")]])
            row = parse_header(initial[0], block)
            if block in resolved_headers and row != resolved_headers[block]:
                raise _error("Resolved historical block reorganized before sampling")
            if rows and row["timestamp"] <= rows[-1]["timestamp"]:
                raise _error("Historical block timestamps are not chronological")
            token0, token1 = [_address(decode_call(raw, ["address"])[0]) for raw in initial[1:3]]
            if int(token0, 16) >= int(token1, 16):
                raise _error("Invalid pool token identity/order")
            factory = _address(decode_call(initial[3], ["address"])[0])
            if factory != FACTORY:
                raise _error("Pool is not from the Ethereum Uniswap v3 factory")
            fee = decode_call(initial[4], ["uint24"])[0]
            spacing = decode_call(initial[5], ["int24"])[0]
            if not 0 < fee < 1_000_000 or not 0 < spacing <= 32767:
                raise _error("Invalid pool fee/tick spacing")
            if lower % spacing or upper % spacing:
                raise _error("Tick bounds must align to verified pool spacing", kind="schema_validation")
            state = rpc_many([
                *[call_request(token, "decimals()", block) for token in (token0, token1)],
                call_request(factory, "getPool(address,address,uint24)", block,
                             ["address", "address", "uint24"], [token0, token1, fee]),
                call_request(factory, "feeAmountTickSpacing(uint24)", block, ["uint24"], [fee]),
                *[call_request(pool, signature, block) for signature in
                  ("slot0()", "liquidity()", "feeGrowthGlobal0X128()", "feeGrowthGlobal1X128()")],
                *[call_request(pool, "ticks(int24)", block, ["int24"], [bound])
                  for bound in (lower, upper)],
            ])
            decimals = [decode_call(raw, ["uint8"])[0] for raw in state[:2]]
            verified = _address(decode_call(state[2], ["address"])[0])
            if verified != pool or decode_call(state[3], ["int24"])[0] != spacing:
                raise _error("Factory pool identity or fee spacing mismatch")
            current_identity = {"chain_id": 1, "pool_address": pool, "factory": factory,
                                "token0": token0, "token1": token1, "decimals": decimals,
                                "fee": fee, "tick_spacing": spacing}
            if identity is not None and identity != current_identity:
                raise _error("Historical pool identity changed")
            identity = current_identity
            sqrt, tick, *_ = decode_call(state[4], SLOT_TYPES)
            if not 4295128739 <= sqrt < 1461446703485210103287273052203988822378723970342 or not -887272 <= tick <= 887272:
                raise _error("Pool is uninitialized or slot0 is invalid")
            row.update({**identity, "sqrt_price_x96": str(sqrt), "tick": tick,
                        "liquidity": str(decode_call(state[5], ["uint128"])[0]),
                        "tick_lower": lower, "tick_upper": upper})
            for index in (0, 1):
                row[f"fee_growth_global{index}_x128"] = str(decode_call(state[6 + index], ["uint256"])[0])
            for side, raw in zip(("lower", "upper"), state[8:]):
                tick_state = decode_call(raw, TICK_TYPES)
                if tick_state[7] != (tick_state[0] > 0) or (not tick_state[7] and any(tick_state[:7])) or abs(tick_state[1]) > tick_state[0]:
                    raise _error("Inconsistent historical tick state")
                row[f"tick_{side}_initialized"] = tick_state[7]
                for index in (0, 1):
                    row[f"tick_{side}_fee_growth_outside{index}_x128"] = str(tick_state[2 + index])
            row["source_response_sha256"] = [s["sha256"] for s in sources[first_source:]]
            rows.append(row)
        # Numeric block tags are mandatory; reject a reorg instead of mixing hashes.
        # Rechecks are independent, so they share the same bounded batch waves.
        recheck_start = len(sources)
        recheck_results = []
        for offset in range(0, len(rows), 6):
            group = rows[offset:offset + 6]
            recheck_results.extend(
                rpc_many([("eth_getBlockByNumber", [hex(row["block_number"]), False]) for row in group]))
        for index, row in enumerate(rows):
            if parse_header(recheck_results[index], row["block_number"]) != {
                    key: row[key] for key in ("block_number", "block_hash", "timestamp")}:
                raise _error("Historical block reorganized during sampling")
            row["source_response_sha256"].append(sources[recheck_start + index]["sha256"])
            row["row_sha256"] = _hash(row)
        assert identity is not None
        token_identities, catalog_source = self._token_catalog(identity, deadline, cancel_token=cancel_token)
        if time.monotonic() > deadline:
            raise _error("Token catalog shared deadline exhausted")
        return {"rows": rows, "source_url": rpc_url, "_envelope": {
            "provider": "uniswap", "action": "pool_history", "row_contract": "uniswap_v3_pool_history.v1",
            "rpc_provider": rpc_provider, "request_args": request_args,
            "mode": "public_read_only", "mock": False, "chain": "Ethereum", "chain_id": 1,
            "observation_kind": "historical_lp_pool_state",
            "pool": identity, "timestamp_unit": "unix_seconds_utc",
            "token_identities": token_identities,
            "pool_identity_scope": ("historical_onchain_pool_with_current_token_catalog" if token_identities
                                    else "historical_onchain_pool_token_identity_unknown"),
            "token_identity_status": "verified" if token_identities else "unknown",
            "token_identity_time_scope": "current_catalog_not_historical_token_metadata",
            "sources": [{"url": rpc_url, "retrieved_at": int(time.time()), "sha256": transcript.hexdigest(),
                         "hash_basis": "ordered_canonical_json_request_response_pairs_utf8_sorted_keys_newline_framed",
                         "request_count": len(sources)}, catalog_source],
            "rpc_response_hashes": [{"url": s["url"], "request": s["request"], "request_sha256": s["request_sha256"],
                                     "response_sha256": s["sha256"]} for s in sources],
            "requested_window": ({key: args[key] for key in ("start", "end", "step_blocks")} if window is not None
                                 else {key: args[key] for key in ("start_block", "end_block", "step_blocks")}),
            "window_resolution": resolution,
            "sampled_blocks": blocks, "endpoint_policy": "inclusive_end_once",
            "sampled_intervals": [{"start_block": a["block_number"], "end_block": b["block_number"],
                                   "start_block_hash": a["block_hash"], "end_block_hash": b["block_hash"],
                                   "start_timestamp": a["timestamp"], "end_timestamp": b["timestamp"],
                                   "tick_path_observed": False} for a, b in zip(rows, rows[1:])],
            "coverage": {"observations": len(rows), "completeness": "not_asserted",
                         "requested_window_complete": False, "sampled_states_complete": True},
            "limitations": LIMITATIONS, "canonicality": "single_rpc_block_hash_rechecked_not_finality_proof",
            "source_row_count": len(rows), "matched_row_count": len(rows), "returned_row_count": len(rows),
            "provider_truncated": False, "truncated": False, "provider_limit": args["max_samples"],
            "request_count": request_count, "request_budget": MAX_REQUESTS, "deadline_seconds": DEADLINE_SECONDS,
            "catalog_request_count": 1, "catalog_request_budget": 1,
            "total_request_count": request_count + 1, "total_request_budget": MAX_REQUESTS + 1,
            "rows_sha256": _hash(rows), "returned_rows_sha256": _hash(rows),
            "rows_hash_scope": "all_sampled_normalized_rows_including_row_sha256",
            "returned_rows_hash_scope": "provider_rows_before_data_api_limit_and_column_projection",
            "row_hash_scope": "canonical_row_excluding_row_sha256",
        }}
