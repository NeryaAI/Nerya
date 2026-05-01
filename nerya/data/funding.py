"""Perp funding-rate — real CEX public endpoints, no auth required.

Supported routing via market prefix:

    ``BINANCE:BTCUSDT``   → ``fapi.binance.com/fapi/v1/premiumIndex``
    ``BYBIT:BTCUSDT``     → ``api.bybit.com/v5/market/tickers``
    ``OKX:BTC-USDT-SWAP`` → ``www.okx.com/api/v5/public/funding-rate``

Unknown prefix / failure → :func:`mock_funding`.
"""

from __future__ import annotations

import logging
import time

from ..connectors.http import HttpTransport, UrllibHttp
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)

log = logging.getLogger(__name__)


def _split_prefix(market: str) -> tuple[str, str]:
    if ":" in market:
        v, sym = market.split(":", 1)
        return v.upper(), sym
    return "BINANCE", market


def _fetch_binance(symbol: str, http: HttpTransport) -> dict | None:
    status, body = http.request(
        "GET", "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": symbol.upper().replace("-", "")}, timeout=10.0,
    )
    if status >= 400 or not isinstance(body, dict):
        return None
    rate = float(body.get("lastFundingRate") or 0.0)
    next_ms = int(body.get("nextFundingTime") or 0)
    next_in = max(0, int(next_ms / 1000 - time.time())) if next_ms else 0
    return {"funding_rate": rate, "next_funding_in_s": next_in,
            "mark_price": float(body.get("markPrice") or 0.0)}


def _fetch_bybit(symbol: str, http: HttpTransport) -> dict | None:
    status, body = http.request(
        "GET", "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear",
                 "symbol": symbol.upper().replace("-", "")},
        timeout=10.0,
    )
    if status >= 400 or not isinstance(body, dict):
        return None
    rows = ((body.get("result") or {}).get("list")) or []
    if not rows:
        return None
    row = rows[0]
    rate = float(row.get("fundingRate") or 0.0)
    next_ms = int(row.get("nextFundingTime") or 0)
    next_in = max(0, int(next_ms / 1000 - time.time())) if next_ms else 0
    return {"funding_rate": rate, "next_funding_in_s": next_in,
            "mark_price": float(row.get("markPrice") or 0.0)}


def _fetch_okx(symbol: str, http: HttpTransport) -> dict | None:
    status, body = http.request(
        "GET", "https://www.okx.com/api/v5/public/funding-rate",
        params={"instId": symbol.upper()}, timeout=10.0,
    )
    if status >= 400 or not isinstance(body, dict):
        return None
    data = body.get("data") or []
    if not data:
        return None
    row = data[0]
    rate = float(row.get("fundingRate") or 0.0)
    next_ms = int(row.get("nextFundingTime") or 0)
    next_in = max(0, int(next_ms / 1000 - time.time())) if next_ms else 0
    return {"funding_rate": rate, "next_funding_in_s": next_in,
            "mark_price": 0.0}


_FETCHERS = {
    "BINANCE": _fetch_binance,
    "BYBIT": _fetch_bybit,
    "OKX": _fetch_okx,
}


def fetch_funding(
    market: str,
    *,
    transport: HttpTransport | None = None,
    allow_mock: bool | None = None,
    config_like=None,
) -> dict:
    """Fetch the current funding rate.

    Returns mock data only when mock mode is explicitly authorised; otherwise
    returns an unavailable envelope with ``funding_rate = 0.0`` and a
    ``_envelope`` truth marker.
    """
    venue, symbol = _split_prefix(market)
    fetcher = _FETCHERS.get(venue)

    def _degraded(err: str) -> dict:
        if resolve_allow_mock(allow_mock, config_like):
            return mock_funding(market)
        return {
            "market": market, "venue": "unavailable",
            "funding_rate": 0.0, "next_funding_in_s": 0, "mark_price": 0.0,
            "source": "unavailable",
            "_envelope": degraded_envelope(
                "funding", error=err, venue=venue.lower()
            ).as_dict(),
        }

    if fetcher is None:
        return _degraded("unknown_venue")
    http = transport or UrllibHttp(rate_limit_per_sec=5.0)
    try:
        result = fetcher(symbol, http)
    except Exception as exc:
        log.debug("funding fetch failed %s: %s", market, exc)
        return _degraded(f"{type(exc).__name__}")
    if not result:
        return _degraded("empty_result")
    out = {"market": market, "venue": venue.lower(),
            "source": venue.lower(), **result}
    out["_envelope"] = live_envelope(source=venue.lower(),
                                      venue=venue.lower()).as_dict()
    return out


def mock_funding(market: str) -> dict:
    return {
        "market": market,
        "venue": "mock",
        "funding_rate": 0.0001,
        "next_funding_in_s": 3600,
        "mark_price": 0.0,
        "source": "mock",
        "_envelope": mock_envelope(source="mock").as_dict(),
    }


__all__ = ["fetch_funding", "mock_funding"]
