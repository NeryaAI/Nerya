"""Portfolio accounting for backtest fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Position:
    market: str
    qty: float = 0.0
    avg_price: float = 0.0
    opened_ts: int | None = None

    @property
    def side(self) -> str:
        if self.qty > 0:
            return "long"
        if self.qty < 0:
            return "short"
        return "flat"


@dataclass
class PortfolioState:
    initial_cash: float
    cash: float | None = None
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_cum: float = 0.0
    slippage_cum: float = 0.0
    peak_equity: float = 0.0
    last_equity: float = 0.0
    equity_series: list[tuple[int, float]] = field(default_factory=list)
    exposure_bars: int = 0
    bars_seen: int = 0
    turnover_notional: float = 0.0

    def __post_init__(self) -> None:
        if self.cash is None:
            self.cash = float(self.initial_cash)
        self.peak_equity = float(self.initial_cash)
        self.last_equity = float(self.initial_cash)

    def position(self, market: str) -> Position:
        return self.positions.get(market, Position(market=market))

    def open_positions_count(self) -> int:
        return sum(1 for p in self.positions.values() if abs(p.qty) > 1e-12)

    def apply_fill(self, fill: dict[str, Any]) -> None:
        market = str(fill["market"])
        side = str(fill["side"]).lower()
        qty = float(fill["qty"])
        price = float(fill["price"])
        fee = float(fill.get("fee", 0.0))
        notional = qty * price
        self.fees_cum += fee
        self.slippage_cum += abs(float(fill.get("slippage_usd", 0.0)))
        self.turnover_notional += abs(notional)
        pos = self.positions.get(market, Position(market=market))
        signed_qty = qty if side == "buy" else -qty
        if pos.qty == 0 or (pos.qty > 0 and signed_qty > 0) or (pos.qty < 0 and signed_qty < 0):
            old_notional = abs(pos.qty) * pos.avg_price
            new_qty_abs = abs(pos.qty) + abs(signed_qty)
            pos.avg_price = (old_notional + abs(signed_qty) * price) / new_qty_abs if new_qty_abs else 0.0
            pos.qty += signed_qty
            pos.opened_ts = int(fill.get("ts", 0)) or pos.opened_ts
            self.cash = float(self.cash or 0.0) - notional - fee
        else:
            closing_qty = min(abs(pos.qty), abs(signed_qty))
            pnl = closing_qty * (price - pos.avg_price) * (1.0 if pos.qty > 0 else -1.0)
            self.realized_pnl += pnl
            self.cash = float(self.cash or 0.0) - notional - fee
            pos.qty += signed_qty
            if abs(pos.qty) < 1e-12:
                pos.qty = 0.0
                pos.avg_price = 0.0
                pos.opened_ts = None
        self.positions[market] = pos

    def mark_to_market(self, ts: int, prices: dict[str, float]) -> float:
        value = float(self.cash or 0.0)
        exposed = False
        for market, pos in self.positions.items():
            if abs(pos.qty) <= 1e-12:
                continue
            exposed = True
            value += pos.qty * float(prices.get(market, pos.avg_price))
        self.last_equity = value
        self.peak_equity = max(self.peak_equity, value)
        self.equity_series.append((int(ts), value))
        self.bars_seen += 1
        if exposed:
            self.exposure_bars += 1
        return value

    def equity(self) -> float:
        return self.last_equity

    def max_drawdown_pct(self) -> float:
        peak = None
        max_dd = 0.0
        for _, equity in self.equity_series:
            peak = equity if peak is None else max(peak, equity)
            if peak and peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)
        return max_dd

    def snapshot(self) -> dict[str, Any]:
        return {
            "cash": float(self.cash or 0.0),
            "equity": self.last_equity,
            "realized_pnl": self.realized_pnl,
            "fees_cum": self.fees_cum,
            "positions": {
                market: {"qty": p.qty, "avg_price": p.avg_price, "side": p.side}
                for market, p in self.positions.items()
                if abs(p.qty) > 1e-12
            },
            "max_drawdown_pct": self.max_drawdown_pct(),
        }

