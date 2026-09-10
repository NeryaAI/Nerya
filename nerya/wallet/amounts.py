"""Exact human-amount → base-unit conversions for on-chain paths (F11).

Float math truncates against approved/frozen amounts (``8.1 * 1e18`` →
``8099999999999999488`` via float); ``Decimal(str(x))`` is exact for the
decimal literals callers actually pass. Approved floors (minOut, approval
floors) must round UP — never below what the operator approved — so floor
conversions use :func:`to_base_units_ceil`.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal


def to_base_units(amount: float | int | str, decimals: int) -> int:
    """Convert a human-readable amount to integer base units (truncating)."""
    return int(Decimal(str(amount)) * (10 ** Decimal(int(decimals))))


def to_base_units_ceil(amount: float | int | str, decimals: int) -> int:
    """Convert to base units, rounding UP — for approved minimum floors."""
    return int(
        (
            Decimal(str(amount)) * (10 ** Decimal(int(decimals)))
        ).to_integral_value(rounding=ROUND_CEILING)
    )
