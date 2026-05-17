"""Default :class:`NewsFetcher` registry for built-in source names.

The strategy facade (:class:`nerya.strategies.context.StrategyNews`) accepts
an operator-supplied ``source_id -> fetcher`` mapping, but historically no
caller ever populated it. As a result every strategy ran with ``ctx.news.fetch``
returning an empty list — even though :func:`nerya.data.news.fetch_news`
already pulled live RSS from CoinDesk / Cointelegraph / BitcoinMagazine and
:func:`nerya.strategies.performance._fetch_yahoo_equity_news` already pulled
Yahoo Finance equity news.

This module ships pragmatic defaults bound to the two source names the v6
generator emits (``crypto`` and ``equity``). They are wired in
:func:`nerya.strategies.context.build_strategy_context` so EVERY code path
that builds a strategy context (the script runner, the agent-task executor,
the agent-team fallback path, ad-hoc tests) gets news content for free.

The fetchers are lazy and resilient:

* They never raise — failures degrade to ``[]`` so the strategy / Agent
  prompt still includes a ``data_quality.news_error`` field.
* They honour the :class:`NewsFetcher` signature
  ``fetcher(*, since, limit) -> list[dict]``.
* The equity fetcher derives tickers from ``manifest.markets`` so a
  strategy declaring ``yahoo:AAPL`` / ``yahoo:NVDA`` automatically pulls
  per-ticker news without extra config.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from .context import NewsFetcher

_LOG = logging.getLogger(__name__)


def _market_to_ticker(market: str) -> str:
    if not market:
        return ""
    if ":" in market:
        return market.split(":", 1)[1].strip().upper()
    return str(market).strip().upper()


def crypto_news_fetcher() -> NewsFetcher:
    """Return a :class:`NewsFetcher` backed by public crypto RSS feeds.

    Wraps :func:`nerya.data.news.fetch_news` (CoinDesk / Cointelegraph /
    BitcoinMagazine) so the source name ``crypto`` declared in
    ``strategy.yml -> news_sources`` resolves to live headlines.
    """

    def _fetch(*, since: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        try:
            from ..data.news import fetch_news
        except Exception as exc:  # pragma: no cover - import guard only
            _LOG.warning("crypto news fetcher import failed: %s", exc)
            return []
        try:
            rows = fetch_news(limit=int(limit or 20)) or []
        except Exception as exc:
            _LOG.warning("crypto news fetcher fetch failed: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            d = dict(row)
            d.setdefault("source", "crypto")
            d.setdefault("kind", "news")
            out.append(d)
        return out

    return _fetch


def equity_news_fetcher(markets: Iterable[str]) -> NewsFetcher:
    """Return a :class:`NewsFetcher` for ``equity`` — derives tickers
    from the strategy's declared ``markets``.

    Wraps :func:`nerya.strategies.performance._fetch_yahoo_equity_news`.
    A strategy declaring ``yahoo:AAPL, yahoo:NVDA`` will pull recent
    Yahoo Finance headlines for both tickers.
    """

    tickers = [t for t in (_market_to_ticker(m) for m in (markets or [])) if t]

    def _fetch(*, since: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        if not tickers:
            return []
        try:
            from .performance import _fetch_yahoo_equity_news
        except Exception as exc:  # pragma: no cover
            _LOG.warning("equity news fetcher import failed: %s", exc)
            return []
        try:
            payload = _fetch_yahoo_equity_news(list(tickers), limit=int(limit or 20)) or {}
        except Exception as exc:
            _LOG.warning("equity news fetcher fetch failed: %s", exc)
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            d = dict(row)
            d.setdefault("source", "equity")
            d.setdefault("kind", "news")
            out.append(d)
        return out

    return _fetch


def default_fetchers_for(
    source_names: Iterable[str],
    *,
    markets: Iterable[str] = (),
) -> dict[str, NewsFetcher]:
    """Build a ``{source -> fetcher}`` map for the configured source names.

    Unknown source names are silently skipped (operators can still register
    their own fetchers via the runner's ``news_fetchers`` argument or via
    :meth:`StrategyNews.register`).
    """

    wanted = {str(s).strip().lower() for s in (source_names or []) if str(s).strip()}
    out: dict[str, NewsFetcher] = {}
    if "crypto" in wanted:
        out["crypto"] = crypto_news_fetcher()
    if "equity" in wanted:
        out["equity"] = equity_news_fetcher(markets)
    return out


__all__ = [
    "crypto_news_fetcher",
    "equity_news_fetcher",
    "default_fetchers_for",
]
