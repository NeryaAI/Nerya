"""Symbol-aware dataset router.

Implements the symbol-detection logic used by research backtests. The
router accepts a list of symbols and returns the
correct loader (fixture for tests, ccxt/polymarket/onchain for
production once those adapters land).

The regex routing lives inside Nerya so the runtime never imports an
external research tree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .base import DatasetError, DatasetWindow, MarketDataset, OhlcvFrame
from .fixtures import FixtureMarketDataset


MarketKind = Literal["crypto", "polymarket", "evm", "fixture", "unknown"]


# Default fixture directory — tests reference this constant directly.
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[3] \
    / "tests" / "fixtures" / "candles"


_CRYPTO_PATTERNS = [
    re.compile(r"^[A-Z]{2,10}/[A-Z]{2,10}$"),                # BTC/USDT
    re.compile(r"^[A-Z]{2,10}-[A-Z]{2,10}(?:-PERP)?$"),       # BTC-USDT
    re.compile(r"^(?:BINANCE|OKX|BYBIT|COINBASE):[A-Z]{2,20}$"),
]

_POLYMARKET_PATTERNS = [
    re.compile(r"^poly:.+$", re.IGNORECASE),
    re.compile(r"^polymarket:.+$", re.IGNORECASE),
    re.compile(r"^market_id:[A-Za-z0-9_-]+$"),
]

_EVM_TOKEN_PATTERNS = [
    re.compile(r"^evm:[A-Za-z0-9_-]+$"),
    re.compile(r"^[A-Za-z0-9]{2,10}/0x[a-fA-F0-9]{40}$"),
]


@dataclass
class RouterDecision:
    symbol: str
    market: MarketKind


class DatasetRouter:
    """Pick a dataset adapter for a given symbol set.

    Tests should pass ``data_source="fixture"`` and a ``fixture_dir``
    pointing at deterministic CSVs.  Future code can attach more
    adapters via :meth:`register_adapter`.
    """

    def __init__(
        self,
        *,
        fixture_dir: Path | str | None = None,
        fixture_aliases: dict[str, str] | None = None,
    ) -> None:
        self.fixture_dir = Path(fixture_dir) if fixture_dir else DEFAULT_FIXTURE_DIR
        self._fixture = FixtureMarketDataset(
            self.fixture_dir, symbol_aliases=fixture_aliases or {})
        self._adapters: dict[MarketKind, MarketDataset] = {}

    # ------------------------------------------------------------------
    # Adapter registration
    # ------------------------------------------------------------------

    def register_adapter(
        self, market: MarketKind, dataset: MarketDataset
    ) -> None:
        self._adapters[market] = dataset

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def detect(self, symbol: str) -> RouterDecision:
        return RouterDecision(symbol=symbol, market=detect_market(symbol))

    def resolve(
        self,
        window: DatasetWindow,
        *,
        data_source: str = "fixture",
    ) -> tuple[MarketDataset, RouterDecision]:
        decision = self.detect(window.symbol)
        if data_source == "fixture":
            return self._fixture, decision
        if data_source not in {"ccxt", "polymarket", "onchain"}:
            raise DatasetError(f"unsupported_data_source:{data_source!r}")
        adapter = self._adapters.get(decision.market)
        if adapter is None:
            raise DatasetError(
                "unsupported_market:"
                f"{decision.market}:no_adapter_registered_for:{data_source}"
            )
        return adapter, decision

    def load(
        self,
        window: DatasetWindow,
        *,
        data_source: str = "fixture",
    ) -> OhlcvFrame:
        adapter, _ = self.resolve(window, data_source=data_source)
        return adapter.load(window)


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def detect_market(symbol: str) -> MarketKind:
    if not isinstance(symbol, str) or not symbol.strip():
        return "unknown"
    s = symbol.strip()
    for pattern in _CRYPTO_PATTERNS:
        if pattern.match(s):
            return "crypto"
    for pattern in _POLYMARKET_PATTERNS:
        if pattern.match(s):
            return "polymarket"
    for pattern in _EVM_TOKEN_PATTERNS:
        if pattern.match(s):
            return "evm"
    if s.startswith("fixture:"):
        return "fixture"
    return "unknown"


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


__all__ = [
    "DEFAULT_FIXTURE_DIR",
    "DatasetRouter",
    "MarketKind",
    "RouterDecision",
    "detect_market",
    "normalize_symbol",
]
