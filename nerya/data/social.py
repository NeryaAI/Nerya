"""Real social feed — Reddit public JSON (no auth required).

Reddit exposes ``/r/<sub>/hot.json`` without authentication, subject to a
modest rate limit. We use it as the canonical public social signal; on
any error we fall back to :func:`mock_social`.
"""

from __future__ import annotations

import logging
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


_DEFAULT_SUBS = ["CryptoCurrency", "Bitcoin", "ethereum", "solana"]
_UA = "Nerya/1.0 (+https://github.com/nerya)"


def _normalise_post(subreddit: str, child: dict) -> dict:
    d = child.get("data") or {}
    score = int(d.get("score") or 0)
    num_comments = int(d.get("num_comments") or 0)
    # Coarse sentiment proxy — upvote ratio (0..1), scaled to [-1, 1].
    ratio = float(d.get("upvote_ratio") or 0.5)
    sentiment = max(-1.0, min(1.0, (ratio - 0.5) * 2.0))
    return {
        "source": "reddit",
        "subreddit": subreddit,
        "handle": f"u/{d.get('author', '') or ''}",
        "title": str(d.get("title") or ""),
        "text": str(d.get("selftext") or d.get("title") or ""),
        "url": f"https://reddit.com{d.get('permalink', '')}",
        "score": score,
        "num_comments": num_comments,
        "upvote_ratio": ratio,
        "sentiment": sentiment,
        "created_utc": float(d.get("created_utc") or 0.0),
    }


def fetch_social(
    *,
    subreddits: list[str] | None = None,
    limit_per_sub: int = 10,
    transport: HttpTransport | None = None,
    allow_mock: bool | None = None,
    config_like=None,
) -> list[dict]:
    """Pull hot posts from the configured subreddits.

    Returns mock data only when mock mode is explicitly authorised; otherwise
    an empty list with a degraded envelope.
    """
    http = transport or UrllibHttp(rate_limit_per_sec=1.5)
    subs = subreddits or _DEFAULT_SUBS
    out: list[dict] = []
    errors: list[str] = []
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/hot.json"
        try:
            status, body = http.request(
                "GET", url,
                params={"limit": limit_per_sub},
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=15.0,
            )
        except Exception as exc:
            errors.append(f"{sub}:{type(exc).__name__}")
            log.debug("reddit %s failed: %s", sub, exc)
            continue
        if status >= 400 or not isinstance(body, dict):
            errors.append(f"{sub}:http_{status}")
            continue
        children = ((body.get("data") or {}).get("children")) or []
        for c in children:
            if not isinstance(c, dict):
                continue
            if (c.get("kind") or "") != "t3":
                continue
            out.append(_normalise_post(sub, c))

    if not out:
        if resolve_allow_mock(allow_mock, config_like):
            return tag_list_envelope(mock_social(), mock_envelope(source="mock"))
        return tag_list_envelope(
            [],
            degraded_envelope("social", error=",".join(errors) or "no_items"),
        )
    out.sort(key=lambda p: p["score"], reverse=True)
    return tag_list_envelope(out, live_envelope(source="reddit"))


def mock_social() -> list[dict]:
    return [
        {"source": "mock", "subreddit": "CryptoCurrency", "handle": "@cz_binance",
         "title": "Something about markets", "text": "Something about markets",
         "url": "", "score": 300, "num_comments": 12,
         "upvote_ratio": 0.9, "sentiment": 0.3, "created_utc": 0.0},
        {"source": "mock", "subreddit": "Bitcoin", "handle": "@anonresearcher",
         "title": "BTC looks strong on the daily",
         "text": "BTC looks strong on the daily", "url": "",
         "score": 600, "num_comments": 25, "upvote_ratio": 0.95,
         "sentiment": 0.6, "created_utc": 0.0},
    ]


__all__ = ["fetch_social", "mock_social"]
