"""News-filtered web search.

Standalone CLI usage::

    python -m nerya.skills.builtin.research.scripts.news_search \\
        --json '{"query": "Solana ETF launch", "max_results": 5,
                 "freshness": "week"}'

Output schema mirrors :mod:`web_search`.

Implementation note: DuckDuckGo's vertical search uses an ``iar=news``
parameter on the regular HTML SERP plus a ``df=`` (date filter) bias
expressed via the query suffix. We pass ``iar=news`` and translate
``freshness`` (``day`` | ``week`` | ``month`` | ``year`` | ``""``) into
the matching ``df=`` form value, then fall through to the standard
``web_search.run`` parser. The legacy ``websearch_skill`` had the same
shape.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any

from ._http import http_get


_DEFAULT_MAX_RESULTS = 8
_HARD_RESULT_CAP = 25
_FRESHNESS_MAP = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}


class _NewsParser(HTMLParser):
    """Reuse the regular SERP parser shape — DDG's news vertical
    decorates each card with the same ``result__a`` / ``result__snippet``
    classes, plus an extra ``result__timestamp`` we do **not** consume
    because the snippet usually carries the timestamp prose already."""

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
            url = _decode_redirect(self._href)
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


def _decode_redirect(href: str) -> str:
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


def run(
    *,
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    region: str = "wt-wt",
    freshness: str = "week",
) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required", "results": []}
    max_results = max(1, min(int(max_results), _HARD_RESULT_CAP))
    df = _FRESHNESS_MAP.get((freshness or "").lower(), "")

    form: dict[str, Any] = {
        "q": query,
        "kl": region or "wt-wt",
        "iar": "news",
        "ia": "news",
    }
    if df:
        form["df"] = df

    started = time.monotonic()
    try:
        status, _h, body = http_get(
            "https://html.duckduckgo.com/html/",
            method="POST", form=form,
            extra_headers={"Referer": "https://html.duckduckgo.com/"},
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "results": []}

    if status >= 400 or b"result__a" not in body:
        return {
            "ok": False,
            "query": query,
            "freshness": freshness,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": f"news SERP empty or blocked (status {status})",
            "results": [],
        }

    parser = _NewsParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    results = [
        {
            "title": html.unescape(r["title"]),
            "url": r["url"],
            "snippet": html.unescape(r.get("snippet") or ""),
            "source": "duckduckgo_news",
        }
        for r in parser.results[:max_results]
    ]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "query": query,
        "freshness": freshness,
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
            freshness=str(payload.get("freshness") or "week"),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
