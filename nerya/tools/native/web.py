"""Native web research tools backed by the research skill scripts.

Surfaces the *multi-engine* + *progressive-fallback* fetch chain the
agent has access to. The search side walks
:mod:`nerya.skills.builtin.research.scripts.web_search` (Exa → Tavily →
Perplexity → LangSearch → Brave → Serper → Firecrawl → SearXNG → Bing →
DuckDuckGo) with per-engine multi-key rotation. The fetch side walks
:mod:`...research.scripts.fetch_url` (direct → Jina Reader → headless
browser engine → Scrapling).

These tools intentionally accept the *high-level* knobs (``engines``,
``keys``, ``base_urls``, ``use_browser_fallback``,
``use_scrapling_fallback``) so the LLM can override the chain when a
specific source is preferred while still inheriting workspace defaults
when the kwargs are omitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...research.captures import ResearchCaptureStore
from ...skills.builtin.research.scripts import fetch_url, search_fetch, web_search
from ..types import ToolCall, ToolResult


_ENGINE_ENUM = [
    "exa",
    "tavily",
    "perplexity",
    "langsearch",
    "brave",
    "serper",
    "firecrawl",
    "searxng",
    "bing",
    "duckduckgo",
    "duckduckgo_html",
    "duckduckgo_lite",
]


_ENGINES_PROP = {
    "type": "array",
    "items": {"type": "string", "enum": _ENGINE_ENUM},
    "description": (
        "Ordered chain of engines to try (left-most first). When omitted, "
        "the workspace default chain from search_engines.json / "
        "NERYA_SEARCH_ENGINES env / built-in defaults is used. Each entry "
        "is rotated through every configured key before falling through to "
        "the next engine."
    ),
}

_KEYS_PROP = {
    "type": "object",
    "additionalProperties": {
        "type": "array",
        "items": {"type": "string"},
    },
    "description": (
        "Optional ad-hoc API keys per engine, e.g. {\"exa\": [\"k1\",\"k2\"]}. "
        "Merged with workspace + env + vault keys; left-most key tried first."
    ),
}

_BASE_URLS_PROP = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "Per-engine base URL override. Currently honoured by ``firecrawl`` "
        "(custom Firecrawl deployment) and ``searxng`` (self-hosted "
        "instance). Falls back to workspace JSON / env / built-in default."
    ),
}

_SAVE_RAW_PROP = {
    "type": "boolean",
    "default": False,
    "description": (
        "Persist the complete, un-truncated tool result to "
        "``<workspace>/state/research_data/<date>/<slug>.json`` and "
        "include the file path as ``saved_path`` in the response. Use "
        "this when collecting data other agents must analyse later so "
        "nothing is lost to context compaction."
    ),
}


def _save_raw_capture(
    workspace_root: Path | str | None,
    *,
    kind: str,
    subject: str,
    data: dict[str, Any],
) -> str | None:
    """Write operational evidence under ``state/research_data/``.

    Research captures are runtime records, like journals and tool results;
    they are not agent-authored strategy/skill/config changes and therefore
    do not use the PatchProposal workflow. Returns the workspace-relative path
    (or ``None`` when no workspace root is wired in / the write fails).
    Failures never break the tool result — the caller still gets inline data.
    """

    if not workspace_root:
        return None
    try:
        return ResearchCaptureStore(Path(workspace_root)).store(
            kind=kind,
            subject=subject,
            data=data,
        )
    except Exception:
        return None


def _attach_saved_capture(
    data: Any,
    *,
    enabled: bool,
    workspace_root: Path | str | None,
    kind: str,
    subject: str,
) -> Any:
    """Attach a persisted evidence path to one web-tool result."""

    if not enabled or not isinstance(data, dict):
        return data
    saved = _save_raw_capture(
        workspace_root,
        kind=kind,
        subject=subject,
        data=data,
    )
    if saved:
        data["saved_path"] = saved
    return data


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
            "enum": _ENGINE_ENUM,
            "description": (
                "Pin to a single engine. Prefer ``engines`` (chain) for "
                "fallback-aware queries — only set ``engine`` when you "
                "deliberately want to disable rotation."
            ),
        },
        "engines": _ENGINES_PROP,
        "keys": _KEYS_PROP,
        "base_urls": _BASE_URLS_PROP,
        "save_raw": _SAVE_RAW_PROP,
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
        "use_jina_fallback": {
            "type": "boolean",
            "default": True,
            "description": "Allow Jina Reader fallback when direct fetch fails or returns thin content.",
        },
        "prefer_jina": {
            "type": "boolean",
            "default": False,
            "description": "Skip the direct fetch and start with Jina Reader.",
        },
        "use_browser_fallback": {
            "type": "boolean",
            "default": True,
            "description": (
                "Allow the configured headless browser engine "
                "(Lightpanda / CloakBrowser / Obscura) to render the page "
                "if Jina Reader also fails or yields low-quality output. "
                "Engine is selected via the dashboard Browsers tab."
            ),
        },
        "use_scrapling_fallback": {
            "type": "boolean",
            "default": True,
            "description": (
                "Final tier — use Scrapling (Camoufox / Playwright stealth) "
                "when every other tier failed. Requires ``pip install "
                "'scrapling[fetchers]' && scrapling install`` on the host."
            ),
        },
        "min_content_chars": {"type": "integer", "minimum": 0, "default": 160},
        "save_raw": _SAVE_RAW_PROP,
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
        "use_browser_fallback": {
            "type": "boolean",
            "default": True,
            "description": (
                "Allow the configured browser engine to render fetched "
                "search results when direct/Jina output is blocked or too "
                "thin. Search-fetch still caps fetch_top_n and total budget."
            ),
        },
        "use_scrapling_fallback": {
            "type": "boolean",
            "default": True,
            "description": (
                "Allow Scrapling stealth fetch as the final tier for fetched "
                "search results when direct/Jina/browser tiers fail."
            ),
        },
        "min_content_chars": {"type": "integer", "minimum": 0, "default": 160},
        "save_raw": _SAVE_RAW_PROP,
    },
    "required": ["query"],
}


def _coerce_engines(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        cleaned = [p for p in parts if p]
        return cleaned or None
    if isinstance(value, (list, tuple)):
        cleaned = [str(p).strip() for p in value if str(p).strip()]
        return cleaned or None
    return None


def _coerce_keys(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, list[str]] = {}
    for engine, raw in value.items():
        if isinstance(raw, str):
            entries = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        elif isinstance(raw, (list, tuple)):
            entries = [str(p).strip() for p in raw if str(p).strip()]
        else:
            continue
        if entries:
            out[str(engine).strip().lower()] = entries
    return out or None


def _coerce_base_urls(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for engine, raw in value.items():
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if url:
            out[str(engine).strip().lower()] = url
    return out or None


def web_search_handler(
    call: ToolCall, *, workspace_root: Path | str | None = None,
) -> ToolResult:
    args = call.arguments or {}
    query = str(args.get("query") or "")
    data = web_search.run(
        query=query,
        max_results=int(args.get("max_results") or 8),
        region=str(args.get("region") or "wt-wt"),
        safesearch=str(args.get("safesearch") or "moderate"),
        engine=args.get("engine") or None,
        engines=_coerce_engines(args.get("engines")),
        keys=_coerce_keys(args.get("keys")),
        base_urls=_coerce_base_urls(args.get("base_urls")),
    )
    data = _attach_saved_capture(
        data,
        enabled=bool(args.get("save_raw")),
        workspace_root=workspace_root,
        kind="web_search",
        subject=query,
    )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


def web_fetch_handler(
    call: ToolCall, *, workspace_root: Path | str | None = None,
) -> ToolResult:
    args = call.arguments or {}
    url = str(args.get("url") or "")
    data = fetch_url.run(
        url=url,
        strip_html=bool(args.get("strip_html", True)),
        max_bytes=int(args.get("max_bytes") or 200_000),
        timeout_s=float(args.get("timeout_s") or 15),
        use_jina_fallback=bool(args.get("use_jina_fallback", True)),
        prefer_jina=bool(args.get("prefer_jina", False)),
        use_browser_fallback=bool(args.get("use_browser_fallback", True)),
        use_scrapling_fallback=bool(args.get("use_scrapling_fallback", True)),
        min_content_chars=int(args.get("min_content_chars") or 160),
    )
    data = _attach_saved_capture(
        data,
        enabled=bool(args.get("save_raw")),
        workspace_root=workspace_root,
        kind="web_fetch",
        subject=url,
    )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


def web_search_fetch_handler(
    call: ToolCall, *, workspace_root: Path | str | None = None,
) -> ToolResult:
    args = call.arguments or {}
    query = str(args.get("query") or "")
    data = search_fetch.run(
        query=query,
        max_results=int(args.get("max_results") or 8),
        fetch_top_n=int(args.get("fetch_top_n") or 3),
        region=str(args.get("region") or "wt-wt"),
        safesearch=str(args.get("safesearch") or "moderate"),
        engine=args.get("engine") or None,
        engines=_coerce_engines(args.get("engines")),
        keys=_coerce_keys(args.get("keys")),
        base_urls=_coerce_base_urls(args.get("base_urls")),
        max_bytes=int(args.get("max_bytes") or 200_000),
        timeout_s=float(args.get("timeout_s") or 15),
        use_jina_fallback=bool(args.get("use_jina_fallback", True)),
        prefer_jina=bool(args.get("prefer_jina", False)),
        use_browser_fallback=bool(args.get("use_browser_fallback", True)),
        use_scrapling_fallback=bool(args.get("use_scrapling_fallback", True)),
        min_content_chars=int(args.get("min_content_chars") or 160),
    )
    data = _attach_saved_capture(
        data,
        enabled=bool(args.get("save_raw")),
        workspace_root=workspace_root,
        kind="web_search_fetch",
        subject=query,
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
