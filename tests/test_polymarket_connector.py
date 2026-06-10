from __future__ import annotations

from typing import Any

from nerya.connectors.polymarket import PolymarketConnector


TOKEN_ID = "111128191581505463501777127559667396812474366956707382672202929745167742497287"


class FakePolymarketHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        self.calls.append((method, url, dict(params or {})))
        if url.endswith("/markets"):
            return 200, [
                {
                    "slug": "event-slug",
                    "question": "Will this test pass?",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": f'["{TOKEN_ID}", "2222222222222222222222222"]',
                }
            ]
        if url.endswith("/book"):
            return 200, {
                "bids": [
                    {"price": "0.10", "size": "10"},
                    {"price": "0.30", "size": "2"},
                ],
                "asks": [
                    {"price": "0.45", "size": "5"},
                    {"price": "0.35", "size": "3"},
                ],
                "last_trade_price": "0.31",
            }
        if url.endswith("/prices-history"):
            if "clob.test" not in url:
                return 404, {"error": "wrong host"}
            return 200, {
                "history": [
                    {"t": 1_700_000_000, "p": "0.20"},
                    {"t": 1_700_003_600, "p": "0.25"},
                ]
            }
        return 500, {"error": f"unexpected url {url}"}


def _connector(transport: FakePolymarketHttp) -> PolymarketConnector:
    return PolymarketConnector(
        transport=transport,
        clob_url="https://clob.test",
        gamma_url="https://gamma.test",
        data_url="https://data.test",
    )


def test_polymarket_accepts_decimal_clob_token_id_without_gamma_lookup() -> None:
    transport = FakePolymarketHttp()
    connector = _connector(transport)

    book = connector.get_order_book(f"POLYMARKET:{TOKEN_ID}")
    ticker = connector.get_ticker(TOKEN_ID)

    assert not any(url.endswith("/markets") for _, url, _ in transport.calls)
    assert book["bid"] == 0.30
    assert book["ask"] == 0.35
    assert ticker.bid == 0.30
    assert ticker.ask == 0.35
    assert ticker.last == 0.31


def test_polymarket_slug_resolves_to_clob_token_id() -> None:
    transport = FakePolymarketHttp()
    connector = _connector(transport)

    ticker = connector.get_ticker("POLYMARKET:event-slug")

    assert ticker.mid == 0.325
    market_calls = [params for _, url, params in transport.calls if url.endswith("/markets")]
    book_calls = [params for _, url, params in transport.calls if url.endswith("/book")]
    assert market_calls == [{"slug": "event-slug"}]
    assert book_calls == [{"token_id": TOKEN_ID}]


def test_polymarket_klines_use_clob_prices_history() -> None:
    transport = FakePolymarketHttp()
    connector = _connector(transport)

    rows = connector.get_klines(TOKEN_ID, interval="1h", limit=2)

    price_history_calls = [
        (url, params) for _, url, params in transport.calls if url.endswith("/prices-history")
    ]
    assert price_history_calls == [
        (
            "https://clob.test/prices-history",
            {"market": TOKEN_ID, "interval": "1d", "fidelity": 2},
        )
    ]
    assert rows == [
        [1_700_000_000_000, 0.20, 0.20, 0.20, 0.20, 0.0],
        [1_700_003_600_000, 0.20, 0.25, 0.20, 0.25, 0.0],
    ]
