"""Position book — event-sourced live + paper position projection.

the position book is the *projection* of
the durable fill ledger; nothing else in Nerya is allowed to compute
"what is my position right now" from scratch. Risk gate, executors,
dashboard — they all read here.

Design:

* Each position has its own row in the ``positions`` table, with a
  stable ``position_id`` and a ``closed_at`` timestamp instead of a
  separate "closed positions" table. This keeps reconciliation cheap
  ("show me everything I've ever held in BTC/USDT") and lets the
  dashboard distinguish open vs closed by a single index.

* Every change is also recorded in ``position_events`` so we can
  replay the full lifecycle for audit and PnL attribution.

* Apply a fill via :meth:`PositionBook.apply_fill`. The book picks the
  current open position (per account/strategy/market) or creates a
  new one. Reduce-then-flip is fully supported: the existing position
  closes (with realised PnL), and a fresh one opens in the new
  direction.

* :meth:`open_positions` and :meth:`get_open` are the read APIs the
  rest of the kernel uses.

We deliberately do not bake mark-price look-ups into the book — the
caller passes ``mark_price`` when they want to refresh
``unrealized_pnl_usd``, otherwise the field stays at its last
computed value.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.ids import (
    event_id as _new_event_id,
    position_id as _new_position_id,
)
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect

log = logging.getLogger(__name__)

PositionSide = Literal["long", "short"]
PositionSource = Literal["paper", "live", "reconciled", "manual_import"]


@dataclass
class Position:
    position_id: str
    account_id: str
    strategy_id: str
    market: str
    venue: str
    side: PositionSide
    size_base: float
    avg_entry_price: float
    mark_price: float | None = None
    liquidation_price: float | None = None
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    leverage: float = 1.0
    source: PositionSource = "paper"
    executor_id: str | None = None
    protection_id: str | None = None
    opened_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None and abs(self.size_base) > 1e-12

    @property
    def notional_usd(self) -> float:
        return abs(self.size_base) * float(self.avg_entry_price or 0.0)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------


class PositionBook:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    # -- read -------------------------------------------------------------------
    def get_open(
        self,
        *,
        account_id: str,
        strategy_id: str,
        market: str,
    ) -> Position | None:
        row = self._con_lazy().execute(
            """
            SELECT * FROM positions
             WHERE account_id = ? AND strategy_id = ? AND market = ?
               AND closed_at IS NULL
             ORDER BY opened_at DESC
             LIMIT 1
            """,
            (account_id, strategy_id, market),
        ).fetchone()
        return _row_to_position(row) if row else None

    def get_by_id(self, position_id: str) -> Position | None:
        row = self._con_lazy().execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,),
        ).fetchone()
        return _row_to_position(row) if row else None

    def open_positions(
        self, *, account_id: str | None = None, strategy_id: str | None = None
    ) -> list[Position]:
        sql = "SELECT * FROM positions WHERE closed_at IS NULL"
        params: list[Any] = []
        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id)
        if strategy_id:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        sql += " ORDER BY opened_at"
        rows = self._con_lazy().execute(sql, tuple(params)).fetchall()
        return [_row_to_position(r) for r in rows]

    def history(
        self, *, account_id: str | None = None, market: str | None = None, limit: int = 100
    ) -> list[Position]:
        sql = "SELECT * FROM positions"
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        if market:
            clauses.append("market = ?")
            params.append(market)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._con_lazy().execute(sql, tuple(params)).fetchall()
        return [_row_to_position(r) for r in rows]

    # -- mutation ---------------------------------------------------------------
    def apply_fill(
        self,
        *,
        account_id: str,
        strategy_id: str,
        market: str,
        side: Literal["buy", "sell"],
        price: float,
        size_base: float,
        fee_usd: float = 0.0,
        funding_usd: float = 0.0,
        venue: str = "",
        leverage: float = 1.0,
        source: PositionSource = "paper",
        executor_id: str | None = None,
        order_id: str | None = None,
        fill_id: str | None = None,
        ts: float | None = None,
    ) -> Position:
        """Apply a single fill to the position book.

        Returns the open Position *after* the fill is applied. If the
        fill closes the position the returned object has
        ``closed_at != None`` and ``is_open == False``.
        """
        ts = ts if ts is not None else time.time()
        signed = float(size_base) if side == "buy" else -float(size_base)
        fee = float(fee_usd or 0.0)
        funding = float(funding_usd or 0.0)

        existing = self.get_open(account_id=account_id, strategy_id=strategy_id, market=market)
        if existing is None:
            position = Position(
                position_id=_new_position_id(),
                account_id=account_id,
                strategy_id=strategy_id,
                market=market,
                venue=venue or _infer_venue(market),
                side="long" if signed > 0 else "short",
                size_base=signed,
                avg_entry_price=float(price),
                mark_price=float(price),
                realized_pnl_usd=0.0,
                unrealized_pnl_usd=0.0,
                fees_usd=fee,
                funding_usd=funding,
                leverage=leverage,
                source=source,
                executor_id=executor_id,
                opened_at=ts,
                updated_at=ts,
            )
            self._insert(position)
            self._record_event(
                position_id=position.position_id,
                kind="opened",
                ts=ts,
                size_delta=signed,
                price=price,
                fee_delta=fee,
                order_id=order_id,
                fill_id=fill_id,
            )
            return position

        prev_size = existing.size_base
        new_size = prev_size + signed
        same_dir = (prev_size > 0 and signed > 0) or (prev_size < 0 and signed < 0)
        pnl_delta = 0.0
        kind: str
        new_avg = existing.avg_entry_price
        new_realized = existing.realized_pnl_usd
        new_fees = existing.fees_usd + fee
        new_funding = existing.funding_usd + funding

        if same_dir:
            # Adding to the position — weighted-average entry.
            if new_size != 0:
                new_avg = (
                    (existing.avg_entry_price * prev_size) + (price * signed)
                ) / new_size
            kind = "increased"
        else:
            closing = min(abs(prev_size), abs(signed))
            sign = 1.0 if prev_size > 0 else -1.0
            pnl_delta = (price - existing.avg_entry_price) * sign * closing
            new_realized += pnl_delta
            if abs(new_size) < 1e-12:
                # Flat close.
                self._close(existing.position_id, ts=ts, realized=new_realized,
                            fees=new_fees, funding=new_funding, mark_price=price)
                self._record_event(
                    position_id=existing.position_id,
                    kind="closed",
                    ts=ts,
                    size_delta=signed,
                    price=price,
                    pnl_delta=pnl_delta,
                    fee_delta=fee,
                    order_id=order_id,
                    fill_id=fill_id,
                )
                refreshed = self.get_by_id(existing.position_id)
                # ``refreshed`` is guaranteed non-None.
                assert refreshed is not None
                return refreshed
            elif (new_size > 0) != (prev_size > 0):
                # Reduce + flip — close existing, open a fresh row in
                # the new direction with the leftover size at fill price.
                self._close(existing.position_id, ts=ts, realized=new_realized,
                            fees=new_fees, funding=new_funding, mark_price=price)
                self._record_event(
                    position_id=existing.position_id,
                    kind="reversed",
                    ts=ts,
                    size_delta=signed,
                    price=price,
                    pnl_delta=pnl_delta,
                    fee_delta=fee,
                    order_id=order_id,
                    fill_id=fill_id,
                )
                flipped = Position(
                    position_id=_new_position_id(),
                    account_id=account_id,
                    strategy_id=strategy_id,
                    market=market,
                    venue=existing.venue,
                    side="long" if new_size > 0 else "short",
                    size_base=new_size,
                    avg_entry_price=float(price),
                    mark_price=float(price),
                    realized_pnl_usd=0.0,
                    fees_usd=0.0,
                    funding_usd=0.0,
                    leverage=leverage,
                    source=source,
                    executor_id=executor_id,
                    opened_at=ts,
                    updated_at=ts,
                )
                self._insert(flipped)
                self._record_event(
                    position_id=flipped.position_id,
                    kind="opened",
                    ts=ts,
                    size_delta=new_size,
                    price=price,
                    order_id=order_id,
                    fill_id=fill_id,
                )
                return flipped
            else:
                kind = "reduced"

        # Update the existing row.
        self._update(
            position_id=existing.position_id,
            size_base=new_size,
            avg_entry_price=new_avg,
            realized_pnl_usd=new_realized,
            fees_usd=new_fees,
            funding_usd=new_funding,
            mark_price=float(price),
            ts=ts,
        )
        self._record_event(
            position_id=existing.position_id,
            kind=kind,
            ts=ts,
            size_delta=signed,
            price=price,
            pnl_delta=pnl_delta,
            fee_delta=fee,
            order_id=order_id,
            fill_id=fill_id,
        )
        refreshed = self.get_by_id(existing.position_id)
        assert refreshed is not None
        return refreshed

    def update_mark(
        self, position_id: str, mark_price: float, *, ts: float | None = None,
    ) -> None:
        ts = ts if ts is not None else time.time()
        position = self.get_by_id(position_id)
        if position is None or not position.is_open:
            return
        side = 1.0 if position.side == "long" else -1.0
        unrealized = (mark_price - position.avg_entry_price) * abs(position.size_base) * side
        con = self._con_lazy()
        con.execute(
            """
            UPDATE positions
               SET mark_price = ?,
                   unrealized_pnl_usd = ?,
                   updated_at = ?
             WHERE position_id = ?
            """,
            (float(mark_price), float(unrealized), ts, position_id),
        )

    def attach_protection(self, position_id: str, protection_id: str) -> None:
        con = self._con_lazy()
        con.execute(
            "UPDATE positions SET protection_id = ?, updated_at = ? WHERE position_id = ?",
            (protection_id, time.time(), position_id),
        )
        self._record_event(
            position_id=position_id,
            kind="protection_attached",
            ts=time.time(),
            payload={"protection_id": protection_id},
        )

    def attach_executor(self, position_id: str, executor_id: str) -> None:
        con = self._con_lazy()
        con.execute(
            "UPDATE positions SET executor_id = ?, updated_at = ? WHERE position_id = ?",
            (executor_id, time.time(), position_id),
        )

    def mark_externally_closed(self, position_id: str, *, reason: str = "external_close") -> None:
        con = self._con_lazy()
        con.execute(
            """
            UPDATE positions
               SET closed_at = COALESCE(closed_at, ?),
                   updated_at = ?,
                   size_base = 0,
                   meta_json = json_set(COALESCE(meta_json, '{}'), '$.external_close_reason', ?)
             WHERE position_id = ?
            """,
            (time.time(), time.time(), reason, position_id),
        )
        self._record_event(
            position_id=position_id, kind="external_change_detected",
            ts=time.time(), payload={"reason": reason},
        )

    # -- internal ---------------------------------------------------------------
    def _insert(self, position: Position) -> None:
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO positions (
                position_id, account_id, strategy_id, market, venue, side,
                size_base, avg_entry_price, mark_price, liquidation_price,
                realized_pnl_usd, unrealized_pnl_usd, fees_usd, funding_usd,
                leverage, source, executor_id, protection_id,
                opened_at, updated_at, closed_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.position_id, position.account_id, position.strategy_id,
                position.market, position.venue, position.side,
                position.size_base, position.avg_entry_price, position.mark_price,
                position.liquidation_price,
                position.realized_pnl_usd, position.unrealized_pnl_usd,
                position.fees_usd, position.funding_usd,
                position.leverage, position.source,
                position.executor_id, position.protection_id,
                position.opened_at, position.updated_at, position.closed_at,
                json.dumps(position.meta),
            ),
        )

    def _update(
        self,
        *,
        position_id: str,
        size_base: float,
        avg_entry_price: float,
        realized_pnl_usd: float,
        fees_usd: float,
        funding_usd: float,
        mark_price: float | None,
        ts: float,
    ) -> None:
        con = self._con_lazy()
        side = "long" if size_base >= 0 else "short"
        con.execute(
            """
            UPDATE positions
               SET size_base = ?,
                   avg_entry_price = ?,
                   realized_pnl_usd = ?,
                   fees_usd = ?,
                   funding_usd = ?,
                   mark_price = COALESCE(?, mark_price),
                   side = ?,
                   updated_at = ?
             WHERE position_id = ?
            """,
            (
                size_base, avg_entry_price, realized_pnl_usd, fees_usd, funding_usd,
                mark_price, side, ts, position_id,
            ),
        )

    def _close(
        self, position_id: str, *, ts: float, realized: float,
        fees: float, funding: float, mark_price: float,
    ) -> None:
        con = self._con_lazy()
        con.execute(
            """
            UPDATE positions
               SET size_base = 0,
                   realized_pnl_usd = ?,
                   fees_usd = ?,
                   funding_usd = ?,
                   mark_price = ?,
                   updated_at = ?,
                   closed_at = ?
             WHERE position_id = ?
            """,
            (realized, fees, funding, mark_price, ts, ts, position_id),
        )

    def _record_event(
        self,
        *,
        position_id: str,
        kind: str,
        ts: float,
        size_delta: float = 0.0,
        price: float | None = None,
        pnl_delta: float = 0.0,
        fee_delta: float = 0.0,
        order_id: str | None = None,
        fill_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO position_events (
                event_id, position_id, kind, ts,
                size_delta, price, pnl_delta, fee_delta,
                order_id, fill_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_event_id(), position_id, kind, ts,
                size_delta, price, pnl_delta, fee_delta,
                order_id, fill_id, json.dumps(payload or {}),
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_position(row: Any) -> Position:
    return Position(
        position_id=str(row["position_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        market=str(row["market"]),
        venue=str(row["venue"] or ""),
        side=str(row["side"]),  # type: ignore[arg-type]
        size_base=float(row["size_base"] or 0.0),
        avg_entry_price=float(row["avg_entry_price"] or 0.0),
        mark_price=(float(row["mark_price"]) if row["mark_price"] is not None else None),
        liquidation_price=(float(row["liquidation_price"]) if row["liquidation_price"] is not None else None),
        realized_pnl_usd=float(row["realized_pnl_usd"] or 0.0),
        unrealized_pnl_usd=float(row["unrealized_pnl_usd"] or 0.0),
        fees_usd=float(row["fees_usd"] or 0.0),
        funding_usd=float(row["funding_usd"] or 0.0),
        leverage=float(row["leverage"] or 1.0),
        source=str(row["source"] or "paper"),  # type: ignore[arg-type]
        executor_id=(row["executor_id"] or None),
        protection_id=(row["protection_id"] or None),
        opened_at=float(row["opened_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
        closed_at=(float(row["closed_at"]) if row["closed_at"] is not None else None),
        meta=json.loads(str(row["meta_json"] or "{}")),
    )


def _infer_venue(market: str) -> str:
    if ":" in market:
        return market.split(":", 1)[0].upper()
    return ""


__all__ = [
    "Position",
    "PositionBook",
    "PositionSide",
    "PositionSource",
]
