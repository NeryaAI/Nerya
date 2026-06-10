"""Fetch recent RSS-backed news headlines.

Usage:
    python recent_news.py --json '{"topic":"热门财经新闻","limit":20}'
    python recent_news.py --json '{"sources":["crypto_rss"],"limit":12}'
    python recent_news.py --json '{"sources":["yahoo_finance_rss"],"tickers":["AAPL"],"limit":10}'
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

from nerya.connectors.http import UrllibHttp
from nerya.core.truth import live_envelope
from nerya.core import yaml_io
from nerya.data.news import fetch_news


_DEFAULT_MARKET_TICKERS = ("^GSPC", "^IXIC", "^DJI")
_RSS_ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL | re.IGNORECASE)
_SOURCE_ALIASES = {
    "crypto": "crypto_rss",
    "crypto_rss": "crypto_rss",
    "coindesk": "crypto_rss",
    "coindesk_rss": "crypto_rss",
    "cointelegraph": "crypto_rss",
    "cointelegraph_rss": "crypto_rss",
    "bitcoinmagazine": "crypto_rss",
    "bitcoinmagazine_rss": "crypto_rss",
    "bitcoin_magazine": "crypto_rss",
    "bitcoin_magazine_rss": "crypto_rss",
    "rss": "crypto_rss",
    "yahoo": "yahoo_finance_rss",
    "yahoo_rss": "yahoo_finance_rss",
    "yahoo_finance": "yahoo_finance_rss",
    "yahoo_finance_rss": "yahoo_finance_rss",
    "equity": "yahoo_finance_rss",
    "equity_rss": "yahoo_finance_rss",
    "market": "yahoo_finance_rss",
    "general_market": "yahoo_finance_rss",
    "custom": "custom_rss",
    "custom_rss": "custom_rss",
    "news_feeds": "custom_rss",
    "news_feeds.yml": "custom_rss",
    "news_feeds.yaml": "custom_rss",
}
_CUSTOM_SOURCE_SENTINEL = "custom_rss"
_EQUITY_TOPIC_RE = re.compile(
    r"(?i)(\b(economy|economic|finance|financial|market|stock|stocks|"
    r"equity|equities|macro|inflation|fed|rate|rates)\b|财经|经济|股市|美股|通胀)"
)
_CRYPTO_TOPIC_RE = re.compile(
    r"(?i)(\b(crypto|bitcoin|btc|ethereum|eth|solana|defi|onchain)\b|加密|币圈|链上)"
)
_LOOKBACK_HOURS_RE = re.compile(
    r"(?i)(?:(?:last|past|recent|within)\s*)?(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|h)\b"
    r"|(?:最近|过去|過去|近)?\s*(\d+(?:\.\d+)?)\s*(?:小时|小時)"
)
_TOPIC_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9.]{2,5}\b")
_NON_TICKER_TOKENS = {
    "API",
    "CEO",
    "CFO",
    "CPI",
    "ETF",
    "FOMC",
    "GDP",
    "HTTP",
    "HTTPS",
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


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        if str(args.payload_file).strip() == "-":
            raw = sys.stdin.read().strip()
            return json.loads(raw) if raw else {}
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def _extract_topic_tickers(topic: str) -> list[str]:
    out: list[str] = []
    for match in _TOPIC_TICKER_RE.finditer(topic or ""):
        ticker = match.group(0).upper().strip(".")
        if ticker in _NON_TICKER_TOKENS or ticker in out:
            continue
        out.append(ticker)
        if len(out) >= 12:
            break
    return out


def _sources_for(topic: str, raw_sources: Any, tickers: list[str]) -> list[str]:
    sources: list[str] = []
    for item in _str_list(raw_sources):
        source = _SOURCE_ALIASES.get(item.lower(), item.lower())
        if source not in sources:
            sources.append(source)
    if sources:
        return sources
    if tickers or _EQUITY_TOPIC_RE.search(topic or ""):
        return ["yahoo_finance_rss"]
    if _CRYPTO_TOPIC_RE.search(topic or ""):
        return ["crypto_rss"]
    return ["yahoo_finance_rss", "crypto_rss"]


def _workspace_root() -> Path:
    raw = os.environ.get("NERYA_WORKSPACE")
    if raw:
        return Path(raw).expanduser()
    return Path.cwd()


def _is_enabled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _safe_feed_name(value: Any, *, index: int) -> str:
    raw = str(value or f"custom_{index}").strip().lower()
    safe = re.sub(r"[^a-z0-9_.-]+", "_", raw).strip("._-")
    return safe or f"custom_{index}"


def _normalise_custom_feed(entry: Any, *, index: int) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    if not _is_enabled(entry.get("enabled", True)):
        return None
    feed_type = str(entry.get("type") or entry.get("kind") or "rss").strip().lower()
    if feed_type and feed_type not in {"rss", "feed", "xml"}:
        return None
    url = str(entry.get("url") or entry.get("link") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    name = _safe_feed_name(
        entry.get("name") or entry.get("id") or entry.get("source"),
        index=index,
    )
    return {"name": name, "url": url}


def _load_workspace_custom_feeds() -> list[dict[str, str]]:
    root = _workspace_root()
    doc: Any = {}
    for filename in ("news_feeds.yml", "news_feeds.yaml"):
        candidate = root / filename
        if not candidate.exists():
            continue
        try:
            doc = yaml_io.load(candidate, default={}) or {}
        except Exception:
            return []
        break
    if isinstance(doc, list):
        raw_feeds = doc
    elif isinstance(doc, dict):
        raw_feeds = doc.get("feeds") or doc.get("sources") or []
    else:
        raw_feeds = []
    if not isinstance(raw_feeds, list):
        return []
    feeds: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for idx, entry in enumerate(raw_feeds, start=1):
        feed = _normalise_custom_feed(entry, index=idx)
        if feed is None or feed["url"] in seen_urls:
            continue
        seen_urls.add(feed["url"])
        feeds.append(feed)
    return feeds[:20]


def _rss_tag(raw_item: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}[^>]*>(.*?)</{tag}>",
        raw_item or "",
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    raw = match.group(1).strip()
    if raw.startswith("<![CDATA["):
        raw = raw[9:]
        if raw.endswith("]]>"):
            raw = raw[:-3]
    return html.unescape(re.sub(r"<[^>]+>", "", raw).strip())


def _fetch_yahoo_rss(tickers: list[str], *, limit: int) -> dict[str, Any]:
    if limit <= 0:
        return {"items": [], "errors": []}
    http = UrllibHttp(rate_limit_per_sec=4.0)
    env = live_envelope(
        "yahoo_finance_rss",
        provider="finance.yahoo.com",
    ).as_dict()
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for ticker in tickers[:12]:
        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={url_quote(ticker)}&region=US&lang=en-US"
        )
        try:
            status, body = http.request("GET", url, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ticker}:yahoo_rss:{type(exc).__name__}")
            continue
        if status >= 400:
            errors.append(f"{ticker}:yahoo_rss:http_{status}")
            continue
        xml = body.get("raw") if isinstance(body, dict) else ""
        rows = _parse_yahoo_rss(xml, ticker=ticker, env=env)
        if not rows:
            errors.append(f"{ticker}:yahoo_rss:no_items")
            continue
        items.extend(rows)
        if len(items) >= limit:
            break
    if not items and not errors:
        errors.append("yahoo_rss:no_items")
    return {"items": items[:limit], "errors": errors}


def _parse_yahoo_rss(
    xml: str,
    *,
    ticker: str,
    env: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_item in _RSS_ITEM_RE.findall(xml or ""):
        title = _rss_tag(raw_item, "title")
        if not title:
            continue
        out.append(
            {
                "source": "yahoo_finance_rss",
                "title": title,
                "summary": _rss_tag(raw_item, "description"),
                "published_at": _rss_tag(raw_item, "pubDate"),
                "url": _rss_tag(raw_item, "link"),
                "tickers": [ticker],
                "_envelope": env,
            }
        )
    return out


def _normalise_item(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "source": row.get("source") or source,
        "title": row.get("title") or "",
        "summary": row.get("summary") or row.get("body") or row.get("description") or "",
        "url": row.get("url") or row.get("link") or "",
        "published_at": row.get("published_at") or row.get("published") or "",
        "tickers": list(row.get("tickers") or row.get("matched_tickers") or []),
        "_envelope": row.get("_envelope"),
    }


def _dedupe(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("url") or item.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _coerce_positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _lookback_hours(payload: dict[str, Any], *, topic: str) -> float | None:
    for key in (
        "lookback_hours",
        "max_age_hours",
        "recent_hours",
        "hours",
        "window_hours",
    ):
        value = _coerce_positive_float(payload.get(key))
        if value is not None:
            return min(value, 24.0 * 30.0)
    for text in (topic, str(payload.get("query") or "")):
        for match in _LOOKBACK_HOURS_RE.finditer(text or ""):
            raw = match.group(1) or match.group(2)
            value = _coerce_positive_float(raw)
            if value is not None:
                return min(value, 24.0 * 30.0)
    return None


def _utc_now(payload: dict[str, Any]) -> datetime:
    raw = payload.get("now")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _parse_published_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _apply_time_filter(
    items: list[dict[str, Any]],
    *,
    payload: dict[str, Any],
    topic: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    hours = _lookback_hours(payload, topic=topic)
    if hours is None:
        return items, None
    now = _utc_now(payload)
    since = now - timedelta(hours=hours)
    kept: list[dict[str, Any]] = []
    old_count = 0
    missing_count = 0
    for item in items:
        published_at = _parse_published_at(
            item.get("published_at") or item.get("published")
        )
        if published_at is None:
            missing_count += 1
            continue
        if published_at < since:
            old_count += 1
            continue
        kept.append(item)
    return kept, {
        "lookback_hours": hours,
        "now": now.isoformat(),
        "since": since.isoformat(),
        "original_count": len(items),
        "kept_count": len(kept),
        "dropped_count": old_count + missing_count,
        "old_count": old_count,
        "missing_timestamp_count": missing_count,
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    topic = str(payload.get("topic") or "")
    limit = max(1, min(int(payload.get("limit") or 20), 50))
    tickers = _str_list(payload.get("tickers"))
    if not tickers and not _CRYPTO_TOPIC_RE.search(topic or ""):
        tickers = _extract_topic_tickers(topic)
    raw_source_list = _str_list(payload.get("sources"))
    sources = _sources_for(topic, payload.get("sources"), tickers)
    custom_sources = _load_workspace_custom_feeds()
    include_custom = bool(custom_sources) and (
        not raw_source_list or _CUSTOM_SOURCE_SENTINEL in sources
    )
    built_in_sources = [s for s in sources if s != _CUSTOM_SOURCE_SENTINEL]
    items: list[dict[str, Any]] = []
    errors: list[str] = []

    if "yahoo_finance_rss" in built_in_sources:
        yahoo_tickers = tickers or list(_DEFAULT_MARKET_TICKERS)
        yahoo = _fetch_yahoo_rss(yahoo_tickers, limit=limit)
        for row in yahoo.get("items") or []:
            if isinstance(row, dict):
                items.append(_normalise_item(row, source="yahoo_finance_rss"))
        errors.extend(str(e) for e in (yahoo.get("errors") or []) if e)

    if "crypto_rss" in built_in_sources and len(items) < limit:
        try:
            rows = fetch_news(limit=limit, allow_mock=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"crypto_rss:{type(exc).__name__}")
            rows = []
        for row in rows or []:
            if isinstance(row, dict):
                items.append(_normalise_item(row, source="crypto_rss"))

    if include_custom and len(items) < limit:
        try:
            rows = fetch_news(
                limit=limit - len(items),
                sources=custom_sources,
                allow_mock=False,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"custom_rss:{type(exc).__name__}")
            rows = []
        for row in rows or []:
            if isinstance(row, dict):
                items.append(_normalise_item(row, source=_CUSTOM_SOURCE_SENTINEL))

    reported_sources = list(sources)
    if include_custom and _CUSTOM_SOURCE_SENTINEL not in reported_sources:
        reported_sources.append(_CUSTOM_SOURCE_SENTINEL)
    deduped = _dedupe(items, limit=max(limit, len(items)))
    filtered, time_filter = _apply_time_filter(deduped, payload=payload, topic=topic)
    out = filtered[:limit]
    if time_filter is not None:
        time_filter["kept_count"] = len(out)
    result: dict[str, Any] = {
        "ok": bool(out),
        "source": "rss",
        "sources": reported_sources,
        "custom_feed_count": len(custom_sources),
        "tickers": tickers or (
            list(_DEFAULT_MARKET_TICKERS) if "yahoo_finance_rss" in built_in_sources else []
        ),
        "count": len(out),
        "items": out,
        "errors": errors[:12],
        "notes": [
            "RSS is a fast first pass for latest/news turns.",
            "Use research/web_search_fetch afterward when broader source diversity or full articles are required.",
        ],
    }
    if time_filter is not None:
        result["time_filter"] = time_filter
    if not out and errors:
        result["error"] = "; ".join(errors[:6])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    args = parser.parse_args()
    payload = _load_payload(args)
    result = run(payload)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
