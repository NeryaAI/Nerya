"""Conflict detector — flags intents that would cross existing open orders."""

from __future__ import annotations

from ..core import jsonl
from ..core.paths import WorkspacePaths


def find_conflicts(paths: WorkspacePaths, strategy_id: str, market: str, side: str) -> list[dict]:
    orders_path = paths.strategy_history(strategy_id) / "orders.jsonl"
    open_orders = []
    for row in jsonl.read_all(orders_path):
        payload = row.get("payload", {})
        if (payload.get("status") in ("accepted", "partial") and
                payload.get("market") == market and
                payload.get("side") != side):
            open_orders.append(payload)
    return open_orders
