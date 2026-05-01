"""Lightweight orderbook snapshot builder.

Production path uses ``connector.get_order_book``. When no connector is
provided we return a degraded envelope (empty book) rather than
fabricating bid/ask/spread from the mark price — silent synthetic
spreads violated the Phase 11 truth gate. Mock behaviour remains
available but requires explicit authorisation via ``allow_mock`` or
``NERYA_ALLOW_MOCK_DATA`` / ``runtime.mock_mode``.
"""

from __future__ import annotations

from typing import Any

from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)


def build_snapshot(
    *,
    market: str,
    mark: float | None = None,
    connector: Any | None = None,
    allow_mock: bool | None = None,
    config_like: Any | None = None,
) -> dict[str, Any]:
    """Return an orderbook snapshot tagged with a truth envelope.

    Resolution order:

    * If ``connector`` is given, call its ``get_order_book`` and tag the
      result with a live envelope.
    * Else if mock mode is explicitly authorised and ``mark`` is
      provided, return a synthetic bid/ask derived from mark with a
      ``mock`` envelope so callers can tell it is not real.
    * Else return an empty snapshot with a ``degraded`` envelope so
      consumers see the truth — no silent fabrication.
    """
    if connector is not None:
        try:
            snap = connector.get_order_book(market)
        except Exception as exc:
            env = degraded_envelope(
                "orderbook",
                error=f"{type(exc).__name__}: {exc}",
                venue=getattr(connector, "venue", "").lower() or "unknown",
            )
            return {
                "market": market, "bid": 0.0, "ask": 0.0, "mid": 0.0,
                "spread_bps": 0, "_envelope": env.as_dict(),
            }
        env = live_envelope(
            source=getattr(connector, "venue", "").lower() or "connector",
            venue=getattr(connector, "venue", "").lower() or "",
            connector_id=getattr(connector, "connector_id", ""),
        )
        if isinstance(snap, dict) and "_envelope" not in snap:
            snap = {**snap, "_envelope": env.as_dict()}
        return snap

    if resolve_allow_mock(allow_mock, config_like) and mark is not None:
        env = mock_envelope(source="mock", venue="mock")
        return {
            "market": market,
            "bid": mark * 0.999,
            "ask": mark * 1.001,
            "mid": float(mark),
            "spread_bps": 20,
            "_envelope": env.as_dict(),
        }

    env = degraded_envelope(
        "orderbook",
        error="no_connector_and_mock_not_authorised",
        venue="unknown",
    )
    return {
        "market": market, "bid": 0.0, "ask": 0.0, "mid": 0.0,
        "spread_bps": 0, "_envelope": env.as_dict(),
    }


__all__ = ["build_snapshot"]
