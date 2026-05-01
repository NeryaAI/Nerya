"""Helpers to emit price triggers (mock-friendly)."""

from __future__ import annotations

from typing import Any

from .event import TriggerEvent


def build_price_trigger(
    strategy_id: str,
    market: str,
    kind: str,
    payload: dict[str, Any],
    target: str = "main",
    idempotency_key: str | None = None,
) -> TriggerEvent:
    return TriggerEvent.new(
        source="price",
        kind=kind,
        payload={"market": market, **payload},
        target=target,
        strategy_id=strategy_id,
        idempotency_key=idempotency_key,
    )
