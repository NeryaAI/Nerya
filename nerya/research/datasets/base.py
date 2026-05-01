"""Dataset protocol and frame model.

Backtest engines receive OHLCV data via :class:`MarketDataset`.  The
protocol is deliberately tiny so future adapters (ccxt, polymarket,
onchain) can plug in without changing the runner.

Frames are immutable lists of :class:`Candle` records.  Engines never
mutate frame state, only iterate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from ...core.errors import NeryaError


class DatasetError(NeryaError):
    """Raised when a dataset adapter cannot satisfy a request."""


@dataclass(frozen=True)
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class DatasetWindow:
    symbol: str
    interval: str
    start_date: str
    end_date: str


@dataclass
class OhlcvFrame:
    """A normalised OHLCV frame for a single symbol/interval."""

    symbol: str
    interval: str
    candles: list[Candle]
    source: str = "fixture"

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.candles)

    def __iter__(self):  # pragma: no cover - trivial
        return iter(self.candles)

    def slice(self, start_date: str, end_date: str) -> "OhlcvFrame":
        cls = self.__class__
        kept = [c for c in self.candles
                if start_date <= c.ts[:10] <= end_date]
        return cls(symbol=self.symbol, interval=self.interval,
                   candles=kept, source=self.source)


class MarketDataset(Protocol):
    """Read-only OHLCV provider.

    Implementations must be deterministic, never call the network from
    tests, and never mutate workspace state.
    """

    def load(self, window: DatasetWindow) -> OhlcvFrame: ...

    def supports(self, window: DatasetWindow) -> bool: ...

    def list_symbols(self) -> Iterable[str]: ...


def candles_from_rows(rows: Sequence[dict]) -> list[Candle]:
    out: list[Candle] = []
    for row in rows:
        out.append(Candle(
            ts=str(row["ts"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0)),
        ))
    return out


__all__ = [
    "Candle",
    "DatasetError",
    "DatasetWindow",
    "MarketDataset",
    "OhlcvFrame",
    "candles_from_rows",
]
