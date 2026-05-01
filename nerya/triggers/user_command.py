"""User-command triggers emitted from the CLI / dashboard."""

from __future__ import annotations

from typing import Any

from .event import TriggerEvent


def build_user_command(
    kind: str,
    payload: dict[str, Any],
    *,
    target: str = "main",
    strategy_id: str | None = None,
) -> TriggerEvent:
    return TriggerEvent.new(
        source="user_command",
        kind=kind, payload=payload,
        target=target, strategy_id=strategy_id,
    )
