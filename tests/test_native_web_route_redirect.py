from __future__ import annotations

import pytest

from nerya.tools.native.web import web_fetch_handler, web_search_fetch_handler, web_search_handler
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def test_web_search_redirects_native_meme_route_discovery() -> None:
    result = web_search_handler(
        ToolCall(
            name="web_search",
            arguments={
                "query": "Solana meme coin smart money tracking tools Birdeye DexScreener"
            },
        )
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
    assert "meme_strategy_guide" in result.text()
    assert "strategy_generate_proposal" in result.text()


def test_web_search_fetch_redirects_native_meme_route_discovery() -> None:
    result = web_search_fetch_handler(
        ToolCall(
            name="web_search_fetch",
            arguments={"query": "Solana on-chain wallet API data provider tracking"},
        )
    )

    assert result.is_error is True
    assert result.error is not None
    assert "strategy_author" in result.text()


def test_web_fetch_redirects_known_native_route_provider_pages() -> None:
    result = web_fetch_handler(
        ToolCall(
            name="web_fetch",
            arguments={"url": "https://api.dexscreener.com/latest/dex/tokens/abc"},
        )
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
