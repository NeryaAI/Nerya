"""Deterministic fixture loader.

CSV layout (header required):

    ts,open,high,low,close,volume
    2024-01-01,42000,42500,41800,42400,123.4

Files live under ``tests/fixtures/candles/`` (or any caller-provided
directory). The loader normalises symbol names — ``BTC/USDT`` becomes
``btc_usdt`` — to map onto a single CSV per symbol/interval.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .base import (
    DatasetError,
    DatasetWindow,
    MarketDataset,
    OhlcvFrame,
    candles_from_rows,
)


class FixtureMarketDataset(MarketDataset):
    """File-system fixture dataset.

    Parameters
    ----------
    root:
        Directory containing ``<symbol>_<interval>.csv`` files.
    symbol_aliases:
        Optional mapping from raw input symbols to fixture file stems.
    """

    def __init__(
        self,
        root: Path,
        *,
        symbol_aliases: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.symbol_aliases = dict(symbol_aliases or {})

    # ------------------------------------------------------------------
    # MarketDataset
    # ------------------------------------------------------------------

    def supports(self, window: DatasetWindow) -> bool:
        try:
            self._resolve_path(window)
        except DatasetError:
            return False
        return True

    def load(self, window: DatasetWindow) -> OhlcvFrame:
        path = self._resolve_path(window)
        candles = candles_from_rows(_read_rows(path))
        if not candles:
            raise DatasetError(
                f"fixture_dataset_empty:{path.name}")
        frame = OhlcvFrame(
            symbol=window.symbol,
            interval=window.interval,
            candles=candles,
            source=f"fixture:{path.name}",
        )
        sliced = frame.slice(window.start_date, window.end_date)
        if not sliced.candles:
            raise DatasetError(
                "fixture_dataset_window_empty:"
                f"{path.name}:{window.start_date}..{window.end_date}")
        return sliced

    def list_symbols(self) -> Iterable[str]:
        if not self.root.exists():
            return []
        out = []
        for entry in sorted(self.root.glob("*.csv")):
            out.append(entry.stem)
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, window: DatasetWindow) -> Path:
        stem = self._fixture_stem(window.symbol, window.interval)
        path = self.root / f"{stem}.csv"
        if not path.is_file():
            raise DatasetError(f"fixture_dataset_missing:{path}")
        return path

    def _fixture_stem(self, symbol: str, interval: str) -> str:
        alias = self.symbol_aliases.get(symbol)
        if alias:
            return alias
        normalised = (
            symbol.lower()
            .replace("/", "_")
            .replace("-", "_")
            .replace(":", "_")
        )
        return f"{normalised}_{interval.lower()}"


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    if not rows:
        return []
    expected = {"ts", "open", "high", "low", "close"}
    missing = expected - set(rows[0].keys())
    if missing:
        raise DatasetError(
            f"fixture_dataset_bad_header:{path.name}:"
            f"missing={sorted(missing)}")
    return rows


__all__ = ["FixtureMarketDataset"]
