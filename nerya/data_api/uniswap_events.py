"""Bounded v3 event evidence; not tick/fee reconstruction or replay input."""
from __future__ import annotations

import re
import time
from copy import deepcopy

from eth_abi import decode, encode
from eth_utils import keccak
from httpx import NetworkError, TimeoutException

from ..agent.provider_errors import is_transient_error
from ..connectors.http import HttpTransport, KeepAliveHttp
from ..core.error_classifier import classify
from ..core.errors import LLMError
from ..core.redaction import redact_text
from ..financial.defi.calls import calldata
from ..harness.cancellation import CancelledError, raise_if_cancelled
from ..llm.retry import is_retryable_stream_transport_error
from .types import DataActionSpec, DataApiContext, DataApiError
from .uniswap_history import (FACTORY, RPC_ORIGINS, SLOT_TYPES, TICK_TYPES, TIMEOUT_SECONDS,
                              MAX_RESOLVER_READS, _canonical, _hash, _utc, floor_block_at)

MAX_BLOCKS = 100
MAX_LOGS = 2000
MAX_RESPONSE_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 4_000_000
MAX_REQUESTS = 135
MAX_TIMESTAMP_REQUESTS = MAX_REQUESTS + MAX_RESOLVER_READS + 1
DEADLINE_SECONDS = 120.0
DRPC_NONRETRYABLE_CODES = {4, 32, 9, 10, 53, 52}  # Documented auth/quota failures.
PERMANENT_RPC_MESSAGE = re.compile(r"auth|forbidden|denied|invalid|malformed|reorg|identity|source.?chang|"
    r"unsupported|not supported|not allowed|quota|billing|not found|missing trie|revert|"
    r"credential|permission|unknown|mismatch|pruned|not enabled|disabled", re.IGNORECASE)
SWAP_TYPES = ["int256", "int256", "uint160", "uint128", "int24"]
SWAP_TOPIC = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
CORE_EVENTS = {
    "Initialize(uint160,int24)": ([], ["uint160", "int24"]),
    "Swap(address,address,int256,int256,uint160,uint128,int24)": (["address", "address"], SWAP_TYPES),
    "Mint(address,address,int24,int24,uint128,uint256,uint256)":
        (["address", "int24", "int24"], ["address", "uint128", "uint256", "uint256"]),
    "Burn(address,int24,int24,uint128,uint256,uint256)":
        (["address", "int24", "int24"], ["uint128", "uint256", "uint256"]),
}
CORE_TOPICS = {"0x" + keccak(text=sig).hex(): (sig.split("(")[0], *types) for sig, types in CORE_EVENTS.items()}
NONCORE_TOPICS = {"0x" + keccak(text=sig).hex() for sig in (
    "Collect(address,address,int24,int24,uint128,uint128)",
    "Flash(address,address,uint256,uint256,uint256,uint256)",
    "IncreaseObservationCardinalityNext(uint16,uint16)",
    "SetFeeProtocol(uint8,uint8,uint8,uint8)", "CollectProtocol(address,address,uint128,uint128)")}
SLOT_FIELDS = ("sqrt_price_x96", "tick", "observation_index", "observation_cardinality",
               "observation_cardinality_next", "fee_protocol", "unlocked")
INPUT_SCHEMA = {"type": "object", "properties": {
    "pool_address": {"type": "string", "pattern": r"^0x[0-9a-fA-F]{40}$"},
    "rpc_provider": {"type": "string", "enum": list(RPC_ORIGINS)},
    "at_utc": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+]00:00)$"},
    **{key: {"type": "integer", "minimum": 1, "maximum": 2**64 - 1}
       for key in ("start_block", "end_block")},
    **{key: {"type": "integer", "minimum": -887272, "maximum": 887272}
       for key in ("tick_lower", "tick_upper")},
}, "required": ["pool_address", "rpc_provider"],
    "allOf": [{"oneOf": [
        {"required": ["start_block", "end_block"], "not": {"required": ["at_utc"]}},
        {"required": ["at_utc"], "not": {"anyOf": [{"required": ["start_block"]}, {"required": ["end_block"]}]}}]}],
    "oneOf": [{"required": ["tick_lower", "tick_upper"]},
              {"not": {"anyOf": [{"required": ["tick_lower"]}, {"required": ["tick_upper"]}]}}],
    "additionalProperties": False}


def uniswap_event_specs(http: HttpTransport | None = None) -> list[DataActionSpec]:
    return [DataActionSpec(
        provider="uniswap", action="pool_event_slice", title="Bounded v3 pool event slice",
        description="Ethereum-1 canonical v3 pool, explicit publicnode/drpc/tenderly; inclusive range <=100 blocks. "
        "Unfiltered pool logs, parent/end state and sequential core price/tick/active-liquidity fold. "
        "Optional paired tick bounds add boundary checkpoints and Mint/Burn continuity verification. "
        "One source/attempt per call; background acquisition can retry explicitly transient reads within its saved limits. "
        "At most 135 RPC requests, 120s, 256KiB/response, one getLogs, <2000 logs. "
        "Alternatively at_utc resolves one successor-block slice with floor proof; at most 168 requests. "
        "Always incomplete for tick/fee reconstruction, full-window replay or trades.",
        input_schema=INPUT_SCHEMA, handler=UniswapEventProvider(http).pool_event_slice,
        tags=("uniswap", "ethereum", "events", "history"),
        docs_url="https://docs.uniswap.org/contracts/v3/reference/core/interfaces/pool/IUniswapV3PoolEvents",
        replay_input=False)]


def _error(message, *, retryable=False, **detail):
    return DataApiError(message, retryable=retryable, detail={
        "provider": "uniswap", "action": "pool_event_slice", "incomplete": True,
        "coverage_complete": False, "safe_to_retry": retryable, **detail})


def _rpc_message(message):
    if not isinstance(message, str):
        return ""
    message = re.sub(r"(?i)\b(?:https?|wss?)://\S+", "[redacted-url]", message)
    message = re.sub(r"(?i)\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
                     "[redacted]", message)
    return " ".join(redact_text(message).split())


def _hex(value, size=None):
    if (not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value)
            or (size is not None and len(value) != 2 + size * 2)):
        raise _error("Invalid evidence hex")
    return value.lower()


def _address(value):
    value = _hex(value, 20)
    if int(value, 16) == 0:
        raise _error("Zero contract address")
    return value


def _quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})", value):
        raise _error("Invalid RPC quantity")
    return int(value, 16)


def _decode_core(row):
    event = CORE_TOPICS.get(row["topics"][0])
    if event is None:
        return None
    name, indexed_types, data_types = event
    try:
        raw = bytes.fromhex(row["data"][2:])
        values = decode(data_types, raw)
        if len(row["topics"]) != len(indexed_types) + 1 or encode(data_types, values) != raw:
            raise ValueError()
        indexed = []
        for kind, topic in zip(indexed_types, row["topics"][1:]):
            word = bytes.fromhex(topic[2:])
            value = decode([kind], word)[0]
            if encode([kind], [value]) != word:
                raise ValueError()
            indexed.append(value)
    except Exception:
        raise _error("Invalid core event ABI", event=name) from None
    return name, indexed, values


def _fold_core(rows, initial, terminal):
    state = {"sqrt_price_x96": int(initial["slot0"]["sqrt_price_x96"]),
             "tick": initial["slot0"]["tick"], "liquidity": int(initial["liquidity"])}
    unknown = []
    for row in rows:
        event = _decode_core(row)
        if event is None:
            if row["topics"][0] not in NONCORE_TOPICS:
                unknown.append({"block_number": row["block_number"],
                                "log_index": row["log_index"], "topic": row["topics"][0]})
            continue
        name, indexed, values = event
        if unknown:
            continue  # Unknown state transitions prevent reliable subsequent arithmetic.
        if name == "Initialize":
            if state["sqrt_price_x96"] != 0:
                raise _error("Initialize on already initialized pool")
            state.update(sqrt_price_x96=values[0], tick=values[1])
        elif name == "Swap":
            state.update(sqrt_price_x96=values[2], liquidity=values[3], tick=values[4])
        else:
            lower, upper = indexed[1:]
            if not -887272 <= lower < upper <= 887272 or any(t % initial["pool"]["tick_spacing"] for t in (lower, upper)):
                raise _error("Invalid Mint/Burn tick range")
            amount = values[1] if name == "Mint" else values[0]
            if lower <= state["tick"] < upper:
                state["liquidity"] += amount if name == "Mint" else -amount
        if (not 0 <= state["liquidity"] < 2**128
                or not 4295128739 <= state["sqrt_price_x96"] < 1461446703485210103287273052203988822378723970342
                or not -887272 <= state["tick"] <= 887272):
            raise _error("Core event fold outside pool bounds")
    expected = {**state, "sqrt_price_x96": str(state["sqrt_price_x96"]), "liquidity": str(state["liquidity"])}
    actual = {**{k: terminal["slot0"][k] for k in ("sqrt_price_x96", "tick")}, "liquidity": terminal["liquidity"]}
    matches = {k: None if unknown else expected[k] == actual[k] for k in expected}
    return {"method": "sequential_core_event_fold", "expected": expected, "observed": actual,
            "fields_match": matches, "price_tick_match": None if unknown else all(matches[k] for k in ("sqrt_price_x96", "tick")),
            "liquidity_match": matches["liquidity"], "checkpoint_match": None if unknown else all(matches.values()),
            "unknown_events": unknown, "core_reconstruction_verified": not unknown and all(matches.values())}


def _fold_range(rows, initial, terminal):
    """Verify only the two boundary lifecycles, never reconstruct fee growth."""
    events = [event for row in rows if (event := _decode_core(row)) is not None]
    reasons = (["unknown_pool_event"] if any(row["topics"][0] not in CORE_TOPICS
               and row["topics"][0] not in NONCORE_TOPICS for row in rows) else [])
    if any(name == "Initialize" for name, _, _ in events):
        reasons.append("pool_initialized")
    result = {}
    for side in ("lower", "upper"):
        tick = initial["tick_" + side]
        before, after = initial[side], terminal[side]
        gross, net = int(before["liquidity_gross"]), int(before["liquidity_net"])
        minimum, failures = gross, []
        if not before["initialized"]:
            failures.append("initial_boundary_uninitialized")
        for name, indexed, values in events:
            if name not in ("Mint", "Burn") or tick not in indexed[1:]:
                continue
            lower, upper = indexed[1:]
            if not -887272 <= lower < upper <= 887272:
                failures.append("invalid_event_range")
                continue
            amount = values[1] if name == "Mint" else values[0]
            delta = amount if name == "Mint" else -amount
            previous = gross
            gross += delta
            net += delta if tick == lower else -delta
            minimum = min(minimum, gross)
            if not 0 <= gross < 2**128 or not -2**127 <= net < 2**127 or abs(net) > gross:
                failures.append("invalid_boundary_liquidity")
            if amount and previous > 0 and gross == 0:
                failures.append("boundary_cleared")
            if amount and previous == 0 and gross > 0:
                failures.append("boundary_reinitialized")
        match = (gross == int(after["liquidity_gross"]) and net == int(after["liquidity_net"])
                 and after["initialized"] == (gross > 0))
        if not match:
            failures.append("terminal_boundary_mismatch")
        if not after["initialized"]:
            failures.append("terminal_boundary_uninitialized")
        failures = sorted(set(failures))
        result[side] = {"tick": tick, "reason_codes": failures,
            "initial_liquidity_gross": before["liquidity_gross"], "initial_liquidity_net": before["liquidity_net"],
            "expected_liquidity_gross": str(gross), "expected_liquidity_net": str(net),
            "minimum_liquidity_gross": str(minimum), "terminal_match": match}
        reasons.extend(side + ":" + reason for reason in failures)
    return {"verified": not reasons, "reason_codes": reasons, **result}


class UniswapEventProvider:
    def __init__(self, http: HttpTransport | None = None):
        self.http = http

    def pool_event_slice(self, context: DataApiContext, args: dict, *, anchor=None):
        deadline = time.monotonic() + DEADLINE_SECONDS
        if self.http is not None:
            return self._pool_event_slice(context, args, self.http, deadline, anchor)
        with KeepAliveHttp(deadline=deadline, cancel_token=context.cancel_token,
                max_response_bytes=MAX_RESPONSE_BYTES, allowed_origins=tuple(RPC_ORIGINS.values()),
                strict_json=True, rate_limit_per_sec=0) as http:
            return self._pool_event_slice(context, args, http, deadline, anchor)

    def _pool_event_slice(self, context, args, http, deadline, anchor):
        if (not isinstance(args, dict) or not set(INPUT_SCHEMA["required"]) <= args.keys()
                or args.keys() - INPUT_SCHEMA["properties"].keys()
                or not isinstance(args["rpc_provider"], str) or args["rpc_provider"] not in RPC_ORIGINS):
            raise _error("Exact pool, block bounds and explicit fixed rpc_provider required")
        ranged = "tick_lower" in args or "tick_upper" in args
        if ranged and (any(type(args.get(key)) is not int for key in ("tick_lower", "tick_upper"))
                       or not -887272 <= args["tick_lower"] < args["tick_upper"] <= 887272):
            raise _error("Paired integer tick bounds required within v3 range")
        request_budget = MAX_REQUESTS if ranged else MAX_REQUESTS - 4
        original_args, target = deepcopy(args), None
        timed = "at_utc" in args
        start, end = args.get("start_block"), args.get("end_block")
        if timed:
            if ({"start_block", "end_block"} & args.keys() or not isinstance(args["at_utc"], str)
                    or not re.fullmatch(INPUT_SCHEMA["properties"]["at_utc"]["pattern"], args["at_utc"])):
                raise _error("Use either paired block bounds or exact UTC at_utc")
            try:
                target = _utc(args["at_utc"])
            except DataApiError as exc:
                raise _error(exc.message) from None
            request_budget += MAX_RESOLVER_READS + 1
        elif (type(start) is not int or type(end) is not int
              or not 1 <= start <= end < 2**64 or end - start + 1 > MAX_BLOCKS):
            raise _error("Inclusive block range must contain 1 to 100 blocks")
        if anchor is not None:
            if (not timed or not isinstance(anchor, dict) or set(anchor) != {"block_number", "block_hash",
                    "block_timestamp", "next_block_timestamp"}
                    or any(type(anchor[k]) is not int for k in ("block_number", "block_timestamp", "next_block_timestamp"))
                    or not 0 <= anchor["block_number"] < 2**64 - 1
                    or not 0 <= anchor["block_timestamp"] <= target
                    or not anchor["block_timestamp"] < anchor["next_block_timestamp"] <= 253402300799):
                raise _error("Invalid observation resolver anchor")
            anchor = {**anchor, "block_hash": _hex(anchor["block_hash"], 32)}
        pool = _address(args["pool_address"])
        url = RPC_ORIGINS[args["rpc_provider"]]
        sources = []
        response_bytes = http_request_count = 0

        def rpc_many(calls):
            nonlocal response_bytes, http_request_count
            raise_if_cancelled(context.cancel_token)
            remaining = deadline - time.monotonic()
            if not 1 <= len(calls) <= 3 or remaining <= 0 or len(sources) + len(calls) > request_budget:
                raise _error("Event slice deadline/request budget exhausted", request_count=len(sources))
            requests = [{"jsonrpc": "2.0", "id": len(sources) + i + 1, "method": method, "params": params}
                        for i, (method, params) in enumerate(calls)]
            method = calls[0][0]
            http_request_count += 1
            try:
                status, doc = http.request("POST", url, headers={"Accept": "application/json"},
                    body=requests[0] if len(requests) == 1 else requests, timeout=min(TIMEOUT_SECONDS, remaining))
            except CancelledError:
                raise
            except Exception as exc:
                raise_if_cancelled(context.cancel_token)
                raise _error("Event slice transport failed", rpc_method=method,
                    request_count=len(sources) + len(calls), failure_stage="transport",
                    transport_error=type(exc).__name__, retryable=is_retryable_stream_transport_error(exc)
                    or isinstance(exc, (NetworkError, TimeoutException))) from None
            raise_if_cancelled(context.cancel_token)
            if time.monotonic() > deadline:
                raise _error("Event slice deadline exhausted", rpc_method=method)
            if status != 200:
                rpc_error = doc.get("error") if isinstance(doc, dict) else None
                code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
                message = _rpc_message(rpc_error.get("message")) if isinstance(rpc_error, dict) else ""
                permanent = (type(code) is int and (code in {-32700, -32600, -32601, -32602}
                    or (args["rpc_provider"] == "drpc" and code in DRPC_NONRETRYABLE_CODES)))
                raise _error("Event slice HTTP failure", http_status=status, rpc_method=method,
                    failure_stage="http_status", retryable=type(status) is int
                    and classify(Exception(), status=status).retryable and not permanent
                    and not bool(PERMANENT_RPC_MESSAGE.search(message)),
                    **({"rpc_error_code": code} if type(code) is int and -(2**31) <= code < 2**31 else {}),
                    **({"rpc_error_message": message[:500]} if message else {}))
            try:
                size = len(_canonical(doc))
            except (ValueError, TypeError, RecursionError, DataApiError):
                raise _error("Invalid RPC JSON") from None
            response_bytes += size
            if size > MAX_RESPONSE_BYTES or response_bytes > MAX_EVIDENCE_BYTES:
                raise _error("Event slice response byte budget exceeded")
            documents = [doc] if len(requests) == 1 else doc
            if not isinstance(documents, list) or len(documents) != len(requests):
                raise _error("Event slice RPC response count mismatch")
            by_id = {}
            for document in documents:
                if (not isinstance(document, dict) or document.get("jsonrpc") != "2.0"
                        or type(document.get("id")) is not int or document["id"] in by_id):
                    raise _error("Invalid or duplicate RPC response ID")
                by_id[document["id"]] = document
            if set(by_id) != {request["id"] for request in requests}:
                raise _error("Event slice RPC response IDs mismatch")
            ordered = [by_id[request["id"]] for request in requests]
            failures = []
            for request, document in zip(requests, ordered):
                if "error" in document or document.get("result") is None:
                    rpc_error = document.get("error")
                    code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
                    raw_message = rpc_error.get("message") if isinstance(rpc_error, dict) else None
                    message = _rpc_message(raw_message)
                    valid = ("result" not in document and type(code) is int and -(2**31) <= code < 2**31
                             and isinstance(raw_message, str))
                    # Reuse the provider compatibility matcher only after permanent RPC failures are excluded.
                    permanent = PERMANENT_RPC_MESSAGE.search(message)
                    # Documented dRPC auth/quota codes override transient wording.
                    permanent_code = type(code) is int and (code in {-32700, -32600, -32601, -32602}
                        or (args["rpc_provider"] == "drpc" and code in DRPC_NONRETRYABLE_CODES))
                    retryable = bool(valid and not permanent_code and not permanent
                                     and is_transient_error(LLMError(message)))
                    failures.append(_error("Event slice RPC failure", rpc_method=request["method"],
                        failure_stage="rpc_error" if valid else "response_envelope", retryable=retryable,
                        **({"rpc_error_code": code} if type(code) is int and -(2**31) <= code < 2**31 else {}),
                        **({"rpc_error_message": message[:500]} if message else {})))
            if failures:
                # One denied/malformed member makes the entire read batch nonretryable.
                raise next((failure for failure in failures if not failure.retryable), failures[0])
            # Admit/hash the entire batch in request order only after validating every message.
            for request, document in zip(requests, ordered):
                sources.append({"request": deepcopy(request), "request_sha256": _hash(request),
                                "response_sha256": _hash(document)})
            return [document["result"] for document in ordered]

        def rpc(method, params):
            return rpc_many([(method, params)])[0]

        def decode_call(raw, signature, types):
            raw = _hex(raw)
            try:
                decoded = decode(types, bytes.fromhex(raw[2:]))
                if encode(types, decoded).hex() != raw[2:]:
                    raise ValueError()
                return decoded
            except Exception:
                raise _error("Invalid checkpoint ABI result", signature=signature) from None

        def call_many(block, specs):
            results = []
            for offset in range(0, len(specs), 3):
                group, calls = specs[offset:offset + 3], []
                for target, signature, types, *parameters in group:
                    inputs, values = parameters or ((), ())
                    calls.append(("eth_call", [{"to": target, "data": calldata(signature, inputs, values)}, hex(block)]))
                results.extend(decode_call(raw, spec[1], spec[2]) for raw, spec in zip(rpc_many(calls), group))
            return results

        if _quantity(rpc("eth_chainId", [])) != 1:
            raise _error("Expected Ethereum chain 1")

        def decode_header(block, raw):
            if not isinstance(raw, dict) or _quantity(raw.get("number")) != block:
                raise _error("Missing historical block")
            return {"number": block, "hash": _hex(raw.get("hash"), 32),
                    "parent_hash": _hex(raw.get("parentHash"), 32),
                    "timestamp": _quantity(raw.get("timestamp"))}

        def read_header(block):
            return decode_header(block, rpc("eth_getBlockByNumber", [hex(block), False]))

        resolved, observation = {}, None
        if timed:
            head = _quantity(rpc("eth_blockNumber", []))
            anchor_block = None
            if anchor is not None:
                anchor_block = anchor["block_number"]
                if anchor_block >= head:
                    raise _error("Resolver anchor must precede head")
                a, b = read_header(anchor_block), read_header(anchor_block + 1)
                if (a["hash"] != anchor["block_hash"] or a["timestamp"] != anchor["block_timestamp"]
                        or b["timestamp"] != anchor["next_block_timestamp"] or b["parent_hash"] != a["hash"]):
                    raise _error("Resolver anchor identity/timestamp/link mismatch")
                resolved.update({anchor_block: a, anchor_block + 1: b})
            try:
                floor, reads = floor_block_at(target, head, read_header, resolved,
                    max_reads=MAX_RESOLVER_READS - (2 if anchor is not None else 0), anchor=anchor_block)
                reads += 2 if anchor is not None else 0
            except DataApiError as exc:
                if exc.detail.get("action") == "pool_event_slice":
                    raise
                raise _error(exc.message) from None
            if floor >= head:
                raise _error("Observation requires a successor after target; at or beyond head timestamp")
            start = end = floor + 1
            args = {k: v for k, v in args.items() if k != "at_utc"}
            args.update(start_block=start, end_block=end)

        headers = []
        for offset in range(start - 1, end + 1, 3):
            blocks = range(offset, min(offset + 3, end + 1))
            raw_headers = rpc_many([("eth_getBlockByNumber", [hex(block), False]) for block in blocks])
            for block, raw in zip(blocks, raw_headers):
                header = decode_header(block, raw)
                if block in resolved and header != resolved[block]:
                    raise _error("Resolved block changed before slice")
                if headers and (header["parent_hash"] != headers[-1]["hash"]
                                or header["timestamp"] <= headers[-1]["timestamp"]):
                    raise _error("Noncanonical block chain")
                headers.append(header)
        if timed:
            before, after = headers
            if not before["timestamp"] <= target < after["timestamp"]:
                raise _error("Observation floor/successor timestamp proof failed")
            observation = {"requested_utc": original_args["at_utc"], "timestamp": target,
                "floor_block": before["number"], "floor_block_hash": before["hash"], "floor_timestamp": before["timestamp"],
                "successor_block": after["number"], "successor_block_hash": after["hash"],
                "successor_timestamp": after["timestamp"], "resolver_reads": reads,
                "resolver_anchor_used": anchor is not None,
                "policy": "floor_timestamp_lte_target_lt_successor"}
            if anchor is not None:
                observation.update(resolver_anchor_block=anchor_block, resolver_anchor_hash=anchor["block_hash"])

        def checkpoint(block):
            base = call_many(block, [(pool, name + "()", ["address"]) for name in ("token0", "token1", "factory")]
                + [(pool, "fee()", ["uint24"]), (pool, "tickSpacing()", ["int24"]), (pool, "slot0()", SLOT_TYPES)])
            tokens = [_address(value[0]) for value in base[:2]]
            factory, fee, spacing = _address(base[2][0]), base[3][0], base[4][0]
            if (factory != FACTORY or int(tokens[0], 16) >= int(tokens[1], 16)
                    or not 0 < fee < 1_000_000 or not 0 < spacing <= 32767):
                raise _error("Invalid v3 pool identity")
            if ranged and any(args[key] % spacing for key in ("tick_lower", "tick_upper")):
                raise _error("Tick bounds must align to actual pool spacing")
            detail = call_many(block, [
                (factory, "getPool(address,address,uint24)", ["address"], ["address", "address", "uint24"], [*tokens, fee]),
                (factory, "feeAmountTickSpacing(uint24)", ["int24"], ["uint24"], [fee]),
                *[(token, "decimals()", ["uint8"]) for token in tokens], (pool, "liquidity()", ["uint128"]),
                *[(pool, f"feeGrowthGlobal{i}X128()", ["uint256"]) for i in (0, 1)],
                *([(pool, "ticks(int24)", TICK_TYPES, ["int24"], [args["tick_" + side]])
                   for side in ("lower", "upper")] if ranged else [])])
            if _address(detail[0][0]) != pool or detail[1][0] != spacing:
                raise _error("Factory pool verification failed")
            identity = {"chain_id": 1, "pool_address": pool, "factory": factory,
                        "token0": tokens[0], "token1": tokens[1], "fee": fee, "tick_spacing": spacing,
                        "decimals": [value[0] for value in detail[2:4]]}
            slot = base[5]
            if not ((slot[0] == 0 and slot[1] == 0) or (4295128739 <= slot[0] < 1461446703485210103287273052203988822378723970342 and -887272 <= slot[1] <= 887272)):
                raise _error("Invalid pool slot0")
            state = {"block_number": block, "block_hash": headers[block - start + 1]["hash"],
                    "pool": identity, "slot0": dict(zip(SLOT_FIELDS, [str(slot[0]), *slot[1:]])),
                    "liquidity": str(detail[4][0]),
                    **{f"fee_growth_global{i}_x128": str(detail[5 + i][0]) for i in (0, 1)}}
            if ranged:
                state["range"] = {key: args[key] for key in ("tick_lower", "tick_upper")}
                for side, bound in zip(("lower", "upper"), detail[7:]):
                    if bound[7] != (bound[0] > 0) or abs(bound[1]) > bound[0] or (not bound[7] and any(bound[:7])):
                        raise _error("Inconsistent historical boundary state", boundary=side)
                    state["range"][side] = {"liquidity_gross": str(bound[0]), "liquidity_net": str(bound[1]),
                        "fee_growth_outside0_x128": str(bound[2]), "fee_growth_outside1_x128": str(bound[3]),
                        "initialized": bound[7]}
            return state

        initial, terminal = checkpoint(start - 1), checkpoint(end)
        if initial["pool"] != terminal["pool"]:
            raise _error("Pool identity changed across slice")
        query = {"address": pool, "fromBlock": hex(start), "toBlock": hex(end)}
        logs = rpc("eth_getLogs", [query])  # Deliberately no topics filter.
        if not isinstance(logs, list) or len(logs) >= MAX_LOGS:
            raise _error("Invalid or potentially capped event list")
        rows, seen, transactions = [], set(), {}
        for log in logs:
            if not isinstance(log, dict) or log.get("removed") is not False or _address(log.get("address")) != pool:
                raise _error("Removed or foreign pool log")
            block = _quantity(log.get("blockNumber"))
            tx_index, log_index = _quantity(log.get("transactionIndex")), _quantity(log.get("logIndex"))
            if not start <= block <= end or _hex(log.get("blockHash"), 32) != headers[block - start + 1]["hash"]:
                raise _error("Log block hash/range mismatch")
            tx_hash = _hex(log.get("transactionHash"), 32)
            key = (block, log_index)
            tx_key = (block, tx_index)
            if key in seen or transactions.get(tx_key, tx_hash) != tx_hash:
                raise _error("Duplicate log or inconsistent transaction")
            seen.add(key)
            transactions[tx_key] = tx_hash
            topics = log.get("topics")
            if not isinstance(topics, list) or not 1 <= len(topics) <= 4:
                raise _error("Invalid event topics")
            rows.append({"block_number": block, "block_hash": headers[block - start + 1]["hash"],
                "transaction_index": tx_index, "transaction_hash": tx_hash, "log_index": log_index,
                "address": pool, "raw_log": deepcopy(log), "topics": [_hex(t, 32) for t in topics],
                "data": _hex(log.get("data")), "removed": False})
        rows.sort(key=lambda row: (row["block_number"], row["transaction_index"], row["log_index"]))
        block_order = [(row["block_number"], row["log_index"]) for row in rows]
        if block_order != sorted(block_order):
            raise _error("Inconsistent transaction/log ordering")
        # Terminal hash commits the parent-linked ancestors; recheck both endpoints.
        for header in (headers[0], headers[-1]):
            raw = rpc("eth_getBlockByNumber", [hex(header["number"]), False])
            if (not isinstance(raw, dict) or _hex(raw.get("hash"), 32) != header["hash"]
                    or _quantity(raw.get("number")) != header["number"]):
                raise _error("Historical block reorganized during slice")
        comparison = _fold_core(rows, initial, terminal)
        if not any(row["topics"][0] == SWAP_TOPIC for row in rows):
            comparison["endpoint_fields_equal"] = {key: initial[key] == terminal[key] for key in
                ("slot0", "liquidity", "fee_growth_global0_x128", "fee_growth_global1_x128")}
        envelope = {"version": 1, "provider": "uniswap", "action": "pool_event_slice", "mock": False,
            "rpc_provider": args["rpc_provider"], "pool": initial["pool"], "request_args": deepcopy(args),
            "requested_range": {"start_block": start, "end_block": end, "inclusive": True},
            "range_receipt": {"complete": True, "blocks": end - start + 1, "logs": len(rows),
                "basis": "unfiltered_eth_getLogs_parent_linked_headers_and_rechecked_terminal_hash",
                "cryptographic_log_completeness_proven": False, "topics_filter": None},
            "headers": headers, "checkpoints": {"initial": initial, "terminal": terminal},
            "checkpoint_comparison": comparison, "rows_sha256": _hash(rows),
            "request_count": len(sources), "request_budget": request_budget, "response_bytes": response_bytes,
            "http_request_count": http_request_count,
            "sources": [{"url": url, "sha256": _hash(sources)}], "rpc_response_hashes": sources,
            "incomplete": True, "coverage": {"requested_window_complete": False, "completeness": "incomplete"},
            "tick_fee_reconstruction_verified": False, "performance_evidence": False,
            "financial_execution_allowed": False,
            "limitations": ["provider_reported_logs_not_receipt_trie_proof", "only_core_price_tick_active_liquidity_folded",
                "unknown_events_invalidate_core_verification", "no_tick_or_fee_reconstruction",
                "no_full_window_replay_trades_or_realized_yield"]}
        if ranged:
            envelope["range_continuity"] = _fold_range(rows, initial["range"], terminal["range"])
        if timed:
            envelope.update(original_request_args=original_args, observation_time=observation)
        envelope["evidence_sha256"] = _hash({"rows": rows, "_envelope": envelope})
        output = {"rows": rows, "_envelope": envelope}
        if len(_canonical(output)) > MAX_EVIDENCE_BYTES:
            raise _error("Event evidence output exceeds byte bound")
        return output
