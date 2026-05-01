"""Position management surface.

The legacy export ``get_positions`` (a paper-ledger flattener used by
the dashboard) stays intact so existing callers keep working. New code
should prefer the event-sourced :class:`PositionBook` from
:mod:`nerya.trading.position_book`, which reads from the durable
``positions`` / ``position_events`` tables introduced by migration v3.
"""

from .portfolio import get_positions  # noqa: F401
from .position_book import (  # noqa: F401
    Position,
    PositionBook,
    PositionSide,
    PositionSource,
)

__all__ = [
    "get_positions",
    "Position",
    "PositionBook",
    "PositionSide",
    "PositionSource",
]
