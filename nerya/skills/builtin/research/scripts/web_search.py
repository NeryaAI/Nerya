"""Search the public web via DuckDuckGo (HTML SERP + lite fallback).

Standalone CLI usage::

    python -m nerya.skills.builtin.research.scripts.web_search \\
        --json '{"query": "Hyperliquid funding rate", "max_results": 5}'

Output schema::

    {
      "ok": bool,
      "query": str,
      "engine": "duckduckgo_html" | "duckduckgo_lite",
      "fallback_errors": [str, ...],
      "elapsed_ms": int,
      "count": int,
      "results": [{"title": str, "url": str, "snippet": str, "source": str}, ...]
    }

Pure stdlib — no extra wheels needed. Uses the same HTML POST trick as
``websearch_skill`` to dodge DuckDuckGo's anti-bot stub.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any

from ._http import http_get


_DEFAULT_MAX_RESULTS = 8
_HARD_RESULT_CAP = 25


class _DDGHtmlParser(HTMLParser):
    """Pull ``{title, url, snippet}`` triples from html.duckduckgo HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._mode: str | None = None
        self._buf: list[str] = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_d = dict(attrs)
        css = attrs_d.get("class") or ""
        if "result__a" in css:
            self._mode = "title"
            self._buf = []
            self._href = attrs_d.get("href") or ""
        elif "result__snippet" in css:
            self._mode = "snippet"
            self._buf = []

    def handle_endtag(self, tag):
        if tag != "a" or self._mode is None:
            return
        text = " ".join("".join(self._buf).split()).strip()
        if self._mode == "title":
            url = _decode_ddg_redirect(self._href)
            if url:
                self.results.append({"title": text, "url": url, "snippet": ""})
        elif self._mode == "snippet":
            if self.results and not self.results[-1].get("snippet"):
                self.results[-1]["snippet"] = text
        self._mode = None
        self._buf = []

    def handle_data(self, data):
        if self._mode is not None:
            self._buf.append(data)


def _decode_ddg_redirect(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("uddg") or qs.get("u")
        if target:
            return urllib.parse.unquote(target[0])
    except Exception:
        pass
    if href.startswith("http"):
        return href
    return ""


def _ddg_html(query: str, *, max_results: int, region: str, safesearch: str) -> list[dict[str, Any]]:
    form = {
        "q": query,
        "kl": region or "wt-wt",
        "kp": {"strict": "1", "moderate": "-1", "off": "-2"}.get(safesearch, "-1"),
    }
    status, _h, body = http_get(
        "https://html.duckduckgo.com/html/",
        method="POST", form=form,
        extra_headers={"Referer": "https://html.duckduckgo.com/"},
    )
    if status == 202 or (status == 200 and b"result__a" not in body):
        raise RuntimeError(
            f"duckduckgo_html anti-bot guard hit (status {status}, len={len(body)})"
        )
    if status >= 400:
        raise RuntimeError(f"duckduckgo_html HTTP {status}")
    parser = _DDGHtmlParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    return [
        {
            "title": html.unescape(r["title"]),
            "url": r["url"],
            "snippet": html.unescape(r.get("snippet") or ""),
            "source": "duckduckgo_html",
        }
        for r in parser.results[:max_results]
    ]


def _ddg_lite(query: str, *, max_results: int, region: str) -> list[dict[str, Any]]:
    form = {"q": query, "kl": region or "wt-wt"}
    status, _h, body = http_get(
        "https://lite.duckduckgo.com/lite/",
        method="POST", form=form,
        extra_headers={"Referer": "https://lite.duckduckgo.com/"},
    )
    if status >= 400:
        raise RuntimeError(f"duckduckgo_lite HTTP {status}")
    text_html = body.decode("utf-8", errors="replace")
    pattern = re.compile(
        r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<td[^>]+class="result-snippet">(.*?)</td>',
        re.DOTALL,
    )
    out: list[dict[str, Any]] = []
    for m in pattern.finditer(text_html):
        href = _decode_ddg_redirect(m.group(1)) or m.group(1)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        if title and href:
            out.append({
                "title": title, "url": href, "snippet": snippet,
                "source": "duckduckgo_lite",
            })
        if len(out) >= max_results:
            break
    return out


def run(
    *,
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    engine: str = "duckduckgo",
) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required", "results": []}
    max_results = max(1, min(int(max_results), _HARD_RESULT_CAP))
    chain: list[str]
    if engine in ("duckduckgo", "ddg"):
        chain = ["duckduckgo_html", "duckduckgo_lite"]
    elif engine in ("duckduckgo_html", "ddg_html"):
        chain = ["duckduckgo_html"]
    elif engine in ("duckduckgo_lite", "ddg_lite"):
        chain = ["duckduckgo_lite"]
    else:
        return {
            "ok": False,
            "error": f"unsupported engine {engine!r}",
            "results": [],
        }

    started = time.monotonic()
    used = ""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in chain:
        try:
            if candidate == "duckduckgo_html":
                results = _ddg_html(
                    query, max_results=max_results,
                    region=region, safesearch=safesearch,
                )
            elif candidate == "duckduckgo_lite":
                results = _ddg_lite(
                    query, max_results=max_results, region=region,
                )
            if results:
                used = candidate
                break
            errors.append(f"{candidate}: empty result set")
        except Exception as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not results and not used:
        return {
            "ok": False,
            "query": query,
            "engine_chain": chain,
            "fallback_errors": errors,
            "elapsed_ms": elapsed_ms,
            "results": [],
        }
    return {
        "ok": True,
        "query": query,
        "engine": used,
        "fallback_errors": errors,
        "elapsed_ms": elapsed_ms,
        "count": len(results),
        "results": results,
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
            max_results=int(payload.get("max_results") or _DEFAULT_MAX_RESULTS),
            region=str(payload.get("region") or "wt-wt"),
            safesearch=str(payload.get("safesearch") or "moderate"),
            engine=str(payload.get("engine") or "duckduckgo"),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
