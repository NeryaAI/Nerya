from __future__ import annotations

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.skills.builtin.research.scripts import fetch_url, search_fetch
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


def test_native_registry_exposes_web_research_tools(tmp_path) -> None:
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[],
    )
    register_native_tools(registry, deps)

    names = {tool.name for tool in registry.list_tools()}

    assert {"web_search", "web_fetch", "web_search_fetch"} <= names
    web_fetch = registry.get("web_fetch")
    assert web_fetch.permission_scope.value == "network"
    assert web_fetch.auto_approve is True
