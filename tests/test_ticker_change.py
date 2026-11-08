"""Provider-native 24h fields survive the public snapshot, HTTP and tool paths."""

from types import SimpleNamespace

import pytest

from nerya.api.routes_market import routes
from nerya.data import candles
from nerya.tools.native import connectors
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def test_provider_ticker_change_reaches_dashboard_and_native_tool(monkeypatch):
    document = {"lastPrice": "101", "priceChange": "1", "priceChangePercent": "1.0"}
    urls = []

    def http(url):
        urls.append(url)
        return document

    monkeypatch.setattr(candles, "_http_json", http)
    monkeypatch.setattr(connectors, "_venue_has_public_market_feed", lambda *a, **k: True)
    # Legacy connector price must not short-circuit the native 24h REST fields.
    legacy = SimpleNamespace(bid=99, ask=101, mid=100, last=100, venue="binance", ts_ms=1)
    monkeypatch.setattr(connectors, "_public_connector", lambda *a, **k: SimpleNamespace(get_ticker=lambda m: legacy))
    ticker_route = next(handler for method, path, handler in routes() if path == "/market/ticker")

    for raw_change, raw_percentage, expected in [("1", "1.0", (1.0, 1.0)),
                                                  ("0", "0", (0.0, 0.0)),
                                                  (None, "NaN", (None, None))]:
        document.update(priceChange=raw_change, priceChangePercent=raw_percentage)
        snapshot = candles.fetch_public_ticker("BINANCE:BTC/USDT", allow_mock=False)
        dashboard = ticker_route(None, {"venue": "binance", "market": "BTC/USDT"})
        result = connectors.market_data_handler(ToolCall(name="market_data", arguments={
            "action": "get_ticker", "venue": "binance", "market": "BTC/USDT",
        }))
        assert not result.is_error
        native = next(part.data for part in result.content if part.type == "json")
        for payload in (snapshot, dashboard, native):
            assert (payload["change"], payload["percentage"]) == expected
            assert payload.get("last", payload.get("price")) == 101
            assert payload["_envelope"]["source"] == "binance_rest"
    assert all("/ticker/24hr?" in url for url in urls)

    document = {"result": {"list": [{"lastPrice": "101", "price24hPcnt": "-0.0125"}]}}
    bybit = candles.fetch_public_ticker("BYBIT:ETH/USDT", allow_mock=False)
    assert bybit["percentage"] == -1.25  # Provider ratio becomes percent points.
    assert bybit["change"] is None  # No absolute change is fabricated.


def test_okx_native_open24h_change_and_timestamp(monkeypatch):
    # Field shape from the installed ccxt.okx.parse_ticker standard payload.
    row = {"instId": "BTC-USDT", "last": "101", "open24h": "100", "ts": "1621440583784"}
    urls = []

    def http(url):
        urls.append(url)
        return {"code": "0", "data": [row]}

    monkeypatch.setattr(candles, "_http_json", http)
    monkeypatch.setattr(candles.time, "time", lambda: 1621440584.784)
    for last, opening, timestamp, expected in [
        ("101", "100", "1621440583784", (1.0, 1.0, 1621440583784)),
        ("99", "100", "1621440583784", (-1.0, -1.0, 1621440583784)),
        ("100", "100", "1621440583784", (0.0, 0.0, 1621440583784)),
        ("100", "0", None, (100.0, None, None)),
        ("100", None, "", (None, None, None)),
        ("100", "NaN", "NaN", (None, None, None)),
        ("100", "", "0", (None, None, None)),
        ("100", False, "-1", (None, None, None)),
        ("100", "Infinity", "1621440583784.5", (None, None, None)),
    ]:
        row.update(last=last, open24h=opening, ts=timestamp)
        result = candles.fetch_public_ticker("OKX:BTC/USDT", allow_mock=False)
        assert (result["change"], result["percentage"], result["ts_ms"]) == expected
        assert result["last"] == float(last)
        assert result["source"] == "okx_rest"
        assert result["age_s"] == pytest.approx(1.0) if expected[2] else result["age_s"] is None
    assert all("/api/v5/market/ticker?instId=BTC-USDT" in url for url in urls)
    # A zero native last remains zero at the producer boundary.
    row.update(last="0", open24h="100", ts=None)
    assert candles._fetch_public_rest_ticker("OKX", "BTC/USDT") == {
        "price": 0.0, "change": -100.0, "percentage": -100.0, "ts_ms": None,
    }
