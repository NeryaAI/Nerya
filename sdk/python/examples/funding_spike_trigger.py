"""Funding-rate spike trigger demo.

Mock a perp funding feed and emit a `funding.spike` trigger to the main
agent when funding crosses a threshold. The trigger carries no order; it
only wakes up the agent and leaves the decision to the risk_critic /
execution_planner subagents."""

from __future__ import annotations

import time

import _bootstrap  # noqa: F401  -- keeps `import nerya_sdk` honest when running from the repo root
from nerya_sdk import connect


def pull_mock_funding() -> list[dict]:
    return [
        {"market": "MOCK:BTC-PERP", "funding_rate_bps": 12.0, "ts": time.time()},
        {"market": "MOCK:ETH-PERP", "funding_rate_bps": 55.0, "ts": time.time()},
        {"market": "MOCK:SOL-PERP", "funding_rate_bps": -42.0, "ts": time.time()},
    ]


def main() -> None:
    c = connect()
    print("routes:", len(c.triggers.list_routes()), "loaded")
    for row in pull_mock_funding():
        if abs(row["funding_rate_bps"]) < 30:
            continue
        r = c.triggers.emit(
            source="script", kind="funding.spike",
            payload=row, target="main",
            strategy_id="btc_momentum",
            idempotency_key=f"fund-{row['market']}-{int(row['ts'])}",
        )
        print("emit:", r)


if __name__ == "__main__":
    main()
