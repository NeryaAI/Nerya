"""Whale-wallet trigger demo.

Mock a chain watcher and emit a `whale.transfer` trigger to
`subagent:onchain_watcher`, which (in a real deployment) reads recent
transfers, computes cluster heuristics and emits a second trigger to the
main agent if the whale activity is relevant to an active strategy."""

from __future__ import annotations

import time

import _bootstrap  # noqa: F401  -- keeps `import nerya_sdk` honest when running from the repo root
from nerya_sdk import connect


MOCK_TRANSFERS = [
    {"chain": "evm", "from": "0xwhale1", "to": "0xcex_in",
     "symbol": "USDT", "amount_usd": 2_500_000},
    {"chain": "solana", "from": "WhaleSol1", "to": "SolCex1",
     "symbol": "USDC", "amount_usd": 600_000},
]


def main() -> None:
    c = connect()
    for tx in MOCK_TRANSFERS:
        if tx["amount_usd"] < 1_000_000:
            continue
        r = c.triggers.emit(
            source="script", kind="whale.transfer",
            payload=tx, target="subagent:onchain_watcher",
            strategy_id="btc_momentum",
            idempotency_key=f"whale-{tx['from']}-{int(time.time())}",
        )
        print("emit:", r)


if __name__ == "__main__":
    main()
