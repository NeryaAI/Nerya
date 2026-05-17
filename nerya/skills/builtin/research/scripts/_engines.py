"""Search engine adapters with multi-key rotation.

Supports the following engines, all behind a uniform ``EngineAdapter``
protocol:

| engine            | needs key | source           |
|-------------------|-----------|------------------|
| ``duckduckgo``    | no        | html.duckduckgo  |
| ``duckduckgo_lite`` | no      | lite.duckduckgo  |
| ``exa``           | yes       | api.exa.ai       |
| ``tavily``        | yes       | api.tavily.com   |
| ``perplexity``    | yes       | api.perplexity.ai (online sonar models) |
| ``brave``         | yes       | api.search.brave.com |
| ``serper``        | yes       | google.serper.dev (Google SERP) |
| ``bing``          | yes       | api.bing.microsoft.com |
| ``langsearch``    | yes       | api.langsearch.com (free tier ~10k/mo) |
| ``searxng``       | no*       | self-hosted meta-search (configurable base_url) |
| ``firecrawl``     | yes       | api.firecrawl.dev (configurable base_url, self-hostable) |

*SearXNG is keyless but requires a reachable base URL; supply it via
``base_url`` config, ``NERYA_SEARCH_SEARXNG_BASE_URL`` env, or via the
optional auto-deployed local docker container managed by the dashboard.

Each adapter accepts a list of API keys. The orchestrator rotates
through them on auth/quota failures. Returns a uniform result envelope:

    [
      {"title": str, "url": str, "snippet": str, "source": str,
       "engine": str, "key_index": int}
    ]

Raises ``EngineKeyExhausted`` when all provided keys for an engine have
been tried and none worked.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Protocol

from ._http import http_get


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EngineError(RuntimeError):
    """Base class for engine failures."""


class EngineKeyExhausted(EngineError):
    """All API keys for an engine have failed."""


class EngineKeylessFailure(EngineError):
    """A keyless engine (e.g. DuckDuckGo) failed."""


# Errors that tell us "rotate to next key", not "engine is broken".
# We treat HTTP 401/403/429 as key/quota issues; 5xx as engine issues.
_KEY_ROTATE_STATUSES: frozenset[int] = frozenset({401, 402, 403, 407, 429})


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


@dataclass
class EngineAdapter(Protocol):
    """Uniform shape: ``run(query, max_results, **kwargs) -> list[dict]``.

    All adapters MUST raise ``EngineError`` on failure, never return a
    partial-but-empty list to mask failures. The orchestrator rotates
    keys / falls through to the next engine based on the exception.
    """

    name: str

    def run(self, *, query: str, max_results: int,
            region: str = "wt-wt", **kwargs: Any) -> list[dict[str, Any]]:
        ...


# ---- DuckDuckGo (HTML) --- keyless, primary fallback ---------------------


class _DDGHtmlParser(HTMLParser):
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


@dataclass
class DuckDuckGoHtmlAdapter:
    name: str = "duckduckgo"

    def run(self, *, query: str, max_results: int, region: str = "wt-wt",
            safesearch: str = "moderate", **_) -> list[dict[str, Any]]:
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
            raise EngineKeylessFailure(
                f"duckduckgo anti-bot guard hit (status {status})"
            )
        if status >= 400:
            raise EngineKeylessFailure(f"duckduckgo HTTP {status}")
        parser = _DDGHtmlParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        out = [
            {
                "title": html.unescape(r["title"]),
                "url": r["url"],
                "snippet": html.unescape(r.get("snippet") or ""),
                "source": "duckduckgo_html",
                "engine": "duckduckgo",
                "key_index": -1,
            }
            for r in parser.results[:max_results]
        ]
        if not out:
            raise EngineKeylessFailure("duckduckgo returned no parsable results")
        return out


@dataclass
class DuckDuckGoLiteAdapter:
    name: str = "duckduckgo_lite"

    def run(self, *, query: str, max_results: int, region: str = "wt-wt",
            **_) -> list[dict[str, Any]]:
        form = {"q": query, "kl": region or "wt-wt"}
        status, _h, body = http_get(
            "https://lite.duckduckgo.com/lite/",
            method="POST", form=form,
            extra_headers={"Referer": "https://lite.duckduckgo.com/"},
        )
        if status >= 400:
            raise EngineKeylessFailure(f"duckduckgo_lite HTTP {status}")
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
                    "engine": "duckduckgo_lite",
                    "key_index": -1,
                })
            if len(out) >= max_results:
                break
        if not out:
            raise EngineKeylessFailure("duckduckgo_lite returned no results")
        return out


# ---- Keyed adapters ------------------------------------------------------


@dataclass
class _KeyedAdapter:
    """Shared scaffolding for engines that need an API key.

    Concrete adapters override ``_call(query, key, max_results, **kw)``
    and let the rotation logic here handle 401/429/etc.
    """

    name: str
    keys: list[str] = field(default_factory=list)

    def run(self, *, query: str, max_results: int, region: str = "wt-wt",
            **kwargs: Any) -> list[dict[str, Any]]:
        if not self.keys:
            raise EngineKeyExhausted(f"{self.name}: no API keys configured")
        last_err: Exception | None = None
        for idx, key in enumerate(self.keys):
            try:
                results = self._call(
                    query=query, key=key, max_results=max_results,
                    region=region, **kwargs,
                )
            except EngineError as exc:
                last_err = exc
                # rotate on quota / auth / rate-limit failures
                if _is_rotate_signal(exc):
                    continue
                # non-rotate engine error → stop trying keys
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
            for r in results:
                r.setdefault("engine", self.name)
                r.setdefault("source", self.name)
                r["key_index"] = idx
            if results:
                return results
        raise EngineKeyExhausted(
            f"{self.name}: all {len(self.keys)} key(s) exhausted ({last_err})"
        )

    # Subclasses implement this:
    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError


def _is_rotate_signal(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "401" in msg or "403" in msg or "429" in msg or "quota" in msg \
            or "rate limit" in msg or "unauthorized" in msg or "forbidden" in msg:
        return True
    return False


def _http_status_from_msg(msg: str) -> int | None:
    m = re.search(r"\b(\d{3})\b", msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


@dataclass
class ExaAdapter(_KeyedAdapter):
    name: str = "exa"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        body = json.dumps({
            "query": query,
            "num_results": max_results,
            "use_autoprompt": True,
            "type": "auto",
            "contents": {"highlights": True, "text": False},
        }).encode("utf-8")
        status, _h, raw = http_get(
            "https://api.exa.ai/search",
            method="POST",
            extra_headers={
                "x-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            form=None,
        )
        # _http.http_get only supports form-encoded POST; do raw urllib here
        import urllib.request
        req = urllib.request.Request(
            "https://api.exa.ai/search",
            method="POST",
            data=body,
            headers={
                "x-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"exa: {type(exc).__name__}: {exc}") from exc
        results = payload.get("results") or []
        out: list[dict[str, Any]] = []
        for r in results[:max_results]:
            highlights = r.get("highlights") or []
            snippet = " … ".join(highlights[:2]) if highlights else (r.get("text") or "")[:300]
            out.append({
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": snippet,
                "source": "exa",
                "published_date": r.get("publishedDate"),
            })
        return out


@dataclass
class TavilyAdapter(_KeyedAdapter):
    name: str = "tavily"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        body = json.dumps({
            "api_key": key,
            "query": query,
            "max_results": max_results,
            "search_depth": kwargs.get("search_depth", "basic"),
            "include_answer": False,
            "include_raw_content": False,
        }).encode("utf-8")
        import urllib.request
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            method="POST", data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Nerya/research"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"tavily: {type(exc).__name__}: {exc}") from exc
        out = []
        for r in (payload.get("results") or [])[:max_results]:
            out.append({
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or "",
                "source": "tavily",
                "score": r.get("score"),
            })
        return out


@dataclass
class PerplexityAdapter(_KeyedAdapter):
    """Perplexity Online via Sonar models — returns synthesized answer.

    Different shape from other engines: returns a single result with the
    full answer text + citations array. Adapter formats citations as
    individual result rows when ``citations_as_results=True``.
    """
    name: str = "perplexity"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        model = kwargs.get("perplexity_model", "sonar")
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": query}],
            "return_citations": True,
        }).encode("utf-8")
        import urllib.request
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            method="POST", data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"perplexity: {type(exc).__name__}: {exc}") from exc
        choices = payload.get("choices") or []
        if not choices:
            return []
        answer = (choices[0].get("message") or {}).get("content") or ""
        citations: list[str] = payload.get("citations") or []
        out = [{
            "title": "Perplexity Sonar answer",
            "url": citations[0] if citations else "",
            "snippet": (answer[:600] + "…") if len(answer) > 600 else answer,
            "source": "perplexity",
        }]
        for url in citations[: max(0, max_results - 1)]:
            out.append({
                "title": url, "url": url, "snippet": "",
                "source": "perplexity_citation",
            })
        return out


@dataclass
class BraveAdapter(_KeyedAdapter):
    name: str = "brave"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({
            "q": query,
            "count": max_results,
            "country": (region or "us").split("-")[-1].upper()[:2] or "US",
        })
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "X-Subscription-Token": key,
                "Accept": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"brave: {type(exc).__name__}: {exc}") from exc
        results = ((payload.get("web") or {}).get("results")) or []
        out = []
        for r in results[:max_results]:
            out.append({
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("description") or "",
                "source": "brave",
                "published_date": r.get("page_age"),
            })
        return out


@dataclass
class SerperAdapter(_KeyedAdapter):
    """Google SERP via google.serper.dev."""
    name: str = "serper"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        body = json.dumps({
            "q": query,
            "num": max_results,
            "gl": (region or "us").split("-")[-1].lower()[:2] or "us",
        }).encode("utf-8")
        import urllib.request
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            method="POST", data=body,
            headers={
                "X-API-KEY": key,
                "Content-Type": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"serper: {type(exc).__name__}: {exc}") from exc
        organic = payload.get("organic") or []
        out = []
        for r in organic[:max_results]:
            out.append({
                "title": r.get("title") or "",
                "url": r.get("link") or "",
                "snippet": r.get("snippet") or "",
                "source": "google_serper",
                "position": r.get("position"),
            })
        return out


@dataclass
class BingAdapter(_KeyedAdapter):
    name: str = "bing"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({
            "q": query,
            "count": max_results,
            "mkt": (region.replace("wt-wt", "en-US") or "en-US"),
            "responseFilter": "Webpages",
        })
        url = f"https://api.bing.microsoft.com/v7.0/search?{params}"
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"bing: {type(exc).__name__}: {exc}") from exc
        webpages = (payload.get("webPages") or {}).get("value") or []
        out = []
        for r in webpages[:max_results]:
            out.append({
                "title": r.get("name") or "",
                "url": r.get("url") or "",
                "snippet": r.get("snippet") or "",
                "source": "bing",
                "date_last_crawled": r.get("dateLastCrawled"),
            })
        return out


@dataclass
class LangSearchAdapter(_KeyedAdapter):
    """LangSearch Web Search API (https://docs.langsearch.com)."""

    name: str = "langsearch"

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        body = json.dumps({
            "query": query,
            "freshness": kwargs.get("freshness", "noLimit"),
            "summary": True,
            "count": max(1, min(int(max_results or 10), 10)),
        }).encode("utf-8")
        import urllib.request
        req = urllib.request.Request(
            "https://api.langsearch.com/v1/web-search",
            method="POST", data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"langsearch: {type(exc).__name__}: {exc}") from exc
        # LangSearch returns: {data: {webPages: {value: [{name,url,snippet,...}]}}}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        webpages = ((data.get("webPages") or {}).get("value")) or []
        if not webpages and isinstance(data.get("results"), list):
            webpages = data["results"]
        out: list[dict[str, Any]] = []
        for r in webpages[:max_results]:
            if not isinstance(r, dict):
                continue
            out.append({
                "title": r.get("name") or r.get("title") or "",
                "url": r.get("url") or r.get("link") or "",
                "snippet": (r.get("snippet") or r.get("summary")
                            or r.get("content") or ""),
                "source": "langsearch",
                "site_name": r.get("siteName"),
                "published_date": r.get("datePublished"),
            })
        return out


def _normalize_base_url(value: str | None, *, default: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raw = default
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


@dataclass
class SearXNGAdapter:
    """Adapter for a self-hosted SearXNG instance.

    Configurable ``base_url`` (default ``http://127.0.0.1:8888``). Hits
    ``GET <base_url>/search?q=<query>&format=json`` — requires the
    instance to allow JSON output (set ``search.formats: [html, json]``
    in ``settings.yml``).
    """

    name: str = "searxng"
    base_url: str = "http://127.0.0.1:8888"
    timeout_s: float = 15.0

    def run(self, *, query: str, max_results: int, region: str = "wt-wt",
            safesearch: str = "moderate", **kwargs: Any) -> list[dict[str, Any]]:
        base = _normalize_base_url(self.base_url, default="http://127.0.0.1:8888")
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "language": (region or "wt-wt").lower().split("-")[0] or "en",
            "safesearch": {"strict": "2", "moderate": "1", "off": "0"}.get(safesearch, "1"),
        })
        url = f"{base}/search?{params}"
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineKeylessFailure(
                f"searxng@{base}: {type(exc).__name__}: {exc}"
            ) from exc
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise EngineKeylessFailure(
                f"searxng@{base}: unexpected payload (no 'results' array)"
            )
        out: list[dict[str, Any]] = []
        for r in items[:max_results]:
            if not isinstance(r, dict):
                continue
            out.append({
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or "",
                "source": "searxng",
                "engine": "searxng",
                "underlying_engine": r.get("engine"),
                "key_index": -1,
            })
        if not out:
            raise EngineKeylessFailure(f"searxng@{base} returned no results")
        return out


@dataclass
class FirecrawlAdapter(_KeyedAdapter):
    """Firecrawl search API. ``base_url`` is overridable for self-hosted."""

    name: str = "firecrawl"
    base_url: str = "https://api.firecrawl.dev"
    timeout_s: float = 30.0

    def _call(self, *, query: str, key: str, max_results: int,
              region: str, **kwargs: Any) -> list[dict[str, Any]]:
        base = _normalize_base_url(self.base_url, default="https://api.firecrawl.dev")
        body_obj: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(max_results or 10), 25)),
        }
        scrape = kwargs.get("firecrawl_scrape_options")
        if isinstance(scrape, dict):
            body_obj["scrapeOptions"] = scrape
        body = json.dumps(body_obj).encode("utf-8")
        import urllib.request
        req = urllib.request.Request(
            f"{base}/v1/search",
            method="POST", data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Nerya/research",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"firecrawl@{base}: {type(exc).__name__}: {exc}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        items: list[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("web", "results", "pages", "items"):
                v = data.get(k)
                if isinstance(v, list):
                    items = v
                    break
        out: list[dict[str, Any]] = []
        for r in items[:max_results]:
            if not isinstance(r, dict):
                continue
            md = r.get("markdown")
            html_doc = r.get("html")
            description = (r.get("description") or r.get("snippet")
                           or (md if isinstance(md, str) else "")
                           or (r.get("metadata") or {}).get("description")
                           or "")
            out.append({
                "title": (r.get("title")
                          or (r.get("metadata") or {}).get("title")
                          or r.get("url") or ""),
                "url": r.get("url") or r.get("link") or "",
                "snippet": (description[:600] if isinstance(description, str)
                            else ""),
                "source": "firecrawl",
                "markdown_available": bool(md or html_doc),
            })
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _make_searxng(_keys: list[str], **cfg: Any) -> EngineAdapter:
    return SearXNGAdapter(
        base_url=str(cfg.get("base_url") or "http://127.0.0.1:8888"),
    )


def _make_firecrawl(keys: list[str], **cfg: Any) -> EngineAdapter:
    return FirecrawlAdapter(
        keys=keys,
        base_url=str(cfg.get("base_url") or "https://api.firecrawl.dev"),
    )


_ADAPTER_FACTORIES: dict[str, Callable[..., EngineAdapter]] = {
    "duckduckgo": lambda _keys, **_: DuckDuckGoHtmlAdapter(),
    "duckduckgo_html": lambda _keys, **_: DuckDuckGoHtmlAdapter(name="duckduckgo_html"),
    "duckduckgo_lite": lambda _keys, **_: DuckDuckGoLiteAdapter(),
    "exa": lambda keys, **_: ExaAdapter(keys=keys),
    "tavily": lambda keys, **_: TavilyAdapter(keys=keys),
    "perplexity": lambda keys, **_: PerplexityAdapter(keys=keys),
    "brave": lambda keys, **_: BraveAdapter(keys=keys),
    "serper": lambda keys, **_: SerperAdapter(keys=keys),
    "google": lambda keys, **_: SerperAdapter(keys=keys, name="serper"),
    "bing": lambda keys, **_: BingAdapter(keys=keys),
    "langsearch": lambda keys, **_: LangSearchAdapter(keys=keys),
    "searxng": _make_searxng,
    "firecrawl": _make_firecrawl,
}


def list_supported_engines() -> list[str]:
    return sorted(_ADAPTER_FACTORIES.keys())


def build_adapter(engine: str, keys: Iterable[str],
                  **config: Any) -> EngineAdapter:
    """Instantiate an adapter by name with its keys.

    ``config`` carries per-engine extras such as ``base_url`` (used by
    ``searxng`` and ``firecrawl``). Unknown engines raise ``ValueError``.
    Keys may be empty for keyless engines (DuckDuckGo, SearXNG); they
    are required for the rest.
    """
    factory = _ADAPTER_FACTORIES.get(engine)
    if not factory:
        raise ValueError(f"unsupported engine: {engine!r}")
    keys_list = [k.strip() for k in keys if k and k.strip()]
    return factory(keys_list, **config)


__all__ = [
    "EngineAdapter",
    "EngineError",
    "EngineKeyExhausted",
    "EngineKeylessFailure",
    "DuckDuckGoHtmlAdapter",
    "DuckDuckGoLiteAdapter",
    "ExaAdapter",
    "TavilyAdapter",
    "PerplexityAdapter",
    "BraveAdapter",
    "SerperAdapter",
    "BingAdapter",
    "LangSearchAdapter",
    "SearXNGAdapter",
    "FirecrawlAdapter",
    "build_adapter",
    "list_supported_engines",
]
