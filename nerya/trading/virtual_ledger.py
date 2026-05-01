"""Per-account virtual ledger for paper trading."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from ..core.atomic_write import atomic_write_text


@dataclass
class Position:
    market: str
    size: float = 0.0
    avg_price: float = 0.0
    realized_pnl_usd: float = 0.0


@dataclass
class LedgerState:
    account_id: str
    cash_usd: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    fees_paid_usd: float = 0.0
    trade_count: int = 0


class VirtualLedger:
    def __init__(self, path: Path, account_id: str, initial_balance_usd: float):
        self.path = Path(path)
        self._lock = RLock()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = _deserialize(data)
        else:
            self.state = LedgerState(account_id=account_id,
                                     cash_usd=float(initial_balance_usd))
            self._flush()

    def _flush(self) -> None:
        atomic_write_text(self.path, json.dumps(_serialize(self.state), indent=2))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return _serialize(self.state)

    def equity_estimate(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        with self._lock:
            eq = self.state.cash_usd
            for market, pos in self.state.positions.items():
                mark = marks.get(market, pos.avg_price)
                eq += pos.size * mark
            return eq

    def apply_fill(
        self, *, market: str, side: str, price: float, size: float, fee_usd: float
    ) -> dict[str, Any]:
        with self._lock:
            pos = self.state.positions.setdefault(market, Position(market=market))
            signed_size = size if side == "buy" else -size
            notional = price * size
            new_size = pos.size + signed_size
            # update cash
            if side == "buy":
                self.state.cash_usd -= notional + fee_usd
            else:
                self.state.cash_usd += notional - fee_usd
            self.state.fees_paid_usd += fee_usd
            self.state.trade_count += 1
            # update position
            if (pos.size == 0) or (pos.size > 0 and signed_size > 0) or (pos.size < 0 and signed_size < 0):
                # opening or adding to same direction — weighted avg
                if new_size != 0:
                    pos.avg_price = (pos.avg_price * pos.size + price * signed_size) / new_size if pos.size != 0 else price
                pos.size = new_size
            else:
                # reducing / flipping — realize pnl on the closed portion
                closing = min(abs(pos.size), abs(signed_size))
                pnl = (price - pos.avg_price) * (closing if pos.size > 0 else -closing)
                pos.realized_pnl_usd += pnl
                pos.size = new_size
                if new_size == 0:
                    pos.avg_price = 0.0
                elif (pos.size > 0 and signed_size > 0) or (pos.size < 0 and signed_size < 0):
                    pass
                else:
                    # flipped direction — new avg = current price
                    pos.avg_price = price
            self._flush()
            return {
                "cash_usd": self.state.cash_usd,
                "position": asdict(pos),
                "fees_paid_usd": self.state.fees_paid_usd,
            }


def _serialize(state: LedgerState) -> dict[str, Any]:
    return {
        "account_id": state.account_id,
        "cash_usd": state.cash_usd,
        "fees_paid_usd": state.fees_paid_usd,
        "trade_count": state.trade_count,
        "positions": {k: asdict(v) for k, v in state.positions.items()},
    }


def _deserialize(data: dict[str, Any]) -> LedgerState:
    return LedgerState(
        account_id=data["account_id"],
        cash_usd=float(data.get("cash_usd", 0)),
        fees_paid_usd=float(data.get("fees_paid_usd", 0)),
        trade_count=int(data.get("trade_count", 0)),
        positions={k: Position(**v) for k, v in (data.get("positions") or {}).items()},
    )


def open_ledger(paths, account_id: str, initial_balance_usd: float) -> VirtualLedger:
    paths.virtual_ledgers.mkdir(parents=True, exist_ok=True)
    return VirtualLedger(
        path=paths.virtual_ledgers / f"{account_id}.json",
        account_id=account_id,
        initial_balance_usd=initial_balance_usd,
    )
