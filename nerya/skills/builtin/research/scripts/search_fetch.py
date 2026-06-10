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
from urllib.parse import urlparse
import re

from ...news_social.scripts import recent_news
from . import fetch_url, web_search
from ._http import DEFAULT_TIMEOUT


_DEFAULT_SEARCH_RESULTS = 8
_DEFAULT_FETCH_TOP_N = 3
_HARD_FETCH_TOP_N = 10
_MIN_FETCH_STEP_TIMEOUT = 0.25
_RSS_FALLBACK_PROBE_LIMIT = 12
_QUERY_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9.]{2,5}\b")
_NON_TICKER_TOKENS = {
    "API",
    "CEO",
    "CFO",
    "CPI",
    "EPS",
    "ETF",
    "FOMC",
    "GDP",
    "GAAP",
    "HTTP",
    "HTTPS",
    "IFRS",
    "IPO",
    "JSON",
    "LLM",
    "RSS",
    "SEC",
    "USD",
    "URL",
    "USA",
    "UTC",
}
_NON_TICKER_TOKEN_RE = re.compile(r"^(?:FY|Q)\d{1,4}$", re.I)
_QUERY_RELEVANCE_STOPWORDS = {
    "after",
    "before",
    "finance",
    "financial",
    "headline",
    "headlines",
    "june",
    "latest",
    "market",
    "markets",
    "message",
    "messages",
    "news",
    "price",
    "quotes",
    "stock",
    "stocks",
    "today",
    "update",
    "updates",
}
_QUERY_RELEVANCE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.-]{2,}")


def _normalise_host(url: str) -> str:
    try:
        host = urlparse(str(url or "")).netloc.lower()
    except Exception:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_overlaps(candidate: str, known_hosts: set[str]) -> bool:
    host = _normalise_host(candidate)
    if not host:
        return False
    for known in known_hosts:
        if host == known or host.endswith(f".{known}") or known.endswith(f".{host}"):
            return True
    return False


def _search_hosts(search: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    for result in search.get("results") or []:
        if not isinstance(result, dict):
            continue
        host = _normalise_host(str(result.get("url") or ""))
        if host:
            hosts.add(host)
    return hosts


def _extract_query_tickers(query: str) -> list[str]:
    out: list[str] = []
    for match in _QUERY_TICKER_RE.finditer(query or ""):
        ticker = match.group(0).upper().strip(".")
        if (
            ticker in _NON_TICKER_TOKENS
            or _NON_TICKER_TOKEN_RE.match(ticker)
            or ticker in out
        ):
            continue
        out.append(ticker)
        if len(out) >= 12:
            break
    return out


def _item_ticker_overlaps(item: dict[str, Any], query_tickers: set[str]) -> bool:
    if not query_tickers:
        return False
    item_tickers = {
        str(ticker).upper().strip(".")
        for ticker in (item.get("tickers") or [])
        if str(ticker).strip()
    }
    return bool(item_tickers & query_tickers)


def _query_relevance_terms(query: str, query_tickers: set[str]) -> set[str]:
    terms: set[str] = set()
    for match in _QUERY_RELEVANCE_TOKEN_RE.finditer(query or ""):
        token = match.group(0).strip(".")
        upper = token.upper()
        lower = token.lower()
        if upper in query_tickers or upper in _NON_TICKER_TOKENS:
            continue
        if lower in _QUERY_RELEVANCE_STOPWORDS:
            continue
        if lower.isdigit():
            continue
        terms.add(lower)
    return terms


def _item_text_matches_query(
    item: dict[str, Any],
    *,
    query: str,
    query_tickers: set[str],
) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "summary", "body", "description", "url", "link")
    )
    upper_text = text.upper()
    if any(ticker and ticker in upper_text for ticker in query_tickers):
        return True
    lower_text = text.lower()
    relevance_terms = _query_relevance_terms(query, query_tickers)
    matched_terms = {term for term in relevance_terms if term and term in lower_text}
    if not relevance_terms:
        return False
    if query_tickers:
        return len(matched_terms) >= min(2, len(relevance_terms))
    return bool(matched_terms)


def _readable_search_documents(
    documents: list[dict[str, Any]],
    *,
    min_content_chars: int,
) -> list[dict[str, Any]]:
    readable: list[dict[str, Any]] = []
    for doc in documents:
        if not doc.get("ok"):
            continue
        error = str(doc.get("error") or "").strip().lower()
        if error == "low_quality_content":
            continue
        markdown = str(doc.get("markdown") or "").strip()
        if not markdown:
            continue
        readable.append(doc)
    return readable


def _rss_markdown(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    source = str(item.get("source") or "rss").strip()
    published = str(item.get("published_at") or item.get("published") or "").strip()
    url = str(item.get("url") or item.get("link") or "").strip()
    summary = str(
        item.get("summary") or item.get("body") or item.get("description") or ""
    ).strip()
    tickers = [str(t).strip() for t in (item.get("tickers") or []) if str(t).strip()]
    lines = [
        f"# {title}" if title else "# RSS headline",
        f"Source: {source}",
    ]
    if published:
        lines.append(f"Published: {published}")
    if url:
        lines.append(f"URL: {url}")
    if tickers:
        lines.append(f"Tickers: {', '.join(tickers)}")
    if summary:
        lines.extend(["", summary])
    return "\n".join(lines).strip()


def _rss_document(item: dict[str, Any], *, rank: int) -> dict[str, Any]:
    markdown = _rss_markdown(item)
    summary = str(
        item.get("summary") or item.get("body") or item.get("description") or ""
    ).strip()
    return {
        "rank": rank,
        "title": item.get("title") or "",
        "url": item.get("url") or item.get("link") or "",
        "snippet": summary[:500],
        "source": item.get("source") or "rss",
        "ok": True,
        "status": 200,
        "fetch_method": "rss_fallback",
        "content_type": "application/rss+xml",
        "bytes": len(markdown.encode("utf-8")),
        "truncated": False,
        "markdown": markdown,
        "fallback_errors": [],
        "safety": {"allowed": True, "source": "rss"},
    }


def _rss_fallback_documents(
    *,
    query: str,
    search: dict[str, Any],
    limit: int,
    reason: str = "no_readable_search_documents",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "matched_count": 0,
        "source": "news_social.recent_news",
        "reason": reason,
    }
    if limit <= 0:
        meta["reason"] = "document_limit_reached"
        return [], meta
    hosts = _search_hosts(search)
    query_tickers = set(_extract_query_tickers(query))
    if query_tickers:
        meta["query_tickers"] = sorted(query_tickers)
    if not hosts and not query_tickers:
        meta["reason"] = "no_search_result_hosts"
        return [], meta
    meta["attempted"] = True
    try:
        payload: dict[str, Any] = {
            "topic": query,
            "limit": max(limit, _RSS_FALLBACK_PROBE_LIMIT),
        }
        if query_tickers:
            payload["tickers"] = sorted(query_tickers)
        rss = recent_news.run(payload)
    except Exception as exc:  # noqa: BLE001
        meta["error"] = f"{type(exc).__name__}: {exc}"
        return [], meta
    meta["sources"] = rss.get("sources") or []
    meta["errors"] = rss.get("errors") or []
    docs: list[dict[str, Any]] = []
    rejected_count = 0
    for item in rss.get("items") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "")
        host_match = _host_overlaps(url, hosts)
        ticker_match = _item_ticker_overlaps(item, query_tickers)
        if ticker_match and query_tickers:
            ticker_match = _item_text_matches_query(
                item,
                query=query,
                query_tickers=query_tickers,
            )
        if not host_match and not ticker_match:
            rejected_count += 1
            continue
        docs.append(_rss_document(item, rank=len(docs) + 1))
        if len(docs) >= limit:
            break
    meta["matched_count"] = len(docs)
    if rejected_count:
        meta["rejected_count"] = rejected_count
    meta["ok"] = bool(docs)
    return docs, meta


def run(
    *,
    query: str,
    max_results: int = _DEFAULT_SEARCH_RESULTS,
    fetch_top_n: int = _DEFAULT_FETCH_TOP_N,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    engine: str | None = None,
    engines: list[str] | None = None,
    keys: dict[str, list[str]] | None = None,
    base_urls: dict[str, str] | None = None,
    max_bytes: int = 200_000,
    timeout_s: float = DEFAULT_TIMEOUT,
    use_jina_fallback: bool = True,
    prefer_jina: bool = False,
    use_browser_fallback: bool = True,
    use_scrapling_fallback: bool = True,
    min_content_chars: int = 160,
) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required", "documents": []}

    fetch_top_n = max(0, min(int(fetch_top_n), _HARD_FETCH_TOP_N))
    timeout_s = max(_MIN_FETCH_STEP_TIMEOUT, float(timeout_s or DEFAULT_TIMEOUT))
    started = time.monotonic()
    # Treat timeout_s as the per-page budget and keep the whole search+fetch
    # operation bounded. Otherwise each progressive fetch fallback can spend
    # its own timeout and a single tool call can outlive the agent turn budget.
    fetch_budget_s = timeout_s * max(1, min(fetch_top_n, 3))
    deadline = started + fetch_budget_s

    def next_timeout(step: str) -> float | None:
        remaining = deadline - time.monotonic()
        if remaining <= _MIN_FETCH_STEP_TIMEOUT:
            return None
        if step == "retry_after_failed_fetch":
            return min(max(timeout_s, 30.0), remaining)
        return min(timeout_s, remaining)

    search = web_search.run(
        query=query,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
        engine=engine,
        engines=engines,
        keys=keys,
        base_urls=base_urls,
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
        step_timeout = next_timeout("fetch")
        if step_timeout is None:
            fetch_errors.append({
                "rank": len(seen),
                "url": url,
                "error": "budget_exhausted",
            })
            break
        rank = len(seen)
        fetched = fetch_url.run(
            url=url,
            strip_html=True,
            max_bytes=max_bytes,
            timeout_s=step_timeout,
            use_jina_fallback=use_jina_fallback,
            prefer_jina=prefer_jina,
            use_browser_fallback=use_browser_fallback,
            use_scrapling_fallback=use_scrapling_fallback,
            min_content_chars=min_content_chars,
        )
        if not fetched.get("ok") and use_jina_fallback:
            step_timeout = next_timeout("retry_after_failed_fetch")
            if step_timeout is None:
                fetch_errors.append({
                    "rank": rank,
                    "url": url,
                    "error": "budget_exhausted",
                })
                break
            retry = fetch_url.run(
                url=url,
                strip_html=True,
                max_bytes=max_bytes,
                timeout_s=step_timeout,
                use_jina_fallback=True,
                prefer_jina=True,
                use_browser_fallback=use_browser_fallback,
                use_scrapling_fallback=use_scrapling_fallback,
                min_content_chars=min_content_chars,
            )
            if retry.get("ok"):
                prior_errors = fetched.get("fallback_errors") or []
                retry["fallback_errors"] = [
                    "retry_after_failed_fetch: prefer_jina",
                    *prior_errors,
                    *(retry.get("fallback_errors") or []),
                ]
                fetched = retry
        doc = {
            "rank": rank,
            "title": fetched.get("title") or result.get("title") or "",
            "url": fetched.get("url") or url,
            "snippet": result.get("snippet") or "",
            "source": result.get("source") or "",
            "ok": bool(fetched.get("ok")),
            "status": fetched.get("status"),
            "error": fetched.get("error") or "",
            "fetch_method": fetched.get("fetch_method") or "",
            "content_type": fetched.get("content_type") or "",
            "bytes": fetched.get("bytes") or 0,
            "truncated": bool(fetched.get("truncated")),
            "markdown": fetched.get("markdown") or fetched.get("text") or "",
            "fallback_errors": fetched.get("fallback_errors") or [],
            "safety": fetched.get("safety"),
        }
        if fetched.get("ok"):
            documents.append(doc)
        else:
            fetch_error = {
                "rank": rank,
                "url": url,
                "error": fetched.get("error") or f"HTTP {fetched.get('status')}",
            }
            if fetched.get("fallback_errors"):
                fetch_error["fallback_errors"] = fetched.get("fallback_errors") or []
            fetch_errors.append(fetch_error)

    rss_fallback: dict[str, Any] | None = None
    readable_documents = _readable_search_documents(
        documents,
        min_content_chars=min_content_chars,
    )
    query_tickers = set(_extract_query_tickers(query))
    should_attempt_rss = (
        fetch_top_n > 0
        and (
            not readable_documents
            or bool(query_tickers)
        )
    )
    if should_attempt_rss:
        rss_reason = (
            "ticker_news_augmentation"
            if readable_documents and query_tickers
            else "no_readable_search_documents"
        )
        rss_docs, rss_fallback = _rss_fallback_documents(
            query=query,
            search=search,
            limit=fetch_top_n,
            reason=rss_reason,
        )
        if rss_docs:
            seen_urls = {str(doc.get("url") or "") for doc in rss_docs}
            documents = [
                *rss_docs,
                *[
                    doc
                    for doc in documents
                    if str(doc.get("url") or "") not in seen_urls
                ],
            ]

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result = {
        "ok": bool(
            _readable_search_documents(documents, min_content_chars=min_content_chars)
            or (
                rss_fallback is not None
                and bool(rss_fallback.get("ok"))
            )
        ),
        "query": query,
        "search": search,
        "elapsed_ms": elapsed_ms,
        "budget_exhausted": any(err.get("error") == "budget_exhausted" for err in fetch_errors),
        "count": len(documents),
        "attempted": len(seen),
        "documents": documents,
        "fetch_errors": fetch_errors,
    }
    if rss_fallback is not None:
        result["rss_fallback"] = rss_fallback
    return result


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
    engines_raw = payload.get("engines")
    engines: list[str] | None = None
    if isinstance(engines_raw, str):
        engines = [e.strip() for e in engines_raw.split(",") if e.strip()]
    elif isinstance(engines_raw, list):
        engines = [str(e).strip() for e in engines_raw if str(e).strip()]
    engine = str(payload.get("engine")).strip() if payload.get("engine") else None
    try:
        result = run(
            query=query,
            max_results=int(payload.get("max_results") or _DEFAULT_SEARCH_RESULTS),
            fetch_top_n=int(payload.get("fetch_top_n") or _DEFAULT_FETCH_TOP_N),
            region=str(payload.get("region") or "wt-wt"),
            safesearch=str(payload.get("safesearch") or "moderate"),
            engine=engine,
            engines=engines,
            keys=payload.get("keys") or None,
            base_urls=payload.get("base_urls") or None,
            max_bytes=int(payload.get("max_bytes") or 200_000),
            timeout_s=float(payload.get("timeout_s") or DEFAULT_TIMEOUT),
            use_jina_fallback=_payload_bool(payload, "use_jina_fallback", True),
            prefer_jina=_payload_bool(payload, "prefer_jina", False),
            use_browser_fallback=_payload_bool(payload, "use_browser_fallback", True),
            use_scrapling_fallback=_payload_bool(payload, "use_scrapling_fallback", True),
            min_content_chars=int(payload.get("min_content_chars") or 160),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
