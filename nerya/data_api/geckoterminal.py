"""Bounded public pool prices, never historical Jupiter routes or fills."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..connectors.http import HttpTransport, UrllibHttp
from ..harness.cancellation import CancelledError, raise_if_cancelled
from .types import DataActionSpec, DataApiContext, DataApiError

ORIGIN = "https://api.geckoterminal.com"
CATALOG_ORIGIN = "https://lite-api.jup.ag"
MAX_BYTES = 2_000_000
MAX_BARS = 1000
ADDRESS = {"type": "string", "pattern": r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"}
IDENTITY = {"network": {"type": "string", "enum": ["solana"]},
            "base_token": ADDRESS, "quote_token": ADDRESS}
DISCOVERY_SCHEMA = {"type": "object", "properties": IDENTITY,
                    "required": list(IDENTITY), "additionalProperties": False}
PAIR_DISCOVERY_SCHEMA = {"type": "object", "properties": {
    "network": {"type": "string", "enum": ["solana"]},
    "base_symbol": {"type": "string", "pattern": r"^[A-Za-z0-9._-]{1,32}$"},
    "quote_symbol": {"type": "string", "pattern": r"^[A-Za-z0-9._-]{1,32}$"},
}, "required": ["network", "base_symbol", "quote_symbol"],
    "additionalProperties": False}
INPUT_SCHEMA = {"type": "object", "properties": {
    **IDENTITY, "pool_address": ADDRESS,
    **{key: {"type": "string", "maxLength": 25} for key in ("start", "end")},
    "timeframe": {"type": "string", "enum": ["1h", "1d"]},
    "warmup_bars": {"type": "integer", "minimum": 0, "maximum": 999, "default": 0},
}, "required": [*IDENTITY, "pool_address", "start", "end", "timeframe"],
    "additionalProperties": False}


def geckoterminal_specs(http: HttpTransport | None = None) -> list[DataActionSpec]:
    provider = GeckoTerminalProvider(http)
    common = dict(provider="geckoterminal", output_kind="table",
                  tags=("solana", "dex", "pool", "ohlcv", "history"),
                  search_terms=("jupiter", "solana", "sol/usdc",
                                "pool price", "pool ohlcv"))
    return [DataActionSpec(**common, action="pair_discover", title="Public token-pair and pool discovery",
        description="Resolve exact Solana token symbols through the official Jupiter token catalog and list matching public pool identities. Current metadata only; this is a pool-price handoff, not historical Jupiter routes or fills.",
        input_schema=PAIR_DISCOVERY_SCHEMA, handler=provider.pair_discover),
        DataActionSpec(**common, action="pool_discover", title="Public pool identity discovery",
        description="One public catalog page, exact base/quote mints in source orientation. Current metadata only; not exhaustive.",
        input_schema=DISCOVERY_SCHEMA, handler=provider.pool_discover),
        DataActionSpec(**common, action="pool_ohlcv", title="Historical pool prices in quote tokens",
        description="Exact Solana pool/mints with verified current Jupiter token catalog identities; 1h or 1d closed candles in [start,end), plus warmup. At most 1000 total bars, three public GETs, no pagination/retries. No padding or inversion. USD volume is not base volume. Pool prices for simulated fills, not Jupiter route/fill evidence.",
        input_schema=INPUT_SCHEMA, handler=provider.pool_ohlcv, replay_input=True)]


def _error(message, *, kind="provider_error", **detail):
    return DataApiError(message, kind=kind, retryable=False, detail={
        "provider": "geckoterminal", "coverage_complete": False, **detail})


def _bytes(doc):
    try:
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise _error("Invalid source JSON") from None
    if len(raw) > MAX_BYTES:
        raise _error("Source exceeds byte bound")
    return raw


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+]00:00))?", value):
        raise _error("UTC date or whole-second RFC3339 required", kind="schema_validation")
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, OSError, OverflowError):
        raise _error("Invalid UTC timestamp", kind="schema_validation") from None


def _validate(args, schema):
    if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
        raise _error("Missing or unknown arguments", kind="schema_validation")
    if args.get("network") != "solana":
        raise _error("Only Solana supported", kind="schema_validation")
    for key in ("base_token", "quote_token", "pool_address"):
        if key in args and (not isinstance(args[key], str) or not re.fullmatch(ADDRESS["pattern"], args[key])):
            raise _error("Invalid exact address: " + key, kind="schema_validation")
    if args.get("base_token") == args.get("quote_token") and "base_token" in args:
        raise _error("Base and quote must differ", kind="schema_validation")


def _pool(item, included, args):
    try:
        address = item["attributes"]["address"]
        if (item["type"] != "pool" or item["id"] != "solana_" + address
                or not re.fullmatch(ADDRESS["pattern"], address)
                or ("pool_address" in args and address != args["pool_address"])):
            raise ValueError()
        relations = item["relationships"]
        tokens = {}
        for side in ("base", "quote"):
            mint = args[side + "_token"]
            relation = relations[side + "_token"]["data"]
            if relation != {"id": "solana_" + mint, "type": "token"}:
                raise ValueError()
            matches = [t for t in included if t.get("id") == relation["id"]]
            if len(matches) != 1 or matches[0].get("type") != "token":
                raise ValueError()
            attributes = matches[0]["attributes"]
            symbol, decimals = attributes["symbol"], attributes["decimals"]
            if (attributes["address"] != mint or not isinstance(symbol, str)
                    or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol)
                    or type(decimals) is not int or not 0 <= decimals <= 255):
                raise ValueError()
            tokens[side] = {"address": mint, "symbol": symbol, "decimals": decimals}
        dex = relations["dex"]["data"]
        if dex["type"] != "dex" or not re.fullmatch(r"[a-z0-9_-]{1,80}", dex["id"]):
            raise ValueError()
        return {"network": "solana", "address": address, **tokens, "dex": dex["id"]}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _error("Exact pool/token metadata identity mismatch") from None


class GeckoTerminalProvider:
    def __init__(self, http: HttpTransport | None = None):
        self.http = http if http is not None else UrllibHttp(
            max_response_bytes=MAX_BYTES, allowed_origins=(ORIGIN, CATALOG_ORIGIN), strict_json=True)

    def _get(self, path, params, context, *, origin=ORIGIN):
        raise_if_cancelled(context.cancel_token)
        if origin not in (ORIGIN, CATALOG_ORIGIN):
            raise _error("Public source origin is not allowlisted")
        prefix = "/api/v2/networks/solana/" if origin == ORIGIN else "/"
        url = origin + prefix + path + "?" + urlencode(params)
        try:
            status, doc = self.http.request("GET", url, headers={"Accept": "application/json"}, timeout=15)
        except CancelledError:
            raise
        except Exception:
            raise_if_cancelled(context.cancel_token)
            raise _error("Public pool request failed", source_url=url) from None
        raise_if_cancelled(context.cancel_token)
        expected = list if origin == CATALOG_ORIGIN else dict
        if status != 200 or not isinstance(doc, expected):
            raise _error("Public pool HTTP/envelope failure", http_status=status, source_url=url)
        source = {"url": url, "sha256": hashlib.sha256(_bytes(doc)).hexdigest(),
                  "retrieved_at": _iso(int(time.time())),
                  "hash_basis": "canonical_parsed_json_utf8_sorted_keys"}
        return doc, source

    def _token_identities(self, pool, context):
        doc, source = self._get("tokens/v2/search",
            {"query": pool["base"]["address"] + "," + pool["quote"]["address"]},
            context, origin=CATALOG_ORIGIN)
        if len(doc) > 100 or any(not isinstance(item, dict) for item in doc):
            raise _error("Invalid bounded official token catalog")
        identities = []
        for side in ("base", "quote"):
            token = pool[side]
            matches = [item for item in doc if item.get("id") == token["address"]]
            if len(matches) != 1:
                raise _error("Missing or duplicate official token identity")
            item = matches[0]
            tags = item.get("tags")
            verified = item.get("isVerified") is True or (
                isinstance(tags, list) and "verified" in tags)
            if (not verified or item.get("symbol") != token["symbol"]
                    or type(item.get("decimals")) is not int
                    or item["decimals"] != token["decimals"]):
                raise _error("Official token identity mismatch or unverified token")
            identities.append({**token, "identity_scope": "current_official_catalog",
                               "catalog_source_sha256": source["sha256"]})
        return identities, source

    def _tokens_by_symbols(self, context, base_symbol, quote_symbol):
        if base_symbol.casefold() == quote_symbol.casefold():
            raise _error("Base and quote symbols must differ", kind="schema_validation")
        combined, combined_source = self._get(
            "tokens/v2/search", {"query": base_symbol + "," + quote_symbol},
            context, origin=CATALOG_ORIGIN)
        if len(combined) > 100 or any(not isinstance(item, dict) for item in combined):
            raise _error("Invalid bounded official token catalog")
        selected = []
        sources = [combined_source]
        documents = {"combined": combined}
        if not combined:
            for symbol in (base_symbol, quote_symbol):
                document, source = self._get("tokens/v2/search", {"query": symbol},
                                             context, origin=CATALOG_ORIGIN)
                if len(document) > 100 or any(not isinstance(item, dict) for item in document):
                    raise _error("Invalid bounded official token catalog")
                documents[symbol] = document
                sources.append(source)
        for symbol in (base_symbol, quote_symbol):
            doc = documents["combined"] or documents[symbol]
            source = sources[0] if documents["combined"] else sources[1 + (symbol == quote_symbol)]
            matches = [item for item in doc
                       if str(item.get("symbol") or "").casefold() == symbol.casefold()]
            verified = [item for item in matches
                        if item.get("isVerified") is True or
                        (isinstance(item.get("tags"), list) and "verified" in item["tags"])]
            if len(verified) != 1:
                raise _error("Official token symbol is missing or ambiguous",
                             symbol=symbol, match_count=len(verified))
            item = verified[0]
            address = item.get("id")
            decimals = item.get("decimals")
            if (not isinstance(address, str) or not re.fullmatch(ADDRESS["pattern"], address)
                    or type(decimals) is not int or not 0 <= decimals <= 255):
                raise _error("Official token identity is malformed", symbol=symbol)
            selected.append({"address": address, "symbol": item["symbol"],
                             "decimals": decimals,
                             "identity_scope": "current_official_catalog",
                             "catalog_source_sha256": source["sha256"]})
        return selected, sources

    def pair_discover(self, context: DataApiContext, args: dict):
        _validate(args, PAIR_DISCOVERY_SCHEMA)
        base, quote = args["base_symbol"], args["quote_symbol"]
        tokens, token_sources = self._tokens_by_symbols(context, base, quote)
        discovered = self.pool_discover(context, {
            "network": args["network"],
            "base_token": tokens[0]["address"],
            "quote_token": tokens[1]["address"],
        })
        envelope = discovered.get("_envelope") if isinstance(discovered, dict) else {}
        return {
            "rows": discovered.get("rows", []),
            "token_identities": tokens,
            "_envelope": {
                **(envelope if isinstance(envelope, dict) else {}),
                "source_role": "alternative_pool_price",
                "historical_data_verified": False,
                "compatible_observation": "same_asset_pool_price_simulation",
                "incompatible_observations": [
                    "historical_jupiter_routes", "historical_jupiter_fills",
                    "historical_jupiter_route_fees",
                ],
                "sources": [*token_sources, *((envelope or {}).get("sources") or [])],
            },
        }

    def pool_discover(self, context: DataApiContext, args: dict):
        _validate(args, DISCOVERY_SCHEMA)
        doc, source = self._get("tokens/" + args["base_token"] + "/pools",
            {"include": "base_token,quote_token,dex", "page": 1}, context)
        if not isinstance(doc.get("data"), list) or len(doc["data"]) > 100:
            raise _error("Invalid bounded pool catalog")
        rows = []
        for item in doc["data"]:
            # Nonmatching catalog pairs are not identity assertions.
            try:
                rel = item["relationships"]
                match = all(rel[side + "_token"]["data"]["id"] == "solana_" + args[side + "_token"]
                            for side in ("base", "quote"))
            except (KeyError, TypeError):
                raise _error("Malformed catalog pool") from None
            if match:
                rows.append(_pool(item, doc.get("included", []), args))
        return {"rows": rows, "_envelope": {"provider": "geckoterminal",
            "action": "pool_discover", "mock": False, "sources": [source],
            "request_count": 1, "observation_kind": "current_pool_metadata",
            "catalog_complete": False}}

    def pool_ohlcv(self, context: DataApiContext, args: dict):
        _validate(args, INPUT_SCHEMA)
        timeframe = args["timeframe"]
        if timeframe not in ("1h", "1d"):
            raise _error("Unsupported timeframe", kind="schema_validation")
        step = 3600 if timeframe == "1h" else 86400
        start, end, warmup = _utc(args["start"]), _utc(args["end"]), args.get("warmup_bars", 0)
        if type(warmup) is not int or not 0 <= warmup < MAX_BARS:
            raise _error("warmup_bars must be an integer between 0 and 999", kind="schema_validation",
                         reason="invalid_warmup_bars", field="warmup_bars",
                         correction="Keep the requested window unchanged and provide a nonnegative integer warmup_bars below 1000.")
        if start < 0 or start >= end:
            raise _error("Pool OHLCV window must be positive", kind="schema_validation",
                         reason="invalid_window", field="start/end",
                         requested_window={"start": _iso(start), "end": _iso(end)},
                         correction="Preserve the pool identity and supply an end after start; the provider will not change the window.")
        if end > int(time.time()):
            raise _error("Pool OHLCV end cannot be in the future", kind="schema_validation",
                         reason="future_window_end", field="end",
                         requested_window={"start": _iso(start), "end": _iso(end)},
                         correction="Preserve the requested pool and use a completed UTC end time; the provider will not move or shorten it.")
        if start % step or end % step:
            bars = (end - start) // step
            raise _error("Pool OHLCV window must align to timeframe candle opens", kind="schema_validation",
                         reason="window_not_aligned", step=step,
                         bars={"requested": bars, "warmup": warmup,
                               "expected": bars + warmup, "max": MAX_BARS},
                         correction="Align explicit or pinned start/end to closed UTC candle boundaries before calling pool_ohlcv; the provider will not floor, move, or shorten the window.")
        bars = (end - start) // step
        expected_bars = bars + warmup
        if start - warmup * step < 0:
            raise _error("Pool OHLCV warmup begins before Unix epoch", kind="schema_validation",
                         reason="warmup_before_epoch", step=step,
                         bars={"requested": bars, "warmup": warmup,
                               "expected": expected_bars, "max": MAX_BARS},
                         correction="Preserve the explicit window and reduce warmup_bars only if that change is intended.")
        if expected_bars > MAX_BARS:
            raise _error("Pool OHLCV request exceeds the provider bar limit", kind="schema_validation",
                         reason="bar_budget_exceeded", step=step,
                         bars={"requested": bars, "warmup": warmup,
                               "expected": expected_bars, "max": MAX_BARS},
                         correction="Preserve the explicit window and pool identity; reduce warmup_bars or provide an explicitly approved shorter aligned window.")
        doc, metadata_source = self._get("pools/" + args["pool_address"],
            {"include": "base_token,quote_token,dex"}, context)
        pool = _pool(doc.get("data"), doc.get("included", []), args)
        doc, candles_source = self._get("pools/" + args["pool_address"] + "/ohlcv/"
            + ("hour" if timeframe == "1h" else "day"),
            {"aggregate": 1, "currency": "token", "token": "base", "limit": MAX_BARS,
             "include_empty_intervals": "false", "before_timestamp": end - 1}, context)
        try:
            for side in ("base", "quote"):
                if doc["meta"][side]["address"] != pool[side]["address"]:
                    raise ValueError()
            if doc["data"]["type"] != "ohlcv_request_response":
                raise ValueError()
            raw = doc["data"]["attributes"]["ohlcv_list"]
            if not isinstance(raw, list) or len(raw) > MAX_BARS:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise _error("Candle metadata/identity mismatch") from None
        by_ts = {}
        for row in raw:
            if (not isinstance(row, list) or len(row) != 6 or type(row[0]) is not int
                    or row[0] < 0 or row[0] % step or row[0] >= end
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in row[1:])
                    or min(row[1:5]) <= 0 or row[5] < 0
                    or row[3] > min(row[1], row[4]) or row[2] < max(row[1], row[4])
                    or row[3] > row[2]):
                raise _error("Invalid OHLCV row")
            normalized = dict(zip(("ts", "open", "high", "low", "close", "volume"), row))
            if row[0] in by_ts and by_ts[row[0]] != normalized:
                raise _error("Conflicting duplicate candle")
            by_ts[row[0]] = normalized
        wanted = range(start - warmup * step, end, step)
        missing = [ts for ts in wanted if ts not in by_ts]
        complete = not any(ts >= start for ts in missing)
        warmup_complete = not any(ts < start for ts in missing)
        selected = [by_ts[ts] for ts in wanted if ts in by_ts]
        identities, catalog_source = self._token_identities(pool, context)
        return {"rows": selected, "_envelope": {
            "provider": "geckoterminal", "action": "pool_ohlcv", "mock": False,
            "observation_kind": "historical_pool_ohlcv", "timeframe": timeframe, "pool": pool,
            "quote_units": "quote_token_per_base_token", "price_units": "quote_token_per_base_token",
            "volume_units": "upstream_reported_usd", "volume_available": False,
            "requested_window": {"start": _iso(start), "end": _iso(end), "inclusive": False},
            "warmup_bars": warmup, "request_count": 3, "provider_truncated": bool(missing),
            "token_identities": identities,
            "source_observed_range": {"start": _iso(min(by_ts)) if by_ts else None,
                                      "end": _iso(max(by_ts)) if by_ts else None},
            "coverage": {"requested_window_complete": complete, "warmup_complete": warmup_complete,
                "completeness": "complete" if not missing else "incomplete",
                "missing_timestamps": missing, "observations": len(selected)},
            "sources": [metadata_source, candles_source, catalog_source]}}
