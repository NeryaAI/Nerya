"""Search the web and fetch readable markdown for the top results.

Standalone CLI usage::

    python -m nerya.skills.builtin.research.scripts.search_fetch \\
        --json '{"query": "Hyperliquid funding rate", "max_results": 5,
                 "fetch_top_n": 3}'

Output schema::

    {
      "ok": bool,
      "query": str,
      "search": dict,
      "count": int,
      "documents": [
        {
          "rank": int,
          "title": str,
          "url": str,
          "snippet": str,
          "ok": bool,
          "fetch_method": str,
          "markdown": str
        }
      ],
      "fetch_errors": [{"rank": int, "url": str, "error": str}]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import fetch_url, web_search
from ._http import DEFAULT_TIMEOUT


_DEFAULT_SEARCH_RESULTS = 8
_DEFAULT_FETCH_TOP_N = 3
_HARD_FETCH_TOP_N = 10


def run(
    *,
    query: str,
    max_results: int = _DEFAULT_SEARCH_RESULTS,
    fetch_top_n: int = _DEFAULT_FETCH_TOP_N,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    engine: str = "duckduckgo",
    max_bytes: int = 200_000,
    timeout_s: float = DEFAULT_TIMEOUT,
    use_jina_fallback: bool = True,
    prefer_jina: bool = False,
    min_content_chars: int = 160,
) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required", "documents": []}

    fetch_top_n = max(0, min(int(fetch_top_n), _HARD_FETCH_TOP_N))
    started = time.monotonic()
    search = web_search.run(
        query=query,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
        engine=engine,
    )
    if not search.get("ok"):
        search["elapsed_ms_total"] = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "query": query,
            "search": search,
            "documents": [],
            "fetch_errors": [],
            "error": search.get("error") or "search failed",
        }

    documents: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in search.get("results", []):
        if len(documents) >= fetch_top_n:
            break
        url = str(result.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        rank = len(seen)
        fetched = fetch_url.run(
            url=url,
            strip_html=True,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            use_jina_fallback=use_jina_fallback,
            prefer_jina=prefer_jina,
            min_content_chars=min_content_chars,
        )
        doc = {
            "rank": rank,
            "title": fetched.get("title") or result.get("title") or "",
            "url": fetched.get("url") or url,
            "snippet": result.get("snippet") or "",
            "source": result.get("source") or "",
            "ok": bool(fetched.get("ok")),
            "status": fetched.get("status"),
            "fetch_method": fetched.get("fetch_method") or "",
            "content_type": fetched.get("content_type") or "",
            "bytes": fetched.get("bytes") or 0,
            "truncated": bool(fetched.get("truncated")),
            "markdown": fetched.get("markdown") or fetched.get("text") or "",
            "fallback_errors": fetched.get("fallback_errors") or [],
            "safety": fetched.get("safety"),
        }
        documents.append(doc)
        if not fetched.get("ok"):
            fetch_errors.append({
                "rank": rank,
                "url": url,
                "error": fetched.get("error") or f"HTTP {fetched.get('status')}",
            })

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": bool(documents) and any(doc.get("ok") for doc in documents),
        "query": query,
        "search": search,
        "elapsed_ms": elapsed_ms,
        "count": len(documents),
        "documents": documents,
        "fetch_errors": fetch_errors,
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--query", dest="query", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    query = args.query or payload.get("query") or ""
    try:
        result = run(
            query=query,
            max_results=int(payload.get("max_results") or _DEFAULT_SEARCH_RESULTS),
            fetch_top_n=int(payload.get("fetch_top_n") or _DEFAULT_FETCH_TOP_N),
            region=str(payload.get("region") or "wt-wt"),
            safesearch=str(payload.get("safesearch") or "moderate"),
            engine=str(payload.get("engine") or "duckduckgo"),
            max_bytes=int(payload.get("max_bytes") or 200_000),
            timeout_s=float(payload.get("timeout_s") or DEFAULT_TIMEOUT),
            use_jina_fallback=_payload_bool(payload, "use_jina_fallback", True),
            prefer_jina=_payload_bool(payload, "prefer_jina", False),
            min_content_chars=int(payload.get("min_content_chars") or 160),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
