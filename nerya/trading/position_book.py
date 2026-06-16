"""Position book — event-sourced merged position + per-strategy share projection.

the position book is the *projection* of the durable fill ledger;
nothing else in Nerya is allowed to compute "what is my position right now"
from scratch. Risk gate, executors, dashboard — they all read here.

Design (v6, merged-by-account+market):

* Each ``(account_id, market)`` has **one** open row in the ``positions``
  table. Two strategies trading the same exchange + symbol no longer
  produce two independent rows; the broker only sees one merged
  position so neither does Nerya.
* Per-strategy attribution lives in ``position_shares``: each strategy
  carries its own size, avg-entry, realized PnL, fees and funding.
* The merged ``positions`` row is the algebraic sum:
  ``size_base = SUM(share.size_share_base)`` and
  ``avg_entry_price = SUM(share.size * share.avg) / size_base`` (cost
  basis), so opposite-direction shares net cleanly (strategy A long 0.5
  + strategy B short 0.3 → merged long 0.2).
* Realized PnL is **share-attributed**: when strategy A closes its slice
  the realized PnL hits A's share with A's own avg-entry — *not* the
  merged blended avg-entry. The merged ``realized_pnl_usd`` field is
  the sum of share realized PnLs, kept materialised for cheap reads.
* Every change is also recorded in ``position_events`` so we can
  replay the full lifecycle for audit and PnL attribution.

Apply a fill via :meth:`PositionBook.apply_fill`. The book finds the
open merged position (per account/market) or creates a fresh one, then
threads the fill through the originating strategy's share — opening,
adding, reducing or flipping it as needed. Reduce-then-flip is fully
supported at *both* levels: the share that submitted the fill can
flip even while the merged position stays open (because another
strategy still has size on the opposite side).

:meth:`open_positions`, :meth:`get_open` and :meth:`get_open_merged`
are the read APIs the rest of the kernel uses; per-strategy callers
should use :meth:`get_share` / :meth:`list_shares` when they want
strategy-level numbers (own PnL, own avg-entry).

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
from typing import Any, Iterable, Literal

from ..core.ids import (
    event_id as _new_event_id,
    position_id as _new_position_id,
)
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect

log = logging.getLogger(__name__)

PositionSide = Literal["long", "short"]
PositionSource = Literal["paper", "live", "reconciled", "manual_import"]


# Sentinel written into ``positions.strategy_id`` for merged rows so
# legacy ``WHERE strategy_id = ?`` filters can't mistakenly match a
# merged row. Strategy attribution always reads ``position_shares``.
MERGED_STRATEGY_SENTINEL = "__merged__"


@dataclass
class Position:
    """Merged position row keyed by ``(account_id, market)``.

    ``strategy_id`` retains its column for compatibility but holds
    ``MERGED_STRATEGY_SENTINEL`` after v6. Use :class:`PositionShare`
    when the caller cares about which strategy owns the slice.
    """

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
    def is_merged(self) -> bool:
        """True after v6 — strategy attribution is in ``position_shares``."""
        return self.strategy_id == MERGED_STRATEGY_SENTINEL

    @property
    def notional_usd(self) -> float:
        return abs(self.size_base) * float(self.avg_entry_price or 0.0)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PositionShare:
    """Per-strategy slice of a merged :class:`Position`."""

    share_id: str
    position_id: str
    account_id: str
    strategy_id: str
    market: str
    venue: str
    size_share_base: float
    avg_entry_share_price: float
    realized_pnl_share_usd: float = 0.0
    fees_share_usd: float = 0.0
    funding_share_usd: float = 0.0
    opened_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None and abs(self.size_share_base) > 1e-12

    @property
    def side(self) -> PositionSide:
        return "long" if self.size_share_base >= 0 else "short"

    @property
    def notional_usd(self) -> float:
        return abs(self.size_share_base) * float(self.avg_entry_share_price or 0.0)

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

    def close(self) -> None:
        """Release the lazily-opened SQLite connection, if any.

        Short-lived books (e.g. the 5s background order poller builds a
        fresh instance every tick) must call this each cycle, otherwise
        the connection's file descriptor leaks until the process exhausts
        its fd limit and wedges.
        """
        con, self._con = self._con, None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    # -- read -------------------------------------------------------------------
    def get_open(
        self,
        *,
        account_id: str,
        strategy_id: str,
        market: str,
    ) -> Position | None:
        """Return the merged open position only if ``strategy_id`` has a
        share in it. Callers that simply want the merged row regardless
        of strategy should use :meth:`get_open_merged`.
        """
        share = self.get_share(
            strategy_id=strategy_id, account_id=account_id, market=market,
        )
        if share is None or not share.is_open:
            return None
        return self.get_by_id(share.position_id)

    def get_open_merged(
        self, *, account_id: str, market: str,
    ) -> Position | None:
        row = self._con_lazy().execute(
            """
            SELECT * FROM positions
             WHERE account_id = ? AND market = ?
               AND closed_at IS NULL
             LIMIT 1
            """,
            (account_id, market),
        ).fetchone()
        return _row_to_position(row) if row else None

    def get_by_id(self, position_id: str) -> Position | None:
        row = self._con_lazy().execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,),
        ).fetchone()
        return _row_to_position(row) if row else None

    def get_share(
        self,
        *,
        strategy_id: str,
        account_id: str,
        market: str,
    ) -> PositionShare | None:
        row = self._con_lazy().execute(
            """
            SELECT * FROM position_shares
             WHERE strategy_id = ? AND account_id = ? AND market = ?
               AND closed_at IS NULL
             LIMIT 1
            """,
            (strategy_id, account_id, market),
        ).fetchone()
        return _row_to_share(row) if row else None

    def list_shares(
        self, position_id: str, *, open_only: bool = True,
    ) -> list[PositionShare]:
        sql = "SELECT * FROM position_shares WHERE position_id = ?"
        if open_only:
            sql += " AND closed_at IS NULL"
        sql += " ORDER BY opened_at"
        rows = self._con_lazy().execute(sql, (position_id,)).fetchall()
        return [_row_to_share(r) for r in rows]

    def open_positions(
        self, *, account_id: str | None = None, strategy_id: str | None = None,
    ) -> list[Position]:
        """List merged open positions.

        ``strategy_id`` filters to positions where that strategy has an
        open share — preserving the pre-v6 contract that
        ``open_positions(strategy_id=X)`` shows "X's portfolio".
        """
        con = self._con_lazy()
        if strategy_id is not None:
            sql = (
                "SELECT p.* FROM positions p "
                "JOIN position_shares s ON s.position_id = p.position_id "
                "WHERE p.closed_at IS NULL "
                "  AND s.closed_at IS NULL "
                "  AND s.strategy_id = ?"
            )
            params: list[Any] = [strategy_id]
            if account_id:
                sql += " AND p.account_id = ?"
                params.append(account_id)
            sql += " GROUP BY p.position_id ORDER BY p.opened_at"
            rows = con.execute(sql, tuple(params)).fetchall()
        else:
            sql = "SELECT * FROM positions WHERE closed_at IS NULL"
            params = []
            if account_id:
                sql += " AND account_id = ?"
                params.append(account_id)
            sql += " ORDER BY opened_at"
            rows = con.execute(sql, tuple(params)).fetchall()
        return [_row_to_position(r) for r in rows]

    def list_shares_history(
        self,
        *,
        strategy_id: str | None = None,
        account_id: str | None = None,
        market: str | None = None,
        open_only: bool = False,
        limit: int = 100_000,
    ) -> list[PositionShare]:
        """List shares — open + closed — with optional filters.

        ``open_only=True`` is the cheap path for live PnL dashboards;
        callers that want realized history across all shares for a
        strategy use the default (``open_only=False``).
        """
        sql = "SELECT * FROM position_shares"
        clauses: list[str] = []
        params: list[Any] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        if market is not None:
            clauses.append("market = ?")
            params.append(market)
        if open_only:
            clauses.append("closed_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._con_lazy().execute(sql, tuple(params)).fetchall()
        return [_row_to_share(r) for r in rows]

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
        """Apply a single fill to the merged position + originating share.

        Returns the **merged** ``Position`` after the fill is applied;
        callers wanting strategy-level numbers should look up
        :meth:`get_share` for the same ``(strategy_id, account_id,
        market)`` after this returns. If the fill closes both the
        share AND the merged position, the returned object has
        ``closed_at != None`` and ``is_open == False``.
        """
        ts = ts if ts is not None else time.time()
        signed = float(size_base) if side == "buy" else -float(size_base)
        if abs(signed) < 1e-12:
            # Empty fill — nothing to do. Still return the current
            # merged state so callers can chain.
            merged = self.get_open_merged(account_id=account_id, market=market)
            if merged is None:
                raise ValueError("apply_fill: empty fill with no open position")
            return merged
        fee = float(fee_usd or 0.0)
        funding = float(funding_usd or 0.0)
        venue_value = venue or _infer_venue(market)

        merged = self.get_open_merged(account_id=account_id, market=market)
        share = (
            self.get_share(strategy_id=strategy_id, account_id=account_id, market=market)
            if merged is not None
            else None
        )

        # --- 1. open a brand-new merged position + first share -------
        if merged is None:
            position = Position(
                position_id=_new_position_id(),
                account_id=account_id,
                strategy_id=MERGED_STRATEGY_SENTINEL,
                market=market,
                venue=venue_value,
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
                position_id=position.position_id, kind="opened", ts=ts,
                size_delta=signed, price=price, fee_delta=fee,
                order_id=order_id, fill_id=fill_id,
                payload={"strategy_id": strategy_id},
            )
            self._open_share(
                position_id=position.position_id, account_id=account_id,
                strategy_id=strategy_id, market=market, venue=venue_value,
                size=signed, avg=float(price), fee=fee, funding=funding,
                ts=ts,
            )
            return position

        # --- 2. update the originating strategy's share --------------
        share_realized_delta, share = self._apply_fill_to_share(
            merged=merged, share=share,
            account_id=account_id, strategy_id=strategy_id, market=market,
            venue=venue_value, signed=signed, price=price,
            fee=fee, funding=funding, ts=ts,
        )

        # --- 3. recompute the merged row from all currently-open shares
        merged = self._refresh_merged_from_shares(
            position_id=merged.position_id,
            ts=ts,
            mark_price=float(price),
            fee_delta=fee, funding_delta=funding,
            realized_delta=share_realized_delta,
            leverage=leverage,
            source=source,
        )

        self._record_event(
            position_id=merged.position_id,
            kind=_kind_for_fill(merged, signed),
            ts=ts,
            size_delta=signed, price=price,
            pnl_delta=share_realized_delta, fee_delta=fee,
            order_id=order_id, fill_id=fill_id,
            payload={"strategy_id": strategy_id, "share_id": share.share_id},
        )
        return merged

    def update_mark(
        self, position_id: str, mark_price: float, *, ts: float | None = None,
    ) -> None:
        ts = ts if ts is not None else time.time()
        position = self.get_by_id(position_id)
        if position is None or not position.is_open:
            return
        side = 1.0 if position.size_base >= 0 else -1.0
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

    def mark_externally_closed(
        self, position_id: str, *, reason: str = "external_close",
    ) -> None:
        con = self._con_lazy()
        now = time.time()
        con.execute(
            """
            UPDATE positions
               SET closed_at = COALESCE(closed_at, ?),
                   updated_at = ?,
                   size_base = 0,
                   meta_json = json_set(COALESCE(meta_json, '{}'), '$.external_close_reason', ?)
             WHERE position_id = ?
            """,
            (now, now, reason, position_id),
        )
        con.execute(
            """
            UPDATE position_shares
               SET closed_at = COALESCE(closed_at, ?),
                   updated_at = ?,
                   size_share_base = 0
             WHERE position_id = ? AND closed_at IS NULL
            """,
            (now, now, position_id),
        )
        self._record_event(
            position_id=position_id, kind="external_change_detected",
            ts=now, payload={"reason": reason},
        )

    # -- internal: share-level math --------------------------------------------
    def _apply_fill_to_share(
        self,
        *,
        merged: Position,
        share: PositionShare | None,
        account_id: str,
        strategy_id: str,
        market: str,
        venue: str,
        signed: float,
        price: float,
        fee: float,
        funding: float,
        ts: float,
    ) -> tuple[float, PositionShare]:
        """Update (or open) the strategy's share for this fill.

        Returns ``(realized_pnl_delta_for_this_fill, share_after_update)``.
        """
        # New share — opens straight from the fill.
        if share is None or not share.is_open:
            new_share = self._open_share(
                position_id=merged.position_id, account_id=account_id,
                strategy_id=strategy_id, market=market, venue=venue,
                size=signed, avg=float(price), fee=fee, funding=funding,
                ts=ts,
            )
            return 0.0, new_share

        prev_size = share.size_share_base
        new_size = prev_size + signed
        same_dir = (prev_size > 0 and signed > 0) or (prev_size < 0 and signed < 0)

        if same_dir:
            # Adding to the share — weighted-avg entry, no realized PnL.
            new_avg = (
                (share.avg_entry_share_price * abs(prev_size) + price * abs(signed))
                / abs(new_size)
            )
            updated = self._update_share(
                share_id=share.share_id,
                size=new_size, avg=new_avg,
                realized_delta=0.0, fee_delta=fee, funding_delta=funding, ts=ts,
            )
            return 0.0, updated

        # Opposite direction. Either reducing, fully closing, or flipping.
        closing_size = min(abs(prev_size), abs(signed))
        sign = 1.0 if prev_size > 0 else -1.0
        realized_delta = (price - share.avg_entry_share_price) * sign * closing_size

        if abs(new_size) < 1e-12:
            # Flat close on this share. Merged may still be open via
            # other strategies — that's fine, we just close *this* share.
            self._close_share(
                share_id=share.share_id, ts=ts,
                realized_delta=realized_delta, fee_delta=fee, funding_delta=funding,
            )
            closed = self._fetch_share_by_id(share.share_id)
            assert closed is not None
            return realized_delta, closed

        if (new_size > 0) != (prev_size > 0):
            # Reduce + flip on this share: close the existing slice,
            # open a fresh one in the opposite direction at the fill
            # price. Realized PnL hits only the closed portion;
            # the new share has avg=price and no carry-over fees.
            self._close_share(
                share_id=share.share_id, ts=ts,
                realized_delta=realized_delta, fee_delta=fee, funding_delta=funding,
            )
            new_share = self._open_share(
                position_id=merged.position_id, account_id=account_id,
                strategy_id=strategy_id, market=market, venue=venue,
                size=new_size, avg=float(price), fee=0.0, funding=0.0, ts=ts,
            )
            return realized_delta, new_share

        # Same-side reduction: keep avg, size shrinks, realized accrues.
        updated = self._update_share(
            share_id=share.share_id,
            size=new_size, avg=share.avg_entry_share_price,
            realized_delta=realized_delta, fee_delta=fee, funding_delta=funding, ts=ts,
        )
        return realized_delta, updated

    def _refresh_merged_from_shares(
        self,
        *,
        position_id: str,
        ts: float,
        mark_price: float,
        fee_delta: float,
        funding_delta: float,
        realized_delta: float,
        leverage: float,
        source: PositionSource,
    ) -> Position:
        """Recompute the merged row from currently-open shares.

        Fees/funding/realized are *aggregated additively*: we
        accumulate the delta from the latest fill onto the merged
        totals. Re-aggregating realized from all (including closed)
        shares would also work but is O(N) on every fill; the
        incremental version stays O(1) and is consistent because every
        share-level realized change is mirrored here exactly once.
        """
        con = self._con_lazy()
        open_shares = self.list_shares(position_id, open_only=True)
        if not open_shares:
            # All shares are closed → close the merged position too.
            con.execute(
                """
                UPDATE positions
                   SET size_base = 0,
                       realized_pnl_usd = realized_pnl_usd + ?,
                       fees_usd = fees_usd + ?,
                       funding_usd = funding_usd + ?,
                       mark_price = ?,
                       updated_at = ?,
                       closed_at = ?
                 WHERE position_id = ?
                """,
                (
                    realized_delta, fee_delta, funding_delta,
                    mark_price, ts, ts, position_id,
                ),
            )
            refreshed = self.get_by_id(position_id)
            assert refreshed is not None
            return refreshed

        size_total = 0.0
        cost_basis = 0.0
        for s in open_shares:
            size_total += s.size_share_base
            cost_basis += s.size_share_base * s.avg_entry_share_price

        merged_avg = cost_basis / size_total if abs(size_total) > 1e-12 else 0.0
        side = "long" if size_total >= 0 else "short"
        side_factor = 1.0 if size_total >= 0 else -1.0
        unrealized = (
            (float(mark_price) - merged_avg) * abs(size_total) * side_factor
        ) if merged_avg else 0.0

        con.execute(
            """
            UPDATE positions
               SET size_base = ?,
                   avg_entry_price = ?,
                   realized_pnl_usd = realized_pnl_usd + ?,
                   unrealized_pnl_usd = ?,
                   fees_usd = fees_usd + ?,
                   funding_usd = funding_usd + ?,
                   mark_price = ?,
                   side = ?,
                   leverage = MAX(leverage, ?),
                   source = COALESCE(?, source),
                   updated_at = ?
             WHERE position_id = ?
            """,
            (
                size_total, merged_avg,
                realized_delta, unrealized,
                fee_delta, funding_delta,
                mark_price, side,
                float(leverage or 1.0), source, ts,
                position_id,
            ),
        )
        refreshed = self.get_by_id(position_id)
        assert refreshed is not None
        return refreshed

    # -- internal: low-level row writers ---------------------------------------
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

    def _open_share(
        self,
        *,
        position_id: str,
        account_id: str,
        strategy_id: str,
        market: str,
        venue: str,
        size: float,
        avg: float,
        fee: float,
        funding: float,
        ts: float,
    ) -> PositionShare:
        share = PositionShare(
            share_id=f"shr_{_new_position_id()[-12:]}_{strategy_id[:16]}",
            position_id=position_id,
            account_id=account_id,
            strategy_id=strategy_id,
            market=market,
            venue=venue,
            size_share_base=size,
            avg_entry_share_price=avg,
            realized_pnl_share_usd=0.0,
            fees_share_usd=fee,
            funding_share_usd=funding,
            opened_at=ts,
            updated_at=ts,
        )
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO position_shares (
                share_id, position_id, account_id, strategy_id, market, venue,
                size_share_base, avg_entry_share_price,
                realized_pnl_share_usd, fees_share_usd, funding_share_usd,
                opened_at, updated_at, closed_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}')
            """,
            (
                share.share_id, share.position_id, share.account_id,
                share.strategy_id, share.market, share.venue,
                share.size_share_base, share.avg_entry_share_price,
                share.realized_pnl_share_usd, share.fees_share_usd,
                share.funding_share_usd, share.opened_at, share.updated_at,
            ),
        )
        return share

    def _update_share(
        self,
        *,
        share_id: str,
        size: float,
        avg: float,
        realized_delta: float,
        fee_delta: float,
        funding_delta: float,
        ts: float,
    ) -> PositionShare:
        con = self._con_lazy()
        con.execute(
            """
            UPDATE position_shares
               SET size_share_base = ?,
                   avg_entry_share_price = ?,
                   realized_pnl_share_usd = realized_pnl_share_usd + ?,
                   fees_share_usd = fees_share_usd + ?,
                   funding_share_usd = funding_share_usd + ?,
                   updated_at = ?
             WHERE share_id = ?
            """,
            (
                size, avg, realized_delta, fee_delta, funding_delta,
                ts, share_id,
            ),
        )
        refreshed = self._fetch_share_by_id(share_id)
        assert refreshed is not None
        return refreshed

    def _close_share(
        self,
        *,
        share_id: str,
        ts: float,
        realized_delta: float,
        fee_delta: float,
        funding_delta: float,
    ) -> None:
        con = self._con_lazy()
        con.execute(
            """
            UPDATE position_shares
               SET size_share_base = 0,
                   realized_pnl_share_usd = realized_pnl_share_usd + ?,
                   fees_share_usd = fees_share_usd + ?,
                   funding_share_usd = funding_share_usd + ?,
                   updated_at = ?,
                   closed_at = ?
             WHERE share_id = ?
            """,
            (realized_delta, fee_delta, funding_delta, ts, ts, share_id),
        )

    def _fetch_share_by_id(self, share_id: str) -> PositionShare | None:
        row = self._con_lazy().execute(
            "SELECT * FROM position_shares WHERE share_id = ?", (share_id,),
        ).fetchone()
        return _row_to_share(row) if row else None

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


def _row_to_share(row: Any) -> PositionShare:
    return PositionShare(
        share_id=str(row["share_id"]),
        position_id=str(row["position_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        market=str(row["market"]),
        venue=str(row["venue"] or ""),
        size_share_base=float(row["size_share_base"] or 0.0),
        avg_entry_share_price=float(row["avg_entry_share_price"] or 0.0),
        realized_pnl_share_usd=float(row["realized_pnl_share_usd"] or 0.0),
        fees_share_usd=float(row["fees_share_usd"] or 0.0),
        funding_share_usd=float(row["funding_share_usd"] or 0.0),
        opened_at=float(row["opened_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
        closed_at=(float(row["closed_at"]) if row["closed_at"] is not None else None),
        meta=json.loads(str(row["meta_json"] or "{}")),
    )


def _kind_for_fill(merged: Position, signed: float) -> str:
    """Classify the event kind based on the merged position state.

    ``opened`` is reserved for the very first share — handled in the
    new-position branch of ``apply_fill``. Here the merged position is
    already open; we classify by direction of the fill relative to the
    merged size, since that's what the audit log cares about.
    """
    if not merged.is_open:
        return "closed"
    if signed > 0 and merged.size_base > 0:
        return "increased"
    if signed < 0 and merged.size_base < 0:
        return "increased"
    return "reduced"


def _infer_venue(market: str) -> str:
    if ":" in market:
        return market.split(":", 1)[0].upper()
    return ""


__all__ = [
    "MERGED_STRATEGY_SENTINEL",
    "Position",
    "PositionBook",
    "PositionShare",
    "PositionSide",
    "PositionSource",
]
