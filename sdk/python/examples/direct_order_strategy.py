"""Demo C — direct-order strategy.

Submits a TradeIntent without any LLM involvement. The intent still flows
through Risk Gate, Approval Gate, and PaperExecution, and then an
immediate strategy-review is triggered.
"""

from __future__ import annotations

import json

import _bootstrap  # noqa: F401  -- keeps `import nerya_sdk` honest when running from the repo root
from nerya_sdk import connect


STRATEGY_ID = "btc_momentum"


def main() -> None:
    client = connect()

    result = client.trading.submit_intent(
        strategy_id=STRATEGY_ID,
        account_id="paper_main",
        market="PAPER:BTCUSDT",
        side="buy",
        size=250,
        size_unit="usd",
        order_type="market",
        confidence=0.6,
        reasoning="direct SDK buy — smoke test",
        source="sdk",
    )
    print("trade result:", json.dumps(result, indent=2, default=str))

    if result.get("status") in ("filled", "partial", "accepted"):
        review = client.strategy.review(
            STRATEGY_ID, result["session_id"], stage="immediate"
        )
        print("review:", json.dumps(review, indent=2, default=str))


if __name__ == "__main__":
    main()
