"""Native web research tools backed by the research skill scripts."""

from __future__ import annotations

from typing import Any

from ...skills.builtin.research.scripts import fetch_url, search_fetch, web_search
from ..types import ToolCall, ToolResult


WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query."},
        "max_results": {"type": "integer", "minimum": 1, "default": 8},
        "region": {"type": "string", "default": "wt-wt"},
        "safesearch": {
            "type": "string",
            "enum": ["strict", "moderate", "off"],
            "default": "moderate",
        },
        "engine": {
            "type": "string",
            "enum": ["duckduckgo", "duckduckgo_html", "duckduckgo_lite"],
            "default": "duckduckgo",
        },
    },
    "required": ["query"],
}

WEB_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "HTTP(S) URL to fetch."},
        "strip_html": {"type": "boolean", "default": True},
        "max_bytes": {"type": "integer", "minimum": 1024, "default": 200000},
        "timeout_s": {"type": "number", "minimum": 1, "default": 15},
        "use_jina_fallback": {"type": "boolean", "default": True},
        "prefer_jina": {"type": "boolean", "default": False},
        "min_content_chars": {"type": "integer", "minimum": 0, "default": 160},
    },
    "required": ["url"],
}

WEB_SEARCH_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **WEB_SEARCH_SCHEMA["properties"],
        "fetch_top_n": {"type": "integer", "minimum": 0, "default": 3},
        "max_bytes": {"type": "integer", "minimum": 1024, "default": 200000},
        "timeout_s": {"type": "number", "minimum": 1, "default": 15},
        "use_jina_fallback": {"type": "boolean", "default": True},
        "prefer_jina": {"type": "boolean", "default": False},
        "min_content_chars": {"type": "integer", "minimum": 0, "default": 160},
    },
    "required": ["query"],
}


def web_search_handler(call: ToolCall) -> ToolResult:
    args = call.arguments or {}
    data = web_search.run(
        query=str(args.get("query") or ""),
        max_results=int(args.get("max_results") or 8),
        region=str(args.get("region") or "wt-wt"),
        safesearch=str(args.get("safesearch") or "moderate"),
        engine=str(args.get("engine") or "duckduckgo"),
    )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


def web_fetch_handler(call: ToolCall) -> ToolResult:
    args = call.arguments or {}
    data = fetch_url.run(
        url=str(args.get("url") or ""),
        strip_html=bool(args.get("strip_html", True)),
        max_bytes=int(args.get("max_bytes") or 200_000),
        timeout_s=float(args.get("timeout_s") or 15),
        use_jina_fallback=bool(args.get("use_jina_fallback", True)),
        prefer_jina=bool(args.get("prefer_jina", False)),
        min_content_chars=int(args.get("min_content_chars") or 160),
    )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


def web_search_fetch_handler(call: ToolCall) -> ToolResult:
    args = call.arguments or {}
    data = search_fetch.run(
        query=str(args.get("query") or ""),
        max_results=int(args.get("max_results") or 8),
        fetch_top_n=int(args.get("fetch_top_n") or 3),
        region=str(args.get("region") or "wt-wt"),
        safesearch=str(args.get("safesearch") or "moderate"),
        engine=str(args.get("engine") or "duckduckgo"),
        max_bytes=int(args.get("max_bytes") or 200_000),
        timeout_s=float(args.get("timeout_s") or 15),
        use_jina_fallback=bool(args.get("use_jina_fallback", True)),
        prefer_jina=bool(args.get("prefer_jina", False)),
        min_content_chars=int(args.get("min_content_chars") or 160),
    )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


__all__ = [
    "WEB_FETCH_SCHEMA",
    "WEB_SEARCH_FETCH_SCHEMA",
    "WEB_SEARCH_SCHEMA",
    "web_fetch_handler",
    "web_search_fetch_handler",
    "web_search_handler",
]
