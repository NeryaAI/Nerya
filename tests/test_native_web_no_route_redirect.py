from __future__ import annotations

import pytest

from nerya.tools.native import web as native_web
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def test_web_search_does_not_route_by_query_keywords(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "results": []}

    monkeypatch.setattr(native_web.web_search, "run", fake_run)

    result = native_web.web_search_handler(
        ToolCall(
            name="web_search",
            arguments={
                "query": "Solana meme coin smart money tracking tools Birdeye DexScreener"
            },
        )
    )

    assert result.is_error is False
    assert captured["query"] == (
        "Solana meme coin smart money tracking tools Birdeye DexScreener"
    )


def test_web_search_fetch_does_not_route_by_query_keywords(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "documents": []}

    monkeypatch.setattr(native_web.search_fetch, "run", fake_run)

    result = native_web.web_search_fetch_handler(
        ToolCall(
            name="web_search_fetch",
            arguments={"query": "Solana on-chain wallet API data provider tracking"},
        )
    )

    assert result.is_error is False
    assert captured["query"] == "Solana on-chain wallet API data provider tracking"
    assert captured["use_browser_fallback"] is True
    assert captured["use_scrapling_fallback"] is True


def test_web_fetch_does_not_route_by_url_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "url": kwargs.get("url"), "text": ""}

    monkeypatch.setattr(native_web.fetch_url, "run", fake_run)

    result = native_web.web_fetch_handler(
        ToolCall(
            name="web_fetch",
            arguments={"url": "https://api.dexscreener.com/latest/dex/tokens/abc"},
        )
    )

    assert result.is_error is False
    assert captured["url"] == "https://api.dexscreener.com/latest/dex/tokens/abc"
