"""Candle fetching — real connector-backed.

Mock fallback is *opt-in only*. Production runtime paths that call
:func:`fetch_candles` without authorising mock mode get an empty list and a
degraded envelope rather than fabricated OHLCV data. See
:mod:`nerya.core.truth` for the opt-in mechanics.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core import yaml_io
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
    tag_list_envelope,
)


def mock_candles(market: str, *, count: int = 60, interval_s: int = 60,
                 seed_price: float | None = None) -> list[dict[str, Any]]:
    """Generate a synthetic OHLCV series biased bullish to trigger breakouts."""
    base = seed_price if seed_price is not None else _default_price(market)
    now = int(time.time())
    out = []
    price = base
    for i in range(count):
        ts = now - (count - i) * interval_s
        drift = 0.0005 * math.sin(i / 5)
        price = price * (1 + drift + 0.0002)
        high = price * 1.001
        low = price * 0.999
        open_ = price * 0.9998
        close = price
        vol = 10 + (i % 7)
        out.append({"ts": ts, "open": open_, "high": high, "low": low,
                    "close": close, "volume": vol})
    last = out[-1]
    last["close"] = last["close"] * 1.015
    last["high"] = last["close"] * 1.002
    return out


def _default_price(market: str) -> float:
    return {
        "MOCK:BTCUSDT": 80000.0,
        "MOCK:ETHUSDT": 3500.0,
        "MOCK:SOLUSDT": 180.0,
        "PAPER:BTCUSDT": 80000.0,
        "PAPER:ETHUSDT": 3500.0,
        "PAPER:SOLUSDT": 180.0,
    }.get(market, 100.0)


# ---------------------------------------------------------------- normalization

def normalize_klines(venue: str, rows: list[Any]) -> list[dict[str, Any]]:
    """Normalize exchange-native kline arrays into ``{ts, open, high, low, close, volume}``.

    Supports Binance, Bybit v5, OKX, Hyperliquid shapes. Unknown shapes
    return an empty list so callers fall back to the mock.
    """
    if not rows:
        return []
    v = (venue or "").upper()
    out: list[dict[str, Any]] = []

    if v == "BINANCE":
        for r in rows:
            if len(r) < 6:
                continue
            out.append({
                "ts": int(r[0]) // 1000,
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]),
            })
        return out

    if v == "BYBIT":
        # Bybit returns newest-first; reverse for chronological order.
        for r in reversed(rows):
            if len(r) < 6:
                continue
            out.append({
                "ts": int(r[0]) // 1000,
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]),
            })
        return out

    if v == "OKX":
        for r in reversed(rows):
            if len(r) < 6:
                continue
            out.append({
                "ts": int(r[0]) // 1000,
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]),
            })
        return out

    if v == "KRAKEN":
        # Kraken OHLC shape:
        # [time_s, open, high, low, close, vwap, volume, count]
        for r in rows:
            if len(r) < 7:
                continue
            out.append({
                "ts": int(r[0]),
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[6]),
            })
        return out

    if v in ("HYPERLIQUID", "HL"):
        for r in rows:
            if isinstance(r, dict) and "t" in r:
                out.append({
                    "ts": int(r["t"]) // 1000,
                    "open": float(r.get("o", 0)), "high": float(r.get("h", 0)),
                    "low": float(r.get("l", 0)), "close": float(r.get("c", 0)),
                    "volume": float(r.get("v", 0)),
                })
        return out

    # Best-effort generic array of six numbers
    for r in rows:
        if isinstance(r, list | tuple) and len(r) >= 6:
            try:
                out.append({
                    "ts": int(r[0]) // 1000 if int(r[0]) > 1e12 else int(r[0]),
                    "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                    "volume": float(r[5]),
                })
            except (TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------- real REST fallback

def _http_json(url: str) -> Any:
    req = Request(url, headers={"User-Agent": "Nerya/0.1"})
    with urlopen(req, timeout=12) as res:
        import json

        return json.loads(res.read().decode("utf-8"))


def _tail_symbol(market: str) -> str:
    return str(market or "").split(":", 1)[-1].strip().upper()


def _compact_symbol(market: str) -> str:
    return _tail_symbol(market).replace("/", "").replace("-", "")


def _hyphen_symbol(market: str) -> str:
    tail = _tail_symbol(market).replace("/", "-")
    if "-" in tail:
        return tail
    for q in ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH"):
        if tail.endswith(q) and len(tail) > len(q):
            return f"{tail[:-len(q)]}-{q}"
    return tail


def _bybit_interval(interval: str) -> str:
    raw = str(interval or "1m").strip().lower()
    if raw.endswith("m"):
        return raw[:-1] or "1"
    if raw.endswith("h"):
        try:
            return str(int(raw[:-1]) * 60)
        except ValueError:
            return "60"
    if raw.endswith("d"):
        return "D"
    return raw or "1"


def _okx_interval(interval: str) -> str:
    raw = str(interval or "1m").strip()
    lowered = raw.lower()
    if lowered.endswith("h"):
        return lowered[:-1] + "H"
    if lowered.endswith("d"):
        return lowered[:-1] + "D"
    if lowered.endswith("w"):
        return lowered[:-1] + "W"
    return lowered


def _tf_seconds(interval: str) -> int:
    raw = str(interval or "1m").strip().lower()
    try:
        qty = int(raw[:-1] or 1)
    except ValueError:
        qty = 1
    unit = raw[-1:] or "m"
    if unit == "m":
        return qty * 60
    if unit == "h":
        return qty * 3600
    if unit == "d":
        return qty * 86400
    if unit == "w":
        return qty * 7 * 86400
    return 60


def _dedupe_sort_tail(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    by_ts: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            ts = int(row.get("ts", row.get("ts_ms", 0)))
        except Exception:
            continue
        if ts <= 0:
            continue
        by_ts[ts] = row
    out = [by_ts[ts] for ts in sorted(by_ts)]
    if limit > 0:
        out = out[-limit:]
    return out


def _window_from_count(
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> tuple[int, int]:
    end_s = int(end or time.time())
    if start is not None:
        return int(start), end_s
    span = max(1, int(count or 60) + 5) * _tf_seconds(interval)
    return end_s - span, end_s


def _bybit_category(venue: str) -> str:
    v = (venue or "").upper()
    if any(key in v for key in ("PERP", "LINEAR", "SWAP", "FUTURES")):
        return "linear"
    if "INVERSE" in v:
        return "inverse"
    return "spot"


def _kraken_interval(interval: str) -> str:
    raw = str(interval or "1m").strip().lower()
    try:
        if raw.endswith("m"):
            return str(max(1, int(raw[:-1] or "1")))
        if raw.endswith("h"):
            return str(max(1, int(raw[:-1] or "1")) * 60)
        if raw.endswith("d"):
            return str(max(1, int(raw[:-1] or "1")) * 1440)
    except ValueError:
        pass
    return "1"


def _kraken_pair(market: str) -> str:
    s = _compact_symbol(market)
    if s.startswith("BTC"):
        s = "XBT" + s[3:]
    return s


def _canonical_venue(venue: str) -> str:
    raw = str(venue or "").strip()
    if raw.lower().startswith("ccxt:"):
        raw = raw.split(":", 1)[1]
    if raw.lower().startswith("ccxt_"):
        raw = raw.split("_", 1)[1]
    key = raw.replace("-", "_").replace(" ", "_").upper()
    aliases = {
        "BINANCEUSDM": "BINANCE_PERPETUAL",
        "BINANCE_USDM": "BINANCE_PERPETUAL",
        "BINANCE_PERP": "BINANCE_PERPETUAL",
        "BINANCE_FUTURES": "BINANCE_PERPETUAL",
        "BYBIT_PERP": "BYBIT_PERPETUAL",
        "BYBIT_LINEAR": "BYBIT_PERPETUAL",
        "BYBIT_SWAP": "BYBIT_PERPETUAL",
        "BYBIT_FUTURES": "BYBIT_PERPETUAL",
        "OKX_OS": "OKX_ONCHAIN",
        "OKX_ONCHAIN_OS": "OKX_ONCHAIN",
        "BITGET_WALLET": "BITGET_ONCHAIN",
        "BITGET_ONCHAIN": "BITGET_ONCHAIN",
        "BINANCE_WEB3": "BINANCE_ALPHA",
        "BINANCE_AGENTIC": "BINANCE_ALPHA",
        "BINANCE_ALPHA": "BINANCE_ALPHA",
        "COINBASE_WALLET": "COINBASE_WALLET",
        "COINBASE_EXCHANGE_WALLET": "COINBASE_WALLET",
        "BYREAL": "BYREAL_ONCHAIN",
        "BYREAL_CLI": "BYREAL_ONCHAIN",
        "BYREAL_SOLANA": "BYREAL_ONCHAIN",
        "BYREAL_ONCHAIN": "BYREAL_ONCHAIN",
        "ONCHAIN": "ONCHAIN",
        "PAPER": "PAPER",
        "MOCK": "MOCK",
        "EQUITY": "YAHOO",
        "EQUITIES": "YAHOO",
        "STOCK": "YAHOO",
        "STOCKS": "YAHOO",
        "US_EQUITY": "YAHOO",
        "US_EQUITIES": "YAHOO",
        "US_STOCK": "YAHOO",
        "US_STOCKS": "YAHOO",
        "YAHOO_FINANCE": "YAHOO",
    }
    return aliases.get(key, key)


def canonical_venue(venue: str) -> str:
    """Public wrapper for shared market-data venue canonicalization."""

    return _canonical_venue(venue)


def _market_for_venue(market: str, venue: str) -> str:
    tail = _tail_symbol(market)
    return f"{_canonical_venue(venue)}:{tail}" if venue else market


_NON_CCXT_VENUES = {
    "",
    "MOCK",
    "PAPER",
    "YAHOO",
    "ALPACA",
    "IBKR",
    "MT5",
    "ONCHAIN",
    "OKX_ONCHAIN",
    "BITGET_ONCHAIN",
    "BYREAL_ONCHAIN",
    "BINANCE_ALPHA",
    "COINBASE_WALLET",
    "TUSHARE",
    "AKSHARE",
    "POLYGON_IO",
    "COINGECKO",
    "COINMARKETCAP",
    "GLASSNODE",
    "DUNE",
    "TENCENT",
    "MOEX",
    "MESSARI",
    "HTTP",
}


def _normalised_exchange_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _ccxt_exchange_id_for_venue(venue: str) -> str:
    canon = _canonical_venue(venue)
    if canon in _NON_CCXT_VENUES:
        return ""
    key = canon.lower()
    aliases = {
        "binance_spot": "binance",
        "binance_perpetual": "binanceusdm",
        "binance_perp": "binanceusdm",
        "binance_usdm": "binanceusdm",
        "binanceusdm": "binanceusdm",
        "binance_coinm_perpetual": "binancecoinm",
        "binance_coinm": "binancecoinm",
        "bybit_perpetual": "bybit",
        "bybit_perp": "bybit",
        "bybit_linear": "bybit",
        "bybit_swap": "bybit",
        "bybit_futures": "bybit",
        "okx_perpetual": "okx",
        "okx_perp": "okx",
        "okx_swap": "okx",
        "gate_perpetual": "gate",
        "gate_io": "gate",
        "gateio": "gate",
        "bitget_perpetual": "bitget",
        "bitget_perp": "bitget",
        "bitget_mix": "bitget",
        "kucoin_perpetual": "kucoinfutures",
        "kucoin_perp": "kucoinfutures",
        "hyperliquid_perpetual": "hyperliquid",
        "hyperliquid_perp": "hyperliquid",
        "hl": "hyperliquid",
        "hl_perp": "hyperliquid",
        "coinbase_exchange": "coinbaseexchange",
        "coinbase_pro": "coinbaseexchange",
        "coinbasepro": "coinbaseexchange",
        "coinbase_international": "coinbaseinternational",
    }
    if key in aliases:
        return aliases[key]
    try:
        from ..connectors.ccxt_adapter import supported_exchanges

        supported = supported_exchanges()
    except Exception:
        supported = []
    if not supported:
        # ccxt may simply be absent. Return the normalized id so the caller can
        # surface the ccxt install/error path instead of silently skipping the
        # requested exchange.
        return key
    if key in supported:
        return key
    by_normalised = {_normalised_exchange_key(exchange_id): exchange_id for exchange_id in supported}
    return by_normalised.get(_normalised_exchange_key(key), "")


def _ccxt_options_for_venue(venue: str) -> dict[str, Any]:
    canon = _canonical_venue(venue)
    if any(token in canon for token in ("PERPETUAL", "PERP", "SWAP", "FUTURES", "LINEAR")):
        return {"defaultType": "swap"}
    return {}


def _config_get(config_like: Any | None, dotted: str, default: Any = None) -> Any:
    if config_like is None:
        return default
    getter = getattr(config_like, "get", None)
    if callable(getter):
        try:
            return getter(dotted, default)
        except TypeError:
            pass
    cur = config_like
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _workspace_paths(config_like: Any | None) -> Any | None:
    return getattr(config_like, "paths", None) if config_like is not None else None


def _add_source(out: list[dict[str, Any]], seen: set[str], venue: str, *, origin: str) -> None:
    canon = _canonical_venue(venue)
    if not canon or canon in seen:
        return
    seen.add(canon)
    out.append({"venue": canon.lower(), "canonical": canon, "origin": origin})


def _config_data(config_like: Any | None) -> dict[str, Any]:
    data = getattr(config_like, "data", None)
    return data if isinstance(data, dict) else (config_like if isinstance(config_like, dict) else {})


def _wallet_market_data_sources() -> list[dict[str, Any]]:
    try:
        from ..wallet import list_wallet_market_data_sources

        return list_wallet_market_data_sources()
    except Exception:
        return []


def _wallet_market_data_source_by_canonical() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for source in _wallet_market_data_sources():
        canon = _canonical_venue(str(source.get("canonical") or source.get("venue") or ""))
        if canon:
            out[canon] = source
    return out


def _wallet_market_data_bindings(config_like: Any | None) -> list[dict[str, Any]]:
    """Return configured wallet bindings that can supply market data."""

    data = _config_data(config_like)
    if not data:
        return []
    try:
        from ..wallet import list_configured_providers, market_data_sources_for_provider

        bindings = list_configured_providers(data)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for binding in bindings:
        provider_name = str(binding.get("provider") or "").lower()
        for source in market_data_sources_for_provider(provider_name):
            item = dict(binding)
            item["market_data_source"] = dict(source)
            item["venue"] = source.get("venue")
            item["canonical"] = _canonical_venue(
                str(source.get("canonical") or source.get("venue") or "")
            )
            out.append(item)
    return out


def _split_chain_token_market(market: str) -> tuple[str, str] | None:
    parts = str(market or "").split(":", 2)
    if len(parts) != 3:
        return None
    chain = parts[1].strip()
    token = parts[2].strip()
    if not chain or not token:
        return None
    return chain, token


def _split_single_market(market: str) -> str | None:
    parts = str(market or "").split(":", 1)
    if len(parts) != 2:
        return None
    value = parts[1].strip()
    return value or None


def _fetch_wallet_market_klines(
    market: str,
    *,
    interval: str,
    count: int,
    config_like: Any | None,
    start: int | None = None,
    end: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    venue = _canonical_venue(_venue_of(market))
    if venue == "ONCHAIN":
        parsed = _split_chain_token_market(market)
        if parsed is None:
            return [], "not_onchain_market"
        chain, token = parsed
        try:
            from .onchain_klines import fetch_token_klines

            return fetch_token_klines(
                chain,
                token,
                interval=interval,
                limit=count,
            ), ""
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"

    source_defs = _wallet_market_data_source_by_canonical()
    if venue not in source_defs:
        return [], "unsupported_wallet_market_venue"
    bindings = [
        binding for binding in _wallet_market_data_bindings(config_like)
        if _canonical_venue(str(binding.get("canonical") or "")) == venue
    ]
    if not bindings:
        return [], f"no_configured_{venue.lower()}_wallet"
    source_def = source_defs[venue]
    market_format = str(source_def.get("market_format") or "")
    chain = token = market_id = ""
    if market_format == "chain:token":
        parsed = _split_chain_token_market(market)
        if parsed is None:
            return [], f"{venue.lower()}_requires_chain_token_market"
        chain, token = parsed
    else:
        market_id = _split_single_market(market) or ""
        if not market_id:
            return [], f"{venue.lower()}_requires_market"

    workspace = None
    paths = _workspace_paths(config_like)
    if paths is not None:
        workspace = getattr(paths, "root", None)
    last_err = ""
    for binding in bindings:
        try:
            from ..wallet import build_provider

            provider_name = str(binding.get("provider") or "")
            provider = build_provider(
                provider_name,
                dict(binding.get("config") or {}),
                workspace=workspace,
            )
            source = dict(binding.get("market_data_source") or source_def)
            method = str(source.get("fetch_method") or "").strip()
            fetcher = getattr(provider, method, None) if method else None
            if not callable(fetcher):
                continue
            if market_format == "chain:token":
                rows = fetcher(
                    chain=chain,
                    token=token,
                    interval=interval,
                    limit=count,
                    after=str(int(start) * 1000) if start is not None else None,
                    before=str(int(end) * 1000) if end is not None else None,
                    start=start,
                    end=end,
                )
            else:
                rows = fetcher(
                    market=market_id,
                    interval=interval,
                    limit=count,
                    after=str(int(start) * 1000) if start is not None else None,
                    before=str(int(end) * 1000) if end is not None else None,
                    start=start,
                    end=end,
                )
            if rows:
                if start is not None or end is not None:
                    lo, hi = _window_from_count(
                        interval=interval,
                        count=count,
                        start=start,
                        end=end,
                    )
                    rows = [row for row in rows if lo <= int(row.get("ts", 0)) <= hi]
                venue_name = str(source.get("venue") or venue.lower())
                env = live_envelope(
                    source=provider_name,
                    venue=venue_name,
                    connector_id=str(binding.get("wallet_id") or ""),
                )
                return tag_list_envelope(rows[:count], env), ""
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    return [], last_err or f"{venue.lower()}_returned_no_rows"


def discover_market_data_sources(config_like: Any | None = None) -> list[dict[str, Any]]:
    """Return venue candidates from workspace config plus built-in public feeds."""

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    default_venue = _config_get(config_like, "workspace_preferences.market_defaults.venue", "")
    if default_venue:
        _add_source(out, seen, default_venue, origin="workspace_preferences.market_defaults.venue")
    for venue in _config_get(
        config_like,
        "workspace_preferences.market_defaults.preferred_venues",
        [],
    ) or []:
        _add_source(out, seen, str(venue), origin="workspace_preferences.market_defaults.preferred_venues")

    paths = _workspace_paths(config_like)
    if paths is not None:
        try:
            accounts = yaml_io.load(paths.accounts_file).get("accounts") or []
        except Exception:
            accounts = []
        for account in accounts:
            if isinstance(account, dict):
                venue = account.get("venue") or account.get("exchange")
                _add_source(out, seen, str(venue or ""), origin="accounts")

        try:
            exchanges = yaml_io.load(paths.exchanges_file).get("exchanges") or {}
        except Exception:
            exchanges = {}
        if isinstance(exchanges, dict):
            for key, value in exchanges.items():
                venue = key
                if isinstance(value, dict):
                    venue = value.get("venue") or value.get("exchange") or key
                _add_source(out, seen, str(venue), origin="exchanges")

        providers = Path(paths.root) / "providers"
        if providers.exists():
            for child in providers.iterdir():
                if child.is_dir() and (child / "provider.py").exists():
                    _add_source(out, seen, child.name, origin="workspace_providers")

    for venue in (
        "yahoo",
        "binance",
        "binance_perpetual",
        "okx",
        "bybit",
        "bybit_perpetual",
        "kraken",
    ):
        _add_source(out, seen, venue, origin="built_in_public_rest")
    for binding in _wallet_market_data_bindings(config_like):
        source = dict(binding.get("market_data_source") or {})
        venue = str(source.get("venue") or "")
        if venue:
            before = len(out)
            _add_source(
                out,
                seen,
                venue,
                origin=f"wallet.providers:{binding.get('wallet_id') or binding.get('provider')}",
            )
            if len(out) > before:
                out[-1].update({
                    "label": source.get("label"),
                    "market_format": source.get("market_format"),
                    "provider": binding.get("provider"),
                    "wallet_id": binding.get("wallet_id"),
                    "description": source.get("description"),
                })
    _add_source(out, seen, "onchain", origin="built_in_onchain_geckoterminal")
    return out


def _candidate_venues(market: str, config_like: Any | None) -> list[str]:
    explicit = _venue_of(market)
    if explicit:
        canon = _canonical_venue(explicit)
        return [canon] if canon else []
    venues: list[str] = []
    seen: set[str] = set()
    for source in discover_market_data_sources(config_like):
        canon = str(source.get("canonical") or "").upper()
        if canon and canon not in seen:
            venues.append(canon)
            seen.add(canon)
    return venues


def _fetch_public_rest_klines(
    venue: str,
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch public OHLCV via venue REST when ccxt cannot load markets.

    This is intentionally narrow and read-only. It is not a mock fallback:
    each branch calls the venue's public market-data endpoint and returns
    normalized live candles, or raises so the caller can emit a degraded
    envelope.
    """

    v = (venue or "").upper()
    target = max(1, int(count or 60))
    if v == "BINANCE":
        return _fetch_binance_rest_pages(
            "https://api.binance.com/api/v3/klines",
            market,
            interval=interval,
            count=target,
            start=start,
            end=end,
        )

    if v in {"BINANCE_PERPETUAL", "BINANCE_PERP", "BINANCEUSDM", "BINANCE_USDM"}:
        return _fetch_binance_rest_pages(
            "https://fapi.binance.com/fapi/v1/klines",
            market,
            interval=interval,
            count=target,
            start=start,
            end=end,
        )

    if v == "OKX":
        return _fetch_okx_rest_pages(market, interval=interval, count=target, start=start, end=end)

    if v in {"BYBIT", "BYBIT_PERPETUAL"}:
        return _fetch_bybit_rest_pages(
            v,
            market,
            interval=interval,
            count=target,
            start=start,
            end=end,
        )

    if v == "KRAKEN":
        return _fetch_kraken_rest_pages(market, interval=interval, count=target, start=start, end=end)

    return []


def _fetch_ccxt_klines(
    venue: str,
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    exchange_id = _ccxt_exchange_id_for_venue(venue)
    if not exchange_id:
        return []
    from ..connectors.ccxt_adapter import CcxtConnector

    since_ms = int(start) * 1000 if start is not None else None
    end_ms = int(end) * 1000 if end is not None else None
    conn = CcxtConnector(
        exchange_id=exchange_id,
        venue=_canonical_venue(venue),
        live=False,
        options=_ccxt_options_for_venue(venue),
    )
    rows = conn.get_klines(
        market,
        interval=interval,
        limit=count,
        since=since_ms,
        end=end_ms,
    )
    return normalize_klines(getattr(conn, "venue", venue), rows)


def _connector_get_klines(
    conn: Any,
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> list[Any]:
    since = int(start) * 1000 if start is not None else None
    end_ms = int(end) * 1000 if end is not None else None
    try:
        return conn.get_klines(
            market,
            interval=interval,
            limit=count,
            since=since,
            end=end_ms,
        )
    except TypeError:
        try:
            return conn.get_klines(
                market,
                interval=interval,
                limit=count,
                since=since,
            )
        except TypeError:
            return conn.get_klines(market, interval=interval, limit=count)


def _fetch_binance_rest_pages(
    base_url: str,
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    page_limit = 1000
    start_s, end_s = _window_from_count(interval=interval, count=count, start=start, end=end)
    cursor_ms = start_s * 1000
    end_ms = end_s * 1000
    rows: list[dict[str, Any]] = []
    max_pages = max(1, min(100, (count + page_limit - 1) // page_limit + 5))
    for _ in range(max_pages):
        url = base_url + "?" + urlencode({
            "symbol": _compact_symbol(market),
            "interval": interval,
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": min(page_limit, max(1, count - len(rows))),
        })
        batch = normalize_klines("BINANCE", _http_json(url))
        if not batch:
            break
        rows.extend(batch)
        newest_ms = max(int(row["ts"]) * 1000 for row in batch)
        next_cursor = newest_ms + (_tf_seconds(interval) * 1000)
        if next_cursor <= cursor_ms:
            break
        cursor_ms = next_cursor
        rows = _dedupe_sort_tail(rows, limit=count)
        if len(rows) >= count or cursor_ms > end_ms:
            break
    return _dedupe_sort_tail(rows, limit=count)


def _fetch_okx_rest_pages(
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    page_limit = 300
    start_s, end_s = _window_from_count(interval=interval, count=count, start=start, end=end)
    cursor_ms = end_s * 1000
    floor_ms = start_s * 1000
    rows: list[dict[str, Any]] = []
    max_pages = max(1, min(100, (count + page_limit - 1) // page_limit + 5))
    for _ in range(max_pages):
        url = "https://www.okx.com/api/v5/market/candles?" + urlencode({
            "instId": _hyphen_symbol(market),
            "bar": _okx_interval(interval),
            "before": cursor_ms,
            "limit": page_limit,
        })
        doc = _http_json(url)
        batch = normalize_klines("OKX", (doc.get("data") or []))
        batch = [row for row in batch if floor_ms <= int(row["ts"]) * 1000 <= cursor_ms]
        if not batch:
            break
        rows.extend(batch)
        oldest_ms = min(int(row["ts"]) * 1000 for row in batch)
        next_cursor = oldest_ms - 1
        if next_cursor >= cursor_ms:
            break
        cursor_ms = next_cursor
        rows = _dedupe_sort_tail(rows, limit=count)
        if len(rows) >= count or cursor_ms < floor_ms:
            break
    return _dedupe_sort_tail(rows, limit=count)


def _fetch_bybit_rest_pages(
    venue: str,
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    page_limit = 1000
    start_s, end_s = _window_from_count(interval=interval, count=count, start=start, end=end)
    cursor_ms = end_s * 1000
    floor_ms = start_s * 1000
    rows: list[dict[str, Any]] = []
    max_pages = max(1, min(100, (count + page_limit - 1) // page_limit + 5))
    for _ in range(max_pages):
        url = "https://api.bybit.com/v5/market/kline?" + urlencode({
            "category": _bybit_category(venue),
            "symbol": _compact_symbol(market),
            "interval": _bybit_interval(interval),
            "start": floor_ms,
            "end": cursor_ms,
            "limit": page_limit,
        })
        doc = _http_json(url)
        batch = normalize_klines("BYBIT", ((doc.get("result") or {}).get("list") or []))
        batch = [row for row in batch if floor_ms <= int(row["ts"]) * 1000 <= cursor_ms]
        if not batch:
            break
        rows.extend(batch)
        oldest_ms = min(int(row["ts"]) * 1000 for row in batch)
        next_cursor = oldest_ms - 1
        if next_cursor >= cursor_ms:
            break
        cursor_ms = next_cursor
        rows = _dedupe_sort_tail(rows, limit=count)
        if len(rows) >= count or cursor_ms < floor_ms:
            break
    return _dedupe_sort_tail(rows, limit=count)


def _fetch_kraken_rest_pages(
    market: str,
    *,
    interval: str,
    count: int,
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    start_s, end_s = _window_from_count(interval=interval, count=count, start=start, end=end)
    cursor_s = start_s
    rows: list[dict[str, Any]] = []
    max_pages = max(1, min(100, (count + 719) // 720 + 5))
    for _ in range(max_pages):
        url = "https://api.kraken.com/0/public/OHLC?" + urlencode({
            "pair": _kraken_pair(market),
            "interval": _kraken_interval(interval),
            "since": cursor_s,
        })
        doc = _http_json(url)
        result = doc.get("result") or {}
        raw_rows: list[Any] = []
        next_cursor = None
        for key, value in result.items():
            if key == "last":
                try:
                    next_cursor = int(value)
                except Exception:
                    next_cursor = None
            elif isinstance(value, list):
                raw_rows = value
        batch = [
            row for row in normalize_klines("KRAKEN", raw_rows)
            if start_s <= int(row["ts"]) <= end_s
        ]
        if not batch:
            break
        rows.extend(batch)
        newest = max(int(row["ts"]) for row in batch)
        cursor_next = max(newest + _tf_seconds(interval), int(next_cursor or 0))
        if cursor_next <= cursor_s:
            break
        cursor_s = cursor_next
        rows = _dedupe_sort_tail(rows, limit=count)
        if len(rows) >= count or cursor_s > end_s:
            break
    return _dedupe_sort_tail(rows, limit=count)


def _fetch_public_rest_ticker(venue: str, market: str) -> dict[str, Any]:
    v = _canonical_venue(venue)
    if v == "YAHOO":
        from ..connectors.yahoo import YahooFinanceConnector

        t = YahooFinanceConnector().get_ticker(market)
        return {
            "price": float(t.last or t.mid),
            "bid": float(t.bid),
            "ask": float(t.ask),
            "mid": float(t.mid),
            "last": float(t.last),
            "spread_bps": float(t.spread_bps),
            "ts_ms": int(t.ts_ms),
        }
    if v == "BINANCE":
        doc = _http_json("https://api.binance.com/api/v3/ticker/price?" + urlencode({
            "symbol": _compact_symbol(market),
        }))
        return {"price": float(doc["price"])}
    if v == "BINANCE_PERPETUAL":
        doc = _http_json("https://fapi.binance.com/fapi/v1/ticker/price?" + urlencode({
            "symbol": _compact_symbol(market),
        }))
        return {"price": float(doc["price"])}
    if v == "OKX":
        doc = _http_json("https://www.okx.com/api/v5/market/ticker?" + urlencode({
            "instId": _hyphen_symbol(market),
        }))
        rows = doc.get("data") or []
        if rows:
            return {"price": float(rows[0]["last"])}
    if v == "BYBIT":
        doc = _http_json("https://api.bybit.com/v5/market/tickers?" + urlencode({
            "category": "spot",
            "symbol": _compact_symbol(market),
        }))
        rows = ((doc.get("result") or {}).get("list") or [])
        if rows:
            return {"price": float(rows[0]["lastPrice"])}
    if v == "KRAKEN":
        doc = _http_json("https://api.kraken.com/0/public/Ticker?" + urlencode({
            "pair": _kraken_pair(market),
        }))
        result = doc.get("result") or {}
        for value in result.values():
            if isinstance(value, dict) and value.get("c"):
                return {"price": float(value["c"][0])}
    return {}


def fetch_public_ticker(
    market: str,
    *,
    allow_mock: bool | None = None,
    config_like: Any | None = None,
) -> dict[str, Any]:
    """Fetch a live ticker/mark snapshot using dynamic workspace candidates."""

    err = ""
    for venue in _candidate_venues(market, config_like):
        if venue in {"MOCK", "PAPER"}:
            continue
        try:
            market_id = _market_for_venue(market, venue)
            snap = _fetch_public_rest_ticker(venue, market_id)
            if snap and snap.get("price"):
                env = live_envelope(
                    source=f"{venue.lower()}_rest",
                    venue=venue.lower(),
                    connector_id="public_rest",
                )
                return {
                    "price": float(snap["price"]),
                    "bid": snap.get("bid"),
                    "ask": snap.get("ask"),
                    "mid": snap.get("mid") or snap.get("price"),
                    "last": snap.get("last") or snap.get("price"),
                    "spread_bps": snap.get("spread_bps"),
                    "ts_ms": snap.get("ts_ms"),
                    "age_s": 0,
                    "source": env.source,
                    "_envelope": env.as_dict(),
                }
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

    venue = _venue_of(market)
    if venue in {"MOCK", "PAPER"} or resolve_allow_mock(allow_mock, config_like):
        price = mock_candles(market, count=1)[-1]["close"]
        return {
            "price": float(price),
            "age_s": 0,
            "source": "mock",
            "_envelope": mock_envelope(venue=(venue.lower() or "mock")).as_dict(),
        }
    return {
        "price": 0.0,
        "age_s": 0,
        "_envelope": degraded_envelope(
            "ticker",
            error=err or "no_live_ticker",
            venue=(venue.lower() or "unknown"),
        ).as_dict(),
    }


# ---------------------------------------------------------------- public fetch

def fetch_candles(market: str, *, count: int = 60, interval: str = "1m",
                   connector: Any | None = None,
                   registry: Any | None = None,
                   account_cfg: dict[str, Any] | None = None,
                   allow_mock: bool | None = None,
                   config_like: Any | None = None,
                   start: int | None = None,
                   end: int | None = None) -> list[dict[str, Any]]:
    """Fetch candles for ``market``.

    Resolution order:

    1. If a ``connector`` is supplied, use it directly.
    2. Else if ``registry`` + ``account_cfg`` are supplied, build one from
       the account and use it.
    3. Else parse the venue from ``VENUE:SYMBOL`` and try to build a
       public, read-only connector from the provider registry.
    4. On error, return mock candles only when mock mode is authorised;
       otherwise return ``[]`` tagged with a degraded envelope.
    """
    conn = connector
    venue = _venue_of(market)
    err = ""
    wallet_market_venues = set(_wallet_market_data_source_by_canonical().keys())
    if _canonical_venue(venue) in (wallet_market_venues | {"ONCHAIN"}):
        rows, wallet_err = _fetch_wallet_market_klines(
            market,
            interval=interval,
            count=count,
            config_like=config_like,
            start=start,
            end=end,
        )
        if rows:
            return rows
        env = degraded_envelope(
            "onchain_klines",
            error=wallet_err or "no_rows",
            venue=_canonical_venue(venue).lower(),
        )
        return tag_list_envelope([], env)
    if conn is None and registry is not None:
        try:
            if account_cfg is not None:
                conn = registry.get_or_build(account_cfg)
            else:
                from ..connectors.registry import build_connector
                conn = build_connector({"venue": venue, "kind": "cex"})
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            conn = None

    if conn is not None:
        try:
            rows = _connector_get_klines(
                conn,
                market,
                interval=interval,
                count=count,
                start=start,
                end=end,
            )
            norm = normalize_klines(getattr(conn, "venue", venue), rows)
            if norm:
                if start is not None or end is not None:
                    lo, hi = _window_from_count(
                        interval=interval,
                        count=count,
                        start=start,
                        end=end,
                    )
                    norm = [row for row in norm if lo <= int(row["ts"]) <= hi]
                env = live_envelope(
                    source=(getattr(conn, "venue", venue) or venue).lower(),
                    venue=(getattr(conn, "venue", venue) or venue).lower(),
                    connector_id=getattr(conn, "connector_id", ""),
                )
                return tag_list_envelope(_dedupe_sort_tail(norm, limit=count), env)
        except NotImplementedError as exc:
            err = f"NotImplementedError: {exc}"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

    for candidate in _candidate_venues(market, config_like):
        if candidate in {"MOCK", "PAPER"}:
            continue
        market_id = _market_for_venue(market, candidate)
        try:
            kwargs: dict[str, Any] = {"interval": interval, "count": count}
            if start is not None:
                kwargs["start"] = start
            if end is not None:
                kwargs["end"] = end
            norm = _fetch_public_rest_klines(
                candidate,
                market_id,
                **kwargs,
            )
            if norm:
                env = live_envelope(
                    source=f"{candidate.lower()}_rest",
                    venue=candidate.lower(),
                    connector_id="public_rest",
                )
                return tag_list_envelope(_dedupe_sort_tail(norm, limit=count), env)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

        try:
            from ..connectors.registry import build_connector

            c = build_connector({"venue": candidate.lower(), "kind": "cex", "live": False})
            rows = _connector_get_klines(
                c,
                market_id,
                interval=interval,
                count=count,
                start=start,
                end=end,
            )
            norm = normalize_klines(getattr(c, "venue", candidate), rows)
            if norm:
                if start is not None or end is not None:
                    lo, hi = _window_from_count(
                        interval=interval,
                        count=count,
                        start=start,
                        end=end,
                    )
                    norm = [row for row in norm if lo <= int(row["ts"]) <= hi]
                source = (getattr(c, "venue", candidate) or candidate).lower()
                env = live_envelope(
                    source=source,
                    venue=source,
                    connector_id=getattr(c, "connector_id", ""),
                )
                return tag_list_envelope(_dedupe_sort_tail(norm, limit=count), env)
        except NotImplementedError as exc:
            err = f"NotImplementedError: {exc}"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

        try:
            norm = _fetch_ccxt_klines(
                candidate,
                market_id,
                interval=interval,
                count=count,
                start=start,
                end=end,
            )
            if norm:
                env = live_envelope(
                    source=f"{candidate.lower()}_ccxt",
                    venue=candidate.lower(),
                    connector_id="ccxt",
                )
                return tag_list_envelope(_dedupe_sort_tail(norm, limit=count), env)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

    explicit_mock_prefix = venue in ("MOCK", "PAPER")
    if explicit_mock_prefix or resolve_allow_mock(allow_mock, config_like):
        if explicit_mock_prefix or resolve_allow_mock(allow_mock, config_like):
            rows = mock_candles(market, count=count)
            return tag_list_envelope(
                rows,
                mock_envelope(venue=(venue.lower() or "mock")),
            )

    env = degraded_envelope(
        "candles", error=err or "no_rows",
        venue=(venue.lower() or "unknown"),
    )
    return tag_list_envelope([], env)


def _venue_of(market: str) -> str:
    """Parse ``VENUE:SYMBOL`` — no silent MOCK default.

    Returns ``""`` for unprefixed markets so downstream code can emit a
    degraded envelope instead of fabricating synthetic candles for every
    caller who forgot to prefix their symbol.
    """
    if ":" in market:
        return market.split(":", 1)[0].upper()
    return ""


__all__ = [
    "mock_candles",
    "canonical_venue",
    "discover_market_data_sources",
    "fetch_candles",
    "fetch_public_ticker",
    "normalize_klines",
]
