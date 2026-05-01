"""Backtest result records."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TradeRecord:
    ts: str
    symbol: str
    side: str
    price: float
    quantity: float
    notional: float
    fee: float
    slippage: float
    target_weight: float
    delta_weight: float
    reason: str = ""


@dataclass
class EquityPoint:
    ts: str
    equity: float
    cash: float
    holdings_value: float
    target_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestResult:
    strategy_id: str
    candidate_id: str
    config: dict[str, Any]
    equity_curve: list[EquityPoint]
    trades: list[TradeRecord]
    metrics: dict[str, float]
    bars_processed: int
    start_date: str
    end_date: str
    final_equity: float
    initial_equity: float
    data_coverage: dict[str, Any]
    engine: dict[str, Any]
    reproducibility: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)


__all__ = [
    "BacktestResult",
    "EquityPoint",
    "TradeRecord",
]
