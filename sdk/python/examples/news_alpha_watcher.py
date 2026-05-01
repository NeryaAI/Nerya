"""Demo B — news alpha watcher (LLM tiered).

Pipeline:
  1. Light tier filters noise via `llm.classify` (cheap).
  2. Only alpha items pay for the high tier's `analyze_signal`.
  3. Anything that comes back with a `recommended_action` emits a
     `news.alpha` trigger — we never place trades directly from a script.

Every LLM call is enforced by the script's `llm_policy`; if the script
manifest sets `allowed_tiers: [light]` the high-tier step will be
rejected by the gateway before a dollar is spent.
"""

from __future__ import annotations

import json

import _bootstrap  # noqa: F401  -- keeps `import nerya_sdk` honest when running from the repo root
from nerya_sdk import connect


CALLER = "script:news_alpha_watcher"

MOCK_HEADLINES = [
    "SEC approves BTC ETF spot trading, BTC surges to new highs",
    "Minor coin listing on regional exchange",
    "Rate cut expected at FOMC meeting; markets rally",
    "Celebrity endorses memecoin",
    "Major exchange reports a hack of 50m usd",
]


def main() -> None:
    client = connect()

    alpha_items: list[dict] = []
    for headline in MOCK_HEADLINES:
        cls = client.llm.classify(
            prompt=headline,
            labels=["alpha", "noise", "risk"],
            caller=CALLER,
        )
        label = (cls.get("result") or {}).get("label")
        print(f"[light] {headline!r:60s} -> {label}")
        if label == "alpha":
            alpha_items.append({"headline": headline, "classification": cls})

    for item in alpha_items:
        analysis = client.llm.analyze_signal(
            context=item["headline"],
            caller=CALLER,
        )
        parsed = analysis.get("parsed") if isinstance(analysis, dict) else None
        print(f"[high ] {item['headline']!r:60s} -> {parsed}")
        if isinstance(parsed, dict) and parsed.get("recommended_action"):
            result = client.triggers.emit(
                source="script", kind="news.alpha",
                payload={"headline": item["headline"], "analysis": parsed},
                target="main",
                strategy_id="btc_momentum",
                idempotency_key=f"news-alpha-{abs(hash(item['headline']))}",
            )
            print("  [trigger]", json.dumps(result, default=str))


if __name__ == "__main__":
    main()
