"""News-keyword trigger demo.

External watchers scrape some news feed and push keyword matches into
Nerya via the Trigger SDK. They deliberately do NOT classify or decide
anything. The news_interpreter subagent + main agent do the reasoning."""

from __future__ import annotations

import time

import _bootstrap  # noqa: F401  -- keeps `import nerya_sdk` honest when running from the repo root
from nerya_sdk import connect


FEED = [
    {"source": "coindesk", "title": "BTC ETF outflows",
     "keywords": ["btc", "etf", "outflow"]},
    {"source": "theblock", "title": "Bull market continues",
     "keywords": ["bull"]},
    {"source": "cryptopanic", "title": "SEC sues large exchange",
     "keywords": ["sec", "lawsuit"]},
]


def main() -> None:
    c = connect()
    for item in FEED:
        r = c.triggers.emit(
            source="script", kind="news.keyword",
            payload=item, target="subagent:news_interpreter",
            strategy_id="btc_momentum",
            idempotency_key=f"news-{item['source']}-{int(time.time())}",
        )
        print("emit:", r)


if __name__ == "__main__":
    main()
