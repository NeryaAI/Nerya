"""Fee and slippage helpers."""

from __future__ import annotations


def venue_of(market: str) -> str:
    return market.split(":", 1)[0].upper() if ":" in market else "UNKNOWN"


def fee_bps_for(market: str, fee_bps_by_venue: dict[str, float]) -> float:
    return float(fee_bps_by_venue.get(venue_of(market), fee_bps_by_venue.get("UNKNOWN", 5.0)))


def slip_bps_for(market: str, slip_bps_by_venue: dict[str, float]) -> float:
    return float(slip_bps_by_venue.get(venue_of(market), slip_bps_by_venue.get("UNKNOWN", 5.0)))


def apply_slippage(price: float, side: str, slip_bps: float) -> float:
    sign = 1.0 if str(side).lower() in {"buy", "long"} else -1.0
    return float(price) * (1.0 + sign * float(slip_bps) / 10000.0)


def compute_fee(notional: float, fee_bps: float) -> float:
    return abs(float(notional)) * float(fee_bps) / 10000.0

