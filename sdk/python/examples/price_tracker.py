"""Demo A — price tracker.

Pulls mock BTC price, uses a light-tier LLM to decide if a breakout is
happening, and emits a TriggerEvent targeted at `subagent:market_analyst`.
The main agent then picks the trigger up (via the inbox or CLI) and
submits a TradeIntent — which goes through Risk Gate and PaperExecution.

Run:
    python sdk/python/examples/price_tracker.py
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

import _bootstrap  # noqa: F401  -- keeps `import nerya_sdk` honest when running from the repo root
from nerya_sdk import connect


MARKET = "PAPER:BTCUSDT"
STRATEGY_ID = "btc_momentum"


def fake_tick() -> dict[str, Any]:
    """Produce a mock candle — biased toward a breakout so the demo runs."""
    price = 80000 + random.random() * 4000
    return {"market": MARKET, "price": round(price, 2),
            "change_pct": round((random.random() - 0.3) * 0.05, 4)}


def main() -> None:
    client = connect()
    for _ in range(3):
        tick = fake_tick()
        text = (f"Market {tick['market']} price={tick['price']} "
                f"change={tick['change_pct']:.2%}")
        classification = client.llm.classify(
            prompt=text, labels=["breakout", "noise"], caller="script:price_tracker",
        )
        print("classified:", classification)
        label = (classification.get("result") or {}).get("label")
        if label == "breakout" or tick["change_pct"] > 0.01:
            result = client.triggers.emit(
                source="script", kind="price.breakout",
                payload={"symbol": "BTC", **tick},
                target="subagent:market_analyst",
                strategy_id=STRATEGY_ID,
                idempotency_key=f"btc-bo-{int(time.time())}",
            )
            print("emitted trigger:", json.dumps(result, indent=2, default=str))

            # Execute the full main-agent turn so the slice is end-to-end.
            from nerya.agent.kernel import AgentKernel
            kernel = AgentKernel(config=client._client().config,
                                 skills=client._client().skills)
            turn = kernel.run_turn(
                trigger={"id": result.get("event_id"),
                         "kind": "price.breakout", "source": "price_tracker",
                         "payload": tick},
                strategy_id=STRATEGY_ID,
            )
            print("turn decision:", json.dumps(turn.decision, indent=2, default=str))
            print("turn actions:", json.dumps(turn.actions, indent=2, default=str))
            break
        time.sleep(0.5)


if __name__ == "__main__":
    main()
