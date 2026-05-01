"""Yahoo Finance public market-data connector.

Reads US equity / ETF / index / FX / crypto quotes and candles from the
public Yahoo Finance chart API and exposes them through Nerya's small
``Connector`` interface.

This connector is intentionally read-only: Yahoo Finance is a data source,
not an execution venue, so balances and order placement remain unsupported.

Supported market shapes:

* ``AAPL``
* ``yahoo:AAPL``
* ``NASDAQ:AAPL`` / ``NYSE:MSFT`` / ``AMEX:SPY``
* ``BRK.B`` / ``BRK-B`` (normalised to Yahoo's ``BRK-B``)
* ``^GSPC`` / ``^IXIC``
* ``BTC-USD`` / ``EURUSD=X``
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
from .base import Connector, Ticker
from .http import HttpTransport, UrllibHttp


CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"

_INTERVAL_MAP: dict[str, str] = {
    "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
    "60m": "60m", "1h": "60m", "90m": "90m",
    "1d": "1d", "1wk": "1wk", "1w": "1wk", "1mo": "1mo", "1M": "1mo",
}

_RANGE_BY_INTERVAL: list[tuple[set[str], str]] = [
    ({"1m"}, "7d"),
    ({"2m", "5m", "15m", "30m", "60m", "90m"}, "60d"),
    ({"1d"}, "1y"),
    ({"1wk"}, "5y"),
    ({"1mo"}, "10y"),
]


@dataclass
class YahooFinanceConnector(Connector):
    venue: str = "YAHOO"
    transport: HttpTransport = field(default_factory=UrllibHttp)
    chart_url: str = CHART_URL
    search_url: str = SEARCH_URL
    timeout: float = 12.0
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _normalise_symbol(self, market: str) -> str:
        raw = (market or "").strip()
        if not raw:
            raise TradingError("yahoo: market is empty")
        if ":" in raw:
            left, right = raw.split(":", 1)
            left = left.strip().lower()
            if left == "yahoo":
                raw = right.strip()
            elif left in {"nasdaq", "nyse", "amex", "arca", "bats", "otc"}:
                raw = right.strip()
        raw = raw.strip()
        if raw.startswith("^") or raw.endswith("=X") or raw.endswith("-USD"):
            return raw.upper()
        raw = raw.replace("/", "-").replace(".", "-")
        return raw.upper()

    def _get(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        status, doc = self.transport.request("GET", url, params=params, timeout=self.timeout)
        if status >= 400:
            raise TradingError(f"yahoo GET {url} {status}: {doc}")
        if not isinstance(doc, dict):
            raise TradingError(f"yahoo: unexpected response type {type(doc).__name__}")
        return doc

    def _range_for_interval(self, interval: str) -> str:
        for keys, rng in _RANGE_BY_INTERVAL:
            if interval in keys:
                return rng
        return "1y"

    def _extract_result(self, symbol: str, doc: dict[str, Any]) -> dict[str, Any]:
        chart = doc.get("chart") or {}
        error = chart.get("error")
        if error:
            raise TradingError(f"yahoo chart error for {symbol}: {error}")
        result = chart.get("result") or []
        if not result:
            raise TradingError(f"yahoo: no chart result for {symbol}")
        row = result[0] or {}
        meta = row.get("meta") or {}
        if not meta:
            raise TradingError(f"yahoo: missing meta for {symbol}")
        return row

    def _fetch_chart(self, symbol: str, *, interval: str, range_: str | None = None) -> dict[str, Any]:
        iv = _INTERVAL_MAP.get(interval, interval)
        params = {
            "interval": iv,
            "range": range_ or self._range_for_interval(iv),
            "includePrePost": "false",
            "events": "div,splits",
        }
        return self._extract_result(symbol, self._get(f"{self.chart_url.rstrip('/')}/{symbol}", params=params))

    def _quote_fields(self, symbol: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self._fetch_chart(symbol, interval="1d", range_="5d")
        meta = row.get("meta") or {}
        indicators = row.get("indicators") or {}
        quote = ((indicators.get("quote") or [{}])[0]) or {}
        return meta, quote

    def _safe_float(self, v: Any) -> float | None:
        try:
            if v is None:
                return None
            f = float(v)
            if math.isnan(f):
                return None
            return f
        except Exception:
            return None

    def get_ticker(self, market: str) -> Ticker:
        symbol = self._normalise_symbol(market)
        meta, quote = self._quote_fields(symbol)
        bid = self._safe_float(meta.get("bid"))
        ask = self._safe_float(meta.get("ask"))
        last = self._safe_float(meta.get("regularMarketPrice"))
        if last is None:
            closes = quote.get("close") or []
            for v in reversed(closes):
                last = self._safe_float(v)
                if last is not None:
                    break
        if last is None:
            raise TradingError(f"yahoo: no last price for {symbol}")
        if bid is None and ask is None:
            bid = last
            ask = last
        elif bid is None:
            bid = ask if ask is not None else last
        elif ask is None:
            ask = bid
        mid = (float(bid) + float(ask)) / 2.0
        spread_bps = 0.0 if mid <= 0 else max(0.0, (float(ask) - float(bid)) / mid * 10000.0)
        ts = int((self._safe_float(meta.get("regularMarketTime")) or time.time()) * 1000)
        self._cache[symbol] = {"meta": meta, "quote": quote, "ts_ms": ts}
        return Ticker(
            market=market,
            bid=float(bid),
            ask=float(ask),
            mid=float(mid),
            last=float(last),
            spread_bps=float(spread_bps),
            ts_ms=ts,
            venue=self.venue,
        )

    def get_order_book(self, market: str) -> dict[str, Any]:
        t = self.get_ticker(market)
        return {
            "market": t.market,
            "bid": t.bid,
            "ask": t.ask,
            "mid": t.mid,
            "spread_bps": t.spread_bps,
            "ts_ms": t.ts_ms,
            "venue": t.venue,
            "bids": [[t.bid, 1.0]],
            "asks": [[t.ask, 1.0]],
            "synthetic": True,
        }

    def get_klines(self, market: str, *, interval: str = "1d", limit: int = 100) -> list[list[Any]]:
        symbol = self._normalise_symbol(market)
        iv = _INTERVAL_MAP.get(interval, interval)
        row = self._fetch_chart(symbol, interval=iv)
        timestamps = row.get("timestamp") or []
        indicators = row.get("indicators") or {}
        quote = ((indicators.get("quote") or [{}])[0]) or {}
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        out: list[list[Any]] = []
        for i, ts in enumerate(timestamps):
            o = self._safe_float(opens[i] if i < len(opens) else None)
            h = self._safe_float(highs[i] if i < len(highs) else None)
            l = self._safe_float(lows[i] if i < len(lows) else None)
            c = self._safe_float(closes[i] if i < len(closes) else None)
            if None in (o, h, l, c):
                continue
            v = self._safe_float(volumes[i] if i < len(volumes) else 0) or 0.0
            out.append([int(ts) * 1000, float(o), float(h), float(l), float(c), float(v)])
        if limit > 0:
            out = out[-limit:]
        return out
