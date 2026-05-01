"""Deterministic, fixture-driven backtest engine.

Plan §5 Task 5: bar-by-bar target-weight rebalancing on top of the
fixture dataset, emitting trades/equity-curve/metrics + structured
:class:`ValidationReport` artifacts under the candidate directory.

The runner imports nothing from ``../Vibe-Trading``.  All formulas live
inside Nerya so the runtime keeps a single source of truth.
"""
from __future__ import annotations

from .metrics import (
    Metrics,
    compute_metrics,
)
from .models import (
    BacktestResult,
    EquityPoint,
    TradeRecord,
)
from .runner import (
    BacktestRunner,
    BacktestRunnerError,
    run_backtest,
)

__all__ = [
    "BacktestResult",
    "BacktestRunner",
    "BacktestRunnerError",
    "EquityPoint",
    "Metrics",
    "TradeRecord",
    "compute_metrics",
    "run_backtest",
]
