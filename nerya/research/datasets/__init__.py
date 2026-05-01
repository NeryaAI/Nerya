"""Research dataset adapters.

Provides a minimal, deterministic, network-free interface over OHLCV
data for backtests/shadow runs.  At test time the only loader exposed
is :class:`FixtureMarketDataset` which reads CSV files under
``tests/fixtures/candles/``.

Inspired by ``../Vibe-Trading/agent/backtest/runner.py:121`` (market
detection) but reimplemented in Nerya so the runtime keeps a single
source of truth.  No imports from ``../Vibe-Trading``.
"""
from __future__ import annotations

from .base import (
    Candle,
    DatasetError,
    DatasetWindow,
    MarketDataset,
    OhlcvFrame,
)
from .fixtures import FixtureMarketDataset
from .router import (
    DEFAULT_FIXTURE_DIR,
    DatasetRouter,
    MarketKind,
    detect_market,
    normalize_symbol,
)

__all__ = [
    "Candle",
    "DEFAULT_FIXTURE_DIR",
    "DatasetError",
    "DatasetRouter",
    "DatasetWindow",
    "FixtureMarketDataset",
    "MarketDataset",
    "MarketKind",
    "OhlcvFrame",
    "detect_market",
    "normalize_symbol",
]
