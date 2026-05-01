"""Backtest metrics.

Implements the deterministic metrics required by research runtime spec §5
Task 5 step 6.  All math lives inside Nerya — no research runtime import.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Sequence

from .models import EquityPoint, TradeRecord


_BAR_DAYS = {
    "1d": 1.0,
    "1D": 1.0,
    "12h": 0.5,
    "8h": 1 / 3,
    "6h": 0.25,
    "4h": 1 / 6,
    "1h": 1 / 24,
    "30m": 1 / 48,
    "15m": 1 / 96,
    "5m": 1 / 288,
    "1m": 1 / 1440,
}


@dataclass
class Metrics:
    total_return: float
    annualized_return: float
    max_drawdown: float
    volatility: float
    sharpe: float
    sortino: float
    turnover: float
    trade_count: int
    win_rate: float
    exposure: float
    bars: int

    def asdict(self) -> dict[str, float | int]:
        return {
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "max_drawdown": self.max_drawdown,
            "volatility": self.volatility,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "turnover": self.turnover,
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "exposure": self.exposure,
            "bars": self.bars,
        }


def compute_metrics(
    equity_curve: Sequence[EquityPoint],
    trades: Sequence[TradeRecord],
    *,
    interval: str = "1D",
    initial_capital_usd: float,
) -> Metrics:
    if not equity_curve:
        return Metrics(
            total_return=0.0,
            annualized_return=0.0,
            max_drawdown=0.0,
            volatility=0.0,
            sharpe=0.0,
            sortino=0.0,
            turnover=0.0,
            trade_count=0,
            win_rate=0.0,
            exposure=0.0,
            bars=0,
        )

    equities = [p.equity for p in equity_curve]
    initial = float(equities[0]) if equities[0] else float(initial_capital_usd)
    final = float(equities[-1])
    total_return = (final / initial) - 1.0 if initial else 0.0

    # Bar-to-bar returns.
    returns: list[float] = []
    for prev, cur in zip(equities[:-1], equities[1:]):
        if prev <= 0:
            returns.append(0.0)
        else:
            returns.append((cur / prev) - 1.0)

    bars = len(equity_curve)
    days_per_bar = _BAR_DAYS.get(interval, 1.0)
    horizon_days = max(bars * days_per_bar, 1.0)
    years = horizon_days / 365.0
    if years > 0 and 1.0 + total_return > 0:
        annualized = (1.0 + total_return) ** (1.0 / years) - 1.0
    else:
        annualized = 0.0

    if len(returns) >= 2:
        volatility = pstdev(returns)
    else:
        volatility = 0.0

    avg_return = mean(returns) if returns else 0.0
    sharpe = (avg_return / volatility) * math.sqrt(252 * days_per_bar) \
        if volatility else 0.0

    downside = [r for r in returns if r < 0]
    if len(downside) >= 2:
        downside_dev = pstdev(downside)
    else:
        downside_dev = 0.0
    sortino = (avg_return / downside_dev) * math.sqrt(252 * days_per_bar) \
        if downside_dev else 0.0

    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        peak = max(peak, eq)
        if peak > 0:
            dd = (eq - peak) / peak
            if dd < max_dd:
                max_dd = dd

    turnover = 0.0
    if initial > 0:
        turnover = sum(t.notional for t in trades) / max(initial, 1.0)

    trade_count = len(trades)

    exposure_samples = []
    for point in equity_curve:
        if point.equity <= 0:
            exposure_samples.append(0.0)
        else:
            exposure_samples.append(point.holdings_value / point.equity)
    exposure = mean(exposure_samples) if exposure_samples else 0.0

    win_rate = _win_rate(equity_curve, trades)

    return Metrics(
        total_return=float(total_return),
        annualized_return=float(annualized),
        max_drawdown=float(max_dd),
        volatility=float(volatility),
        sharpe=float(sharpe),
        sortino=float(sortino),
        turnover=float(turnover),
        trade_count=int(trade_count),
        win_rate=float(win_rate),
        exposure=float(exposure),
        bars=int(bars),
    )


def _win_rate(
    equity_curve: Sequence[EquityPoint], trades: Sequence[TradeRecord]
) -> float:
    """Approximate win rate based on equity changes around trade timestamps."""

    if not trades or not equity_curve:
        return 0.0
    by_ts = {p.ts: i for i, p in enumerate(equity_curve)}
    wins = 0
    counted = 0
    for trade in trades:
        idx = by_ts.get(trade.ts)
        if idx is None or idx + 1 >= len(equity_curve):
            continue
        before = equity_curve[idx].equity
        after = equity_curve[idx + 1].equity
        counted += 1
        if (trade.side == "buy" and after >= before) or \
                (trade.side == "sell" and after >= before):
            wins += 1
    if not counted:
        return 0.0
    return wins / counted


__all__ = ["Metrics", "compute_metrics"]
