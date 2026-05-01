"""Real news feed — CoinDesk / Cointelegraph RSS (no auth) + optional
CryptoPanic when ``CRYPTOPANIC_TOKEN`` is resolvable via the secret vault.

Kept ``mock_news`` as the deterministic fallback used when the network is
unreachable or no source returned any items.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..connectors.http import HttpTransport, UrllibHttp
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
    tag_list_envelope,
)

log = logging.getLogger(__name__)


_DEFAULT_SOURCES: list[dict[str, str]] = [
    {"name": "coindesk",
     "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "cointelegraph",
     "url": "https://cointelegraph.com/rss"},
    {"name": "bitcoinmagazine",
     "url": "https://bitcoinmagazine.com/.rss/full/"},
]

# Cheap ticker extractor — matches $TICKER and all-caps 2-5 letter words.
_TICKER_RE = re.compile(r"(?:\$([A-Z]{2,6})\b|(?:\b([A-Z]{3,5})\b))")
_ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<([a-zA-Z:]+)[^>]*>(.*?)</\1>", re.DOTALL)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _tag(item: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", item, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    raw = m.group(1).strip()
    if raw.startswith("<![CDATA["):
        raw = raw[9:]
        if raw.endswith("]]>"):
            raw = raw[:-3]
    return html.unescape(_strip_tags(raw)).strip()


def _extract_tickers(text: str) -> list[str]:
    tickers: list[str] = []
    for m in _TICKER_RE.finditer(text or ""):
        sym = m.group(1) or m.group(2)
        if sym and sym not in tickers and sym not in _STOPWORDS:
            tickers.append(sym)
    return tickers[:5]


_STOPWORDS = {
    "THE", "AND", "FOR", "USD", "USDT", "NEW", "OLD", "NFT", "CEO", "CFO",
    "ETF", "SEC", "IPO", "API", "DEX", "CEX", "ATH", "ATL", "TVL", "FUD",
    "ICO", "AMA", "FYI", "DAO", "GDP", "CPI", "PPI", "FED", "LLC", "AI",
}


def _parse_rss(xml: str, *, source: str, limit: int) -> list[dict]:
    out: list[dict] = []
    for item in _ITEM_RE.findall(xml)[:limit]:
        title = _tag(item, "title")
        desc = _tag(item, "description")
        link = _tag(item, "link")
        pub = _tag(item, "pubDate") or _tag(item, "dc:date")
        if not title:
            continue
        tickers = _extract_tickers(title + " " + desc)
        out.append({
            "source": source,
            "title": title,
            "body": desc,
            "link": link,
            "published_at": pub,
            "tickers": tickers,
        })
    return out


def fetch_news(
    *,
    limit: int = 20,
    sources: list[dict[str, str]] | None = None,
    transport: HttpTransport | None = None,
    allow_mock: bool | None = None,
    config_like=None,
) -> list[dict]:
    """Pull the latest crypto headlines from public RSS feeds.

    When no source returns items, behaviour depends on authorisation:

    * If ``allow_mock`` or env ``NERYA_ALLOW_MOCK_DATA`` is set, returns
      :func:`mock_news` (explicit opt-in).
    * Otherwise returns ``[]`` with a degraded envelope — production
      runtime paths must never silently receive fake headlines.
    """
    http = transport or UrllibHttp(rate_limit_per_sec=4.0)
    srcs = sources or _DEFAULT_SOURCES
    all_items: list[dict] = []
    errors: list[str] = []
    for src in srcs:
        try:
            status, body = http.request("GET", src["url"], timeout=15.0)
        except Exception as exc:
            errors.append(f"{src.get('name')}:{type(exc).__name__}")
            log.debug("news source failed %s: %s", src.get("name"), exc)
            continue
        if status >= 400:
            errors.append(f"{src.get('name')}:http_{status}")
            continue
        xml = body.get("raw") if isinstance(body, dict) else ""
        if not xml:
            continue
        all_items.extend(_parse_rss(xml, source=src.get("name", ""), limit=limit))

    if not all_items:
        if resolve_allow_mock(allow_mock, config_like):
            return tag_list_envelope(mock_news(), mock_envelope(source="mock"))
        env = degraded_envelope("news",
                                error=",".join(errors) or "no_items")
        return tag_list_envelope([], env)
    seen: set[str] = set()
    unique: list[dict] = []
    for item in all_items:
        key = item["title"][:200]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    # per-item source already identifies the upstream feed
    return tag_list_envelope(unique, live_envelope(source="rss"))


_SAMPLES = [
    {"source": "mock", "title": "BTC ETF records record inflows",
     "body": "Spot BTC ETFs saw $500M of inflows...", "tickers": ["BTC"],
     "link": "", "published_at": ""},
    {"source": "mock", "title": "Random protocol announces partnership",
     "body": "A new partnership was announced...", "tickers": [],
     "link": "", "published_at": ""},
    {"source": "mock", "title": "Major exchange suffers outage",
     "body": "Trading was halted for 30 minutes...",
     "tickers": ["BTC", "ETH"], "link": "", "published_at": ""},
]


def mock_news() -> list[dict]:
    return [dict(s) for s in _SAMPLES]


__all__ = ["fetch_news", "mock_news"]
