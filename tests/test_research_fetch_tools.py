from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.skills.builtin.research.scripts import (
    _engine_config,
    _engines,
    fetch_url,
    search_fetch,
    web_search,
)
from nerya.skills.builtin.news_social.scripts import recent_news
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.registry import ToolRegistry


pytestmark = pytest.mark.smoke


def test_fetch_url_blocks_private_hosts() -> None:
    result = fetch_url.run(url="http://127.0.0.1:18317/health")

    assert result["ok"] is False
    assert result["safety"]["reason"] == "loopback"


def test_fetch_url_extracts_html_to_markdown_without_jina(monkeypatch) -> None:
    html = b"""
    <html>
      <head><title>Example Title</title><style>.x{}</style></head>
      <body>
        <article>
          <h1>Example Title</h1>
          <p>This is a useful paragraph about market structure and liquidity.</p>
          <p>It has enough detail to pass the minimum content threshold cleanly.</p>
        </article>
        <script>alert(1)</script>
      </body>
    </html>
    """

    def fake_http_get(url: str, **kwargs):
        assert url == "https://example.com/report"
        return 200, {"content-type": "text/html; charset=utf-8"}, html

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)

    result = fetch_url.run(
        url="https://example.com/report",
        use_jina_fallback=False,
        min_content_chars=20,
    )

    assert result["ok"] is True
    assert result["url"] == "https://example.com/report"
    assert result["fetch_method"] in {
        "trafilatura",
        "markdownify",
        "stdlib_html_text",
    }
    assert "useful paragraph" in result["markdown"]
    assert "<script>" not in result["markdown"]
    assert result["text"] == result["markdown"]


def test_fetch_url_detects_pdf_from_content_type_extension_or_magic() -> None:
    assert fetch_url._is_pdf_response(
        "application/pdf", "https://example.com/report", b"not-pdf",
    )
    assert fetch_url._is_pdf_response(
        "application/octet-stream", "https://example.com/report.PDF", b"x",
    )
    assert fetch_url._is_pdf_response(
        "application/octet-stream", "https://example.com/download", b"%PDF-1.7",
    )
    assert not fetch_url._is_pdf_response(
        "text/html", "https://example.com/report", b"<html>",
    )


def test_fetch_url_extracts_pdf_before_max_bytes_truncation(monkeypatch) -> None:
    body = b"%PDF-1.7\n" + (b"x" * 5000)
    extracted_lengths = []

    def fake_http_get(url: str, **kwargs):
        assert url == "https://example.com/filing.pdf"
        return 200, {"content-type": "application/pdf"}, body

    def fake_extract_pdf(received: bytes, *, url: str):
        extracted_lengths.append(len(received))
        assert url == "https://example.com/filing.pdf"
        return {
            "text": "Primary filing text",
            "title": "Annual filing",
            "pages_total": 12,
            "pages_read": 12,
            "truncated_pages": False,
        }

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)
    monkeypatch.setattr(fetch_url, "_extract_pdf", fake_extract_pdf)

    result = fetch_url.run(
        url="https://example.com/filing.pdf",
        max_bytes=1024,
        use_jina_fallback=False,
        use_browser_fallback=False,
        use_scrapling_fallback=False,
    )

    assert extracted_lengths == [len(body)]
    assert result["ok"] is True
    assert result["fetch_method"] == "direct_pdf_pypdf"
    assert result["markdown"] == "Primary filing text"
    assert result["pdf_pages_total"] == 12


def test_fetch_url_uses_jina_reader_for_low_quality_direct_html(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_get(url: str, **kwargs):
        calls.append(url)
        if url == "https://example.com/protected":
            return (
                200,
                {"content-type": "text/html"},
                b"<html><body>Please enable JavaScript.</body></html>",
            )
        if url == "https://r.jina.ai/https://example.com/protected":
            return (
                200,
                {"content-type": "text/plain"},
                b"Title: Protected Report\n\nReadable markdown body with enough detail.",
            )
        raise AssertionError(url)

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)

    result = fetch_url.run(
        url="https://example.com/protected",
        min_content_chars=30,
    )

    assert calls == [
        "https://example.com/protected",
        "https://r.jina.ai/https://example.com/protected",
    ]
    assert result["ok"] is True
    assert result["fetch_method"] == "jina_reader"
    assert result["direct_fetch_method"] == "stdlib_html_text"
    assert "Readable markdown body" in result["markdown"]
    assert result["reader_url"] == "https://r.jina.ai/https://example.com/protected"


def test_fetch_url_uses_browser_when_antibot_and_jina_are_low_quality(monkeypatch) -> None:
    from nerya.integrations import browser_engines

    calls: list[str] = []

    def fake_http_get(url: str, **kwargs):
        calls.append(url)
        if url == "https://example.com/antibot":
            return (
                200,
                {"content-type": "text/html"},
                b"<html><body>Cloudflare: verify you are human before continuing.</body></html>",
            )
        if url == "https://r.jina.ai/https://example.com/antibot":
            return (
                200,
                {"content-type": "text/plain"},
                b"Just a moment... Checking your browser before accessing the site.",
            )
        raise AssertionError(url)

    def fake_browser_fetch(workspace_root, *, url: str, timeout_s: float):
        assert url == "https://example.com/antibot"
        return {
            "ok": True,
            "name": "camofox",
            "fetch_method": "browser:camofox",
            "markdown": "Rendered article body with enough useful information for a summary.",
            "bytes": 64,
        }

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)
    monkeypatch.setattr(browser_engines, "fetch", fake_browser_fetch)

    result = fetch_url.run(
        url="https://example.com/antibot",
        min_content_chars=40,
        use_scrapling_fallback=False,
    )

    assert calls == [
        "https://example.com/antibot",
        "https://r.jina.ai/https://example.com/antibot",
    ]
    assert result["ok"] is True
    assert result["fetch_method"] == "browser:camofox"
    assert result["direct_fetch_method"] == "stdlib_html_text"
    assert "Rendered article body" in result["markdown"]
    assert any("jina_reader: low-quality content" in e for e in result["fallback_errors"])


def test_fetch_url_records_missing_browser_engine_when_antibot_page(monkeypatch) -> None:
    from nerya.integrations import browser_engines

    def fake_http_get(url: str, **kwargs):
        if url == "https://example.com/antibot":
            return (
                401,
                {"content-type": "text/html"},
                b"<html><body>Please enable JS and disable any ad blocker</body></html>",
            )
        if url == "https://r.jina.ai/https://example.com/antibot":
            return (
                200,
                {"content-type": "text/plain"},
                b"Security Verification",
            )
        raise AssertionError(url)

    def fake_browser_fetch(workspace_root, *, url: str, timeout_s: float):
        assert url == "https://example.com/antibot"
        return {"ok": False, "error": "no_engine_selected"}

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)
    monkeypatch.setattr(browser_engines, "fetch", fake_browser_fetch)

    result = fetch_url.run(
        url="https://example.com/antibot",
        min_content_chars=40,
        use_scrapling_fallback=False,
    )

    assert result["ok"] is False
    assert result["status"] == 401
    assert any("browser:?: no_engine_selected" in e for e in result["fallback_errors"])


def test_fetch_url_marks_blocker_page_failed_when_fallbacks_unusable(monkeypatch) -> None:
    def fake_http_get(url: str, **kwargs):
        if url == "https://finance.example/":
            return (
                200,
                {"content-type": "text/html"},
                b"<html><body>Oops, something went wrong. Please enable JS and disable any ad blocker.</body></html>",
            )
        if url == "https://r.jina.ai/https://finance.example/":
            return (
                200,
                {"content-type": "text/plain"},
                b"Security Verification",
            )
        raise AssertionError(url)

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)

    result = fetch_url.run(
        url="https://finance.example/",
        min_content_chars=40,
        use_browser_fallback=False,
        use_scrapling_fallback=False,
    )

    assert result["ok"] is False
    assert result["error"] == "low_quality_content"
    assert result["status"] == 200
    assert any("jina_reader: low-quality content" in e for e in result["fallback_errors"])
    assert any("direct_fetch: low-quality content" in e for e in result["fallback_errors"])


def test_fetch_url_marks_soft_404_page_failed(monkeypatch) -> None:
    def fake_http_get(url: str, **kwargs):
        if url == "https://example.com/missing":
            return (
                200,
                {"content-type": "text/html"},
                b"<html><head><title>404. Page Not Found - Example</title></head><body>404. Page Not Found</body></html>",
            )
        if url == "https://r.jina.ai/https://example.com/missing":
            return (
                200,
                {"content-type": "text/plain"},
                b"404. Page Not Found",
            )
        raise AssertionError(url)

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)

    result = fetch_url.run(
        url="https://example.com/missing",
        min_content_chars=20,
        use_browser_fallback=False,
        use_scrapling_fallback=False,
    )

    assert result["ok"] is False
    assert result["error"] == "low_quality_content"
    assert any("direct_fetch: low-quality content" in e for e in result["fallback_errors"])


def test_fetch_url_rejects_low_quality_jina_after_direct_timeout(monkeypatch) -> None:
    def fake_http_get(url: str, **kwargs):
        if url == "https://example.com/blocked":
            raise TimeoutError("timed out")
        if url == "https://r.jina.ai/https://example.com/blocked":
            return (
                200,
                {"content-type": "text/plain"},
                b"Title: Access Denied\n\nWarning: Target URL returned error 403: Forbidden\n\nMarkdown Content:\nYou don't have permission to access this server.",
            )
        raise AssertionError(url)

    monkeypatch.setattr(fetch_url, "http_get", fake_http_get)

    result = fetch_url.run(
        url="https://example.com/blocked",
        min_content_chars=40,
        use_browser_fallback=False,
        use_scrapling_fallback=False,
    )

    assert result["ok"] is False
    assert result["error"] == "TimeoutError: timed out"
    assert any("direct_fetch: TimeoutError" in e for e in result["fallback_errors"])
    assert any("jina_reader: low-quality content" in e for e in result["fallback_errors"])


def test_search_fetch_searches_then_fetches_top_results(monkeypatch) -> None:
    def fake_search_run(**kwargs):
        assert kwargs["query"] == "nerya web reader"
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 2,
            "results": [
                {
                    "title": "A",
                    "url": "https://a.example/report",
                    "snippet": "Snippet A",
                    "source": "fake",
                },
                {
                    "title": "B",
                    "url": "https://b.example/report",
                    "snippet": "Snippet B",
                    "source": "fake",
                },
            ],
        }

    def fake_fetch_run(**kwargs):
        return {
            "ok": True,
            "status": 200,
            "url": kwargs["url"],
            "title": "Fetched",
            "fetch_method": "trafilatura",
            "content_type": "text/html",
            "bytes": 123,
            "truncated": False,
            "markdown": f"Markdown for {kwargs['url']}",
            "fallback_errors": [],
            "safety": {"allowed": True},
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)

    result = search_fetch.run(query="nerya web reader", fetch_top_n=1)

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["documents"][0]["url"] == "https://a.example/report"
    assert result["documents"][0]["markdown"] == "Markdown for https://a.example/report"


def test_search_fetch_augments_empty_news_publisher_results_with_rss(
    monkeypatch,
) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 2,
            "results": [
                {
                    "title": "CoinDesk: Bitcoin and crypto news",
                    "url": "https://www.coindesk.com/",
                    "snippet": "Latest cryptocurrency headlines.",
                    "source": "duckduckgo_lite",
                },
                {
                    "title": "crypto.news",
                    "url": "https://crypto.news/",
                    "snippet": "Cryptocurrency news homepage.",
                    "source": "duckduckgo_lite",
                },
            ],
        }

    def fake_fetch_run(**kwargs):
        return {
            "ok": False,
            "status": 200,
            "url": kwargs["url"],
            "error": "low_quality_content",
            "fallback_errors": ["direct_fetch: low-quality content"],
        }

    def fake_recent_news_run(payload):
        assert payload["topic"] == "latest crypto news"
        return {
            "ok": True,
            "source": "rss",
            "sources": ["crypto_rss"],
            "items": [
                {
                    "source": "coindesk",
                    "title": "Bitcoin treasury firms add fresh holdings",
                    "summary": "A concise article summary from the feed.",
                    "url": "https://www.coindesk.com/markets/2026/06/06/bitcoin-treasury",
                    "published_at": "Sat, 06 Jun 2026 09:38:00 +0000",
                    "tickers": ["BTC"],
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)
    monkeypatch.setattr(
        search_fetch,
        "recent_news",
        SimpleNamespace(run=fake_recent_news_run),
        raising=False,
    )

    result = search_fetch.run(query="latest crypto news", fetch_top_n=2)

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["rss_fallback"]["ok"] is True
    assert result["rss_fallback"]["matched_count"] == 1
    assert result["documents"][0]["fetch_method"] == "rss_fallback"
    assert result["documents"][0]["source"] == "coindesk"
    assert "Sat, 06 Jun 2026" in result["documents"][0]["markdown"]
    assert "https://www.coindesk.com/markets/2026/06/06/bitcoin-treasury" in result["documents"][0]["markdown"]


def test_search_fetch_augments_low_quality_ticker_results_with_rss(
    monkeypatch,
) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 2,
            "results": [
                {
                    "title": "NVIDIA Newsroom",
                    "url": "https://nvidianews.nvidia.com/",
                    "snippet": "Homepage.",
                    "source": "searxng",
                },
                {
                    "title": "Apple stock is heading into WWDC",
                    "url": "https://www.cnbc.com/2026/06/05/apple-stock-wwdc.html",
                    "snippet": "Apple article.",
                    "source": "searxng",
                },
            ],
        }

    def fake_fetch_run(**kwargs):
        return {
            "ok": True,
            "status": 200,
            "url": kwargs["url"],
            "title": "Homepage",
            "markdown": "thin homepage",
            "error": "low_quality_content",
            "fallback_errors": ["direct_fetch: low-quality content"],
        }

    def fake_recent_news_run(payload):
        assert payload["topic"] == "AAPL 和 NVDA 今天有什么消息？"
        assert payload["tickers"] == ["AAPL", "NVDA"]
        return {
            "ok": True,
            "source": "rss",
            "sources": ["yahoo_finance_rss"],
            "tickers": ["AAPL", "NVDA"],
            "items": [
                {
                    "source": "yahoo_finance_rss",
                    "title": "AAPL and NVDA ticker headline",
                    "summary": "Ticker-specific RSS summary.",
                    "url": "https://finance.yahoo.com/news/apple-nvidia-headline-20260606.html",
                    "published_at": "Sat, 06 Jun 2026 10:00:00 +0000",
                    "tickers": ["AAPL", "NVDA"],
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)
    monkeypatch.setattr(
        search_fetch,
        "recent_news",
        SimpleNamespace(run=fake_recent_news_run),
        raising=False,
    )

    result = search_fetch.run(
        query="AAPL 和 NVDA 今天有什么消息？",
        fetch_top_n=2,
        min_content_chars=160,
    )

    assert result["ok"] is True
    assert result["rss_fallback"]["ok"] is True
    assert result["rss_fallback"]["reason"] == "no_readable_search_documents"
    assert result["rss_fallback"]["matched_count"] == 1
    assert result["documents"][0]["fetch_method"] == "rss_fallback"
    assert result["documents"][0]["title"] == "AAPL and NVDA ticker headline"
    assert result["documents"][0]["url"].startswith("https://finance.yahoo.com/news/")


def test_search_fetch_augments_readable_ticker_pages_with_relevant_rss(
    monkeypatch,
) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 1,
            "results": [
                {
                    "title": "Check out Apple's stock price (AAPL) in real time",
                    "url": "https://www.cnbc.com/quotes/AAPL",
                    "snippet": "AAPL quote page.",
                    "source": "searxng",
                }
            ],
        }

    def fake_fetch_run(**kwargs):
        return {
            "ok": True,
            "status": 200,
            "url": kwargs["url"],
            "title": "Check out Apple's stock price (AAPL) in real time",
            "markdown": "AAPL quote page with price snapshot and navigation." * 20,
            "fetch_method": "direct_html",
        }

    def fake_recent_news_run(payload):
        assert payload["tickers"] == ["AAPL"]
        return {
            "ok": True,
            "source": "rss",
            "sources": ["yahoo_finance_rss"],
            "tickers": ["AAPL"],
            "items": [
                {
                    "source": "yahoo_finance_rss",
                    "title": "Not SpaceX: satellite stocks quietly plugging into space",
                    "summary": "A broad market article that does not mention the requested ticker.",
                    "url": "https://247wallst.com/investing/2026/06/06/satellite-stocks/",
                    "published_at": "Sat, 06 Jun 2026 12:44:35 +0000",
                    "tickers": ["AAPL"],
                },
                {
                    "source": "yahoo_finance_rss",
                    "title": "AAPL supplier checks put Apple iPhone demand in focus",
                    "summary": "Analysts discuss Apple demand before the next product cycle.",
                    "url": "https://finance.yahoo.com/news/aapl-apple-demand-20260606.html",
                    "published_at": "Sat, 06 Jun 2026 13:00:00 +0000",
                    "tickers": ["AAPL"],
                },
            ],
            "errors": [],
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)
    monkeypatch.setattr(
        search_fetch,
        "recent_news",
        SimpleNamespace(run=fake_recent_news_run),
        raising=False,
    )

    result = search_fetch.run(
        query="AAPL Apple stock news today",
        fetch_top_n=1,
        min_content_chars=160,
    )

    assert result["ok"] is True
    assert result["rss_fallback"]["ok"] is True
    assert result["rss_fallback"]["reason"] == "ticker_news_augmentation"
    assert result["rss_fallback"]["matched_count"] == 1
    assert result["documents"][0]["fetch_method"] == "rss_fallback"
    assert result["documents"][0]["title"] == "AAPL supplier checks put Apple iPhone demand in focus"
    assert "satellite stocks" not in json.dumps(result["documents"], ensure_ascii=False)
    assert result["documents"][1]["url"] == "https://www.cnbc.com/quotes/AAPL"


def test_search_fetch_rejects_feed_tagged_ticker_article_without_query_evidence(
    monkeypatch,
) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 1,
            "results": [
                {
                    "title": "NVIDIA investor relations",
                    "url": "https://nvidianews.nvidia.com/",
                    "snippet": "NVIDIA newsroom.",
                    "source": "searxng",
                }
            ],
        }

    def fake_fetch_run(**kwargs):
        return {
            "ok": False,
            "status": 200,
            "url": kwargs["url"],
            "error": "low_quality_content",
            "fallback_errors": ["direct_fetch: low-quality content"],
        }

    def fake_recent_news_run(payload):
        assert payload["tickers"] == ["NVDA"]
        return {
            "ok": True,
            "source": "rss",
            "sources": ["yahoo_finance_rss"],
            "tickers": ["NVDA"],
            "items": [
                {
                    "source": "yahoo_finance_rss",
                    "title": (
                        "The No. 1 Reason to Buy and Hold Walmart Forever "
                        "Has Virtually Nothing to Do With Its Brick-and-Mortar Stores"
                    ),
                    "summary": "Retail membership revenue and store traffic.",
                    "url": "https://247wallst.com/investing/2026/06/06/walmart/",
                    "published_at": "Sat, 06 Jun 2026 14:43:44 +0000",
                    "tickers": ["NVDA"],
                },
                {
                    "source": "yahoo_finance_rss",
                    "title": "NVIDIA earnings: Data Center revenue jumps again",
                    "summary": "NVIDIA reported revenue and EPS metrics from the quarter.",
                    "url": "https://finance.yahoo.com/news/nvidia-earnings-20260606.html",
                    "published_at": "Sat, 06 Jun 2026 14:44:00 +0000",
                    "tickers": ["NVDA"],
                },
            ],
            "errors": [],
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)
    monkeypatch.setattr(
        search_fetch,
        "recent_news",
        SimpleNamespace(run=fake_recent_news_run),
        raising=False,
    )

    result = search_fetch.run(
        query="NVDA earnings key metrics revenue EPS guidance",
        fetch_top_n=2,
        min_content_chars=160,
    )

    assert result["ok"] is True
    assert result["rss_fallback"]["matched_count"] == 1
    assert result["documents"][0]["title"] == (
        "NVIDIA earnings: Data Center revenue jumps again"
    )
    assert "Walmart Forever" not in json.dumps(result["documents"], ensure_ascii=False)


def test_search_fetch_does_not_attach_unrelated_rss_without_source_overlap(
    monkeypatch,
) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 1,
            "results": [
                {
                    "title": "Python packaging guide",
                    "url": "https://packaging.python.org/en/latest/",
                    "snippet": "Python packaging documentation.",
                    "source": "duckduckgo_lite",
                }
            ],
        }

    def fake_fetch_run(**kwargs):
        return {
            "ok": False,
            "status": 200,
            "url": kwargs["url"],
            "error": "low_quality_content",
            "fallback_errors": ["direct_fetch: low-quality content"],
        }

    def fake_recent_news_run(payload):
        return {
            "ok": True,
            "source": "rss",
            "sources": ["crypto_rss"],
            "items": [
                {
                    "source": "coindesk",
                    "title": "Unrelated crypto headline",
                    "summary": "A feed item that must not be attached.",
                    "url": "https://www.coindesk.com/markets/2026/06/06/unrelated",
                    "published_at": "Sat, 06 Jun 2026 09:38:00 +0000",
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)
    monkeypatch.setattr(
        search_fetch,
        "recent_news",
        SimpleNamespace(run=fake_recent_news_run),
        raising=False,
    )

    result = search_fetch.run(query="python packaging", fetch_top_n=1)

    assert result["ok"] is False
    assert result["documents"] == []
    assert result["rss_fallback"]["ok"] is False
    assert result["rss_fallback"]["matched_count"] == 0


def test_search_fetch_defaults_to_browser_and_scrapling_fallbacks(monkeypatch) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 1,
            "results": [
                {
                    "title": "Blocked article",
                    "url": "https://blocked.example/article",
                    "snippet": "Needs rendered fallback",
                    "source": "fake",
                },
            ],
        }

    captured: dict[str, object] = {}

    def fake_fetch_run(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": 200,
            "url": kwargs["url"],
            "title": "Rendered",
            "fetch_method": "browser:camofox",
            "content_type": "text/html",
            "bytes": 512,
            "truncated": False,
            "markdown": "Rendered article body with enough evidence.",
            "fallback_errors": ["direct_fetch: low-quality content"],
            "safety": {"allowed": True},
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)

    result = search_fetch.run(query="latest market news", fetch_top_n=1)

    assert result["ok"] is True
    assert captured["use_browser_fallback"] is True
    assert captured["use_scrapling_fallback"] is True


def test_search_fetch_cli_passes_payload_fallbacks_and_base_urls(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "query": kwargs["query"], "documents": []}

    monkeypatch.setattr(search_fetch, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "search_fetch",
            "--json",
            (
                '{"query":"local probe","engines":["searxng"],'
                '"base_urls":{"searxng":"http://127.0.0.1:9999"},'
                '"use_browser_fallback":false,'
                '"use_scrapling_fallback":true}'
            ),
        ],
    )

    search_fetch.main()

    assert captured["query"] == "local probe"
    assert captured["engines"] == ["searxng"]
    assert captured["base_urls"] == {"searxng": "http://127.0.0.1:9999"}
    assert captured["use_browser_fallback"] is False
    assert captured["use_scrapling_fallback"] is True
    assert '"ok": true' in capsys.readouterr().out


def test_searxng_adapter_uses_module_level_urllib_request(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return (
                b'{"results":[{"title":"Result","url":"https://example.com",'
                b'"content":"Snippet","engine":"local"}]}'
            )

    captured: dict[str, object] = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(_engines.urllib.request, "urlopen", fake_urlopen)

    result = _engines.SearXNGAdapter(
        base_url="https://search.example",
        timeout_s=3,
    ).run(query="AI news", max_results=1, region="zh-CN")

    assert result == [
        {
            "title": "Result",
            "url": "https://example.com",
            "snippet": "Snippet",
            "source": "searxng",
            "engine": "searxng",
            "underlying_engine": "local",
            "key_index": -1,
        }
    ]
    assert "q=AI+news" in str(captured["url"])
    assert "language=zh-CN" in str(captured["url"])
    assert captured["timeout"] == 3


def test_searxng_adapter_maps_global_region_to_auto_language(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return (
                b'{"results":[{"title":"Result","url":"https://example.com",'
                b'"content":"Snippet","engine":"local"}]}'
            )

    captured: dict[str, object] = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(_engines.urllib.request, "urlopen", fake_urlopen)

    result = _engines.SearXNGAdapter(
        base_url="http://127.0.0.1:8888",
        timeout_s=3,
    ).run(query="NVDA export controls", max_results=1, region="wt-wt")

    assert result[0]["title"] == "Result"
    assert "language=auto" in str(captured["url"])
    assert captured["timeout"] == 3


def test_searxng_adapter_maps_duckduckgo_region_to_searxng_locale(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return (
                b'{"results":[{"title":"Result","url":"https://example.com",'
                b'"content":"Snippet","engine":"local"}]}'
            )

    captured: dict[str, object] = {}

    def fake_urlopen(req, *, timeout):
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(_engines.urllib.request, "urlopen", fake_urlopen)

    result = _engines.SearXNGAdapter(
        base_url="http://127.0.0.1:8888",
    ).run(query="NVDA export controls", max_results=1, region="us-en")

    assert result[0]["title"] == "Result"
    assert "language=en-US" in str(captured["url"])


def test_web_search_pinned_keyed_engine_without_key_falls_back_to_keyless(
    monkeypatch,
) -> None:
    resolve_calls: list[list[str] | None] = []

    def fake_resolve_config(**kwargs):
        requested = kwargs.get("engines")
        resolve_calls.append(list(requested or []) if requested is not None else None)
        if requested == ["brave"]:
            return _engine_config.SearchEngineConfig(
                engines=[_engine_config.EngineSpec(name="brave", keys=[])],
                sources={"chain": "kwargs"},
            )
        assert requested == ["searxng", "duckduckgo", "duckduckgo_lite"]
        return _engine_config.SearchEngineConfig(
            engines=[_engine_config.EngineSpec(name="duckduckgo_lite", keys=[])],
            sources={"chain": "kwargs"},
        )

    class FakeAdapter:
        def run(self, **kwargs):
            return [
                {
                    "title": "Fallback result",
                    "url": "https://example.com/2026/news",
                    "snippet": "Fallback via keyless search.",
                    "source": "duckduckgo_lite",
                    "engine": "duckduckgo_lite",
                    "key_index": -1,
                }
            ]

    def fake_build_adapter(name, keys, **kwargs):
        assert name == "duckduckgo_lite"
        assert keys == []
        return FakeAdapter()

    monkeypatch.setattr(web_search, "resolve_config", fake_resolve_config)
    monkeypatch.setattr(web_search, "build_adapter", fake_build_adapter)

    result = web_search.run(query="latest AI market news", engine="brave")

    assert result["ok"] is True
    assert result["engine"] == "duckduckgo_lite"
    assert result["engine_chain"] == ["brave", "duckduckgo_lite"]
    assert result["fallback_errors"] == ["brave: no API keys configured"]
    assert result["config_sources"]["fallback"] == "keyless"
    assert resolve_calls == [
        ["brave"],
        ["searxng", "duckduckgo", "duckduckgo_lite"],
    ]


def test_search_fetch_retries_and_skips_failed_candidates(monkeypatch) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 3,
            "results": [
                {"title": "Blocked", "url": "https://blocked.example/report", "snippet": "Blocked", "source": "fake"},
                {"title": "Slow", "url": "https://slow.example/report", "snippet": "Slow", "source": "fake"},
                {"title": "Good", "url": "https://good.example/report", "snippet": "Good", "source": "fake"},
            ],
        }

    calls: list[tuple[str, bool, float]] = []

    def fake_fetch_run(**kwargs):
        calls.append((kwargs["url"], bool(kwargs.get("prefer_jina")), float(kwargs["timeout_s"])))
        if kwargs["url"] == "https://blocked.example/report":
            return {
                "ok": False,
                "status": 401,
                "url": kwargs["url"],
                "error": "low_quality_content",
                "fallback_errors": ["direct_fetch: low-quality content (20 chars)"],
            }
        if kwargs["url"] == "https://slow.example/report" and not kwargs.get("prefer_jina"):
            return {
                "ok": False,
                "status": None,
                "url": kwargs["url"],
                "error": "TimeoutError",
                "fallback_errors": ["jina_reader: TimeoutError"],
            }
        return {
            "ok": True,
            "status": 200,
            "url": kwargs["url"],
            "title": "Fetched",
            "fetch_method": "jina_reader" if kwargs.get("prefer_jina") else "markdownify",
            "content_type": "text/plain",
            "bytes": 123,
            "truncated": False,
            "markdown": f"Markdown for {kwargs['url']}",
            "fallback_errors": [],
            "safety": {"allowed": True},
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)

    result = search_fetch.run(query="market news", fetch_top_n=2, timeout_s=5)

    assert result["ok"] is True
    assert result["count"] == 2
    assert [doc["url"] for doc in result["documents"]] == [
        "https://slow.example/report",
        "https://good.example/report",
    ]
    assert result["documents"][0]["fetch_method"] == "jina_reader"
    assert result["documents"][0]["fallback_errors"][0] == "retry_after_failed_fetch: prefer_jina"
    assert result["fetch_errors"] == [
        {
            "rank": 1,
            "url": "https://blocked.example/report",
            "error": "low_quality_content",
            "fallback_errors": ["direct_fetch: low-quality content (20 chars)"],
        }
    ]
    slow_retry_timeouts = [
        timeout
        for url, prefer_jina, timeout in calls
        if url == "https://slow.example/report" and prefer_jina
    ]
    assert slow_retry_timeouts
    assert 0 < slow_retry_timeouts[0] <= 10.0


def test_search_fetch_stops_when_fetch_budget_is_exhausted(monkeypatch) -> None:
    def fake_search_run(**kwargs):
        return {
            "ok": True,
            "query": kwargs["query"],
            "count": 2,
            "results": [
                {"title": "Slow", "url": "https://slow.example/report", "snippet": "Slow", "source": "fake"},
                {"title": "Later", "url": "https://later.example/report", "snippet": "Later", "source": "fake"},
            ],
        }

    fake_now = {"value": 100.0}

    def fake_monotonic() -> float:
        return fake_now["value"]

    def fake_fetch_run(**kwargs):
        fake_now["value"] += 11.0
        return {
            "ok": False,
            "status": None,
            "url": kwargs["url"],
            "error": "TimeoutError",
            "fallback_errors": ["direct_fetch: TimeoutError"],
        }

    monkeypatch.setattr(search_fetch.web_search, "run", fake_search_run)
    monkeypatch.setattr(search_fetch.fetch_url, "run", fake_fetch_run)
    monkeypatch.setattr(search_fetch.time, "monotonic", fake_monotonic)

    result = search_fetch.run(query="market news", fetch_top_n=2, timeout_s=5)

    assert result["ok"] is False
    assert result["budget_exhausted"] is True
    assert result["fetch_errors"][-1]["error"] == "budget_exhausted"


def test_native_registry_exposes_web_research_tools(tmp_path) -> None:
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[],
    )
    register_native_tools(registry, deps)

    names = {tool.name for tool in registry.list_tools()}

    assert {"web_search", "web_fetch", "web_search_fetch"} <= names
    assert "news_recent" not in names
    web_fetch = registry.get("web_fetch")
    assert web_fetch.permission_scope.value == "network"
    assert web_fetch.auto_approve is True


def test_news_social_skill_is_lazy_loaded_not_native_tool(tmp_path) -> None:
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"],
    )

    record = deps.skill_index.get("news_social", refresh=True)

    assert record is not None
    assert "economy/finance/market news" in record.description
    assert "热门经济新闻" in record.triggers
    assert record.permissions == ["network"]
    assert record.has_scripts is True
    assert "recent_news.py" in record.scripts


def test_news_social_recent_news_routes_finance_to_yahoo_rss(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_yahoo(tickers, *, limit):
        calls.append(("yahoo", tuple(tickers)))
        return {
            "items": [
                {
                    "source": "yahoo_finance_rss",
                    "title": "Market headline",
                    "summary": "Summary",
                    "url": "https://example.com/market",
                    "published_at": "Wed, 20 May 2026 00:00:00 GMT",
                    "tickers": ["^GSPC"],
                }
            ],
            "errors": [],
        }

    def fake_crypto(*args, **kwargs):
        calls.append(("crypto", kwargs))
        return []

    monkeypatch.setattr(recent_news, "_fetch_yahoo_rss", fake_yahoo)
    monkeypatch.setattr(recent_news, "fetch_news", fake_crypto)

    result = recent_news.run({"topic": "热门财经新闻", "limit": 5})

    assert result["ok"] is True
    assert result["sources"] == ["yahoo_finance_rss"]
    assert result["items"][0]["title"] == "Market headline"
    assert calls == [("yahoo", ("^GSPC", "^IXIC", "^DJI"))]


def test_news_social_recent_news_extracts_equity_tickers_from_topic(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def fake_yahoo(tickers, *, limit):
        calls.append(("yahoo", tuple(tickers)))
        return {
            "items": [
                {
                    "source": "yahoo_finance_rss",
                    "title": "AAPL headline",
                    "summary": "Summary",
                    "url": "https://finance.yahoo.com/news/aapl",
                    "published_at": "Sat, 06 Jun 2026 10:00:00 +0000",
                    "tickers": ["AAPL"],
                },
                {
                    "source": "yahoo_finance_rss",
                    "title": "NVDA headline",
                    "summary": "Summary",
                    "url": "https://finance.yahoo.com/news/nvda",
                    "published_at": "Sat, 06 Jun 2026 10:10:00 +0000",
                    "tickers": ["NVDA"],
                },
            ],
            "errors": [],
        }

    def fake_crypto(*args, **kwargs):
        raise AssertionError("explicit equity tickers should not call crypto rss")

    monkeypatch.setattr(recent_news, "_fetch_yahoo_rss", fake_yahoo)
    monkeypatch.setattr(recent_news, "fetch_news", fake_crypto)

    result = recent_news.run({"topic": "AAPL 和 NVDA 今天有什么消息？", "limit": 5})

    assert result["ok"] is True
    assert result["sources"] == ["yahoo_finance_rss"]
    assert result["tickers"] == ["AAPL", "NVDA"]
    assert [item["title"] for item in result["items"]] == [
        "AAPL headline",
        "NVDA headline",
    ]
    assert calls == [("yahoo", ("AAPL", "NVDA"))]


def test_news_social_recent_news_routes_explicit_crypto_feed_names(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_yahoo(*args, **kwargs):
        raise AssertionError("crypto feed aliases should not call yahoo rss")

    def fake_crypto(*args, **kwargs):
        calls.append(dict(kwargs))
        return [
            {
                "source": "coindesk",
                "title": "Crypto headline",
                "summary": "Summary",
                "url": "https://www.coindesk.com/markets/2026/06/06/btc",
                "published_at": "Sat, 06 Jun 2026 09:38:00 +0000",
                "tickers": [],
            }
        ]

    monkeypatch.setattr(recent_news, "_fetch_yahoo_rss", fake_yahoo)
    monkeypatch.setattr(recent_news, "fetch_news", fake_crypto)

    result = recent_news.run({
        "sources": [
            "coindesk_rss",
            "cointelegraph_rss",
            "bitcoinmagazine_rss",
        ],
        "limit": 5,
    })

    assert result["ok"] is True
    assert result["sources"] == ["crypto_rss"]
    assert result["items"][0]["source"] == "coindesk"
    assert calls == [{"limit": 5, "allow_mock": False}]


def test_news_social_recent_news_filters_inferred_recent_hour_window(
    monkeypatch,
) -> None:
    def fake_yahoo(*args, **kwargs):
        raise AssertionError("crypto topic should not call yahoo rss")

    def fake_crypto(*args, **kwargs):
        return [
            {
                "source": "coindesk",
                "title": "Inside the three hour window",
                "summary": "Recent summary",
                "url": "https://www.coindesk.com/markets/2026/06/06/recent",
                "published_at": "Sat, 06 Jun 2026 10:45:00 +0000",
                "tickers": ["BTC"],
            },
            {
                "source": "coindesk",
                "title": "Outside the three hour window",
                "summary": "Old summary",
                "url": "https://www.coindesk.com/markets/2026/06/06/old",
                "published_at": "Sat, 06 Jun 2026 07:20:00 +0000",
                "tickers": ["ETH"],
            },
            {
                "source": "coindesk",
                "title": "Missing timestamp",
                "summary": "Should not satisfy a strict recent window",
                "url": "https://www.coindesk.com/markets/2026/06/06/missing",
                "published_at": "",
                "tickers": [],
            },
        ]

    monkeypatch.setattr(recent_news, "_fetch_yahoo_rss", fake_yahoo)
    monkeypatch.setattr(recent_news, "fetch_news", fake_crypto)

    result = recent_news.run({
        "topic": "最近 3 小时的加密新闻",
        "limit": 10,
        "now": "2026-06-06T11:30:00+00:00",
    })

    assert result["ok"] is True
    assert [item["title"] for item in result["items"]] == [
        "Inside the three hour window"
    ]
    assert result["time_filter"]["lookback_hours"] == 3.0
    assert result["time_filter"]["dropped_count"] == 2
    assert result["time_filter"]["missing_timestamp_count"] == 1


def test_news_social_payload_file_dash_reads_stdin(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"topic":"crypto","limit":3}'))

    payload = recent_news._load_payload(
        argparse.Namespace(payload_file="-", payload_json=None)
    )

    assert payload == {"topic": "crypto", "limit": 3}


def test_news_social_recent_news_loads_custom_workspace_feeds(tmp_path, monkeypatch) -> None:
    (tmp_path / "news_feeds.yml").write_text(
        "feeds:\n"
        "  - id: example\n"
        "    url: https://example.com/feed.xml\n"
        "    type: rss\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    calls: list[list[dict[str, str]]] = []

    def fake_fetch_news(*, limit, sources=None, **kwargs):
        calls.append(list(sources or []))
        return [
            {
                "source": "example",
                "title": "Example headline",
                "body": "Summary",
                "link": "https://example.com/post",
                "published_at": "Wed, 20 May 2026 00:00:00 GMT",
                "tickers": [],
            }
        ]

    monkeypatch.setenv("NERYA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(recent_news, "fetch_news", fake_fetch_news)

    result = recent_news.run({"sources": ["custom_rss"], "limit": 5})

    assert result["ok"] is True
    assert result["sources"] == ["custom_rss"]
    assert result["custom_feed_count"] == 1
    assert calls == [[{"name": "example", "url": "https://example.com/feed.xml"}]]
    assert result["items"][0]["source"] == "example"


def test_news_social_recent_news_records_feed_errors_without_crashing(
    monkeypatch,
) -> None:
    def fake_yahoo(tickers, *, limit):
        return {"items": [], "errors": ["yahoo_rss:no_items"]}

    def fake_fetch_news(*args, **kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(recent_news, "_fetch_yahoo_rss", fake_yahoo)
    monkeypatch.setattr(recent_news, "fetch_news", fake_fetch_news)

    result = recent_news.run({"topic": "crypto bitcoin defi", "limit": 5})

    assert result["ok"] is False
    assert "crypto_rss:RuntimeError" in result["errors"]
