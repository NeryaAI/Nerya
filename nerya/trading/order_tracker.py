"""Durable order tracker.

the connector framework's ``ClientOrderTracker`` is the model: every order has a
client-side id we generate before we hit the exchange, every
state/fill/cancel transition is recorded, and the tracker persists
enough state in SQLite to recover after a runtime crash.

lays out four primary states a tracker must be able to
recognise without help from a fragile ``OrderAck`` round-trip:

* ``active`` — submitted and not yet terminal.
* ``cached`` — terminal (``filled``/``canceled``/etc) but kept around
  for a configurable retention window so out-of-order fill updates
  from the exchange still match.
* ``lost`` — repeated ``fetch_order`` not-founds, but local terminal
  state never observed. Operators decide whether to recover or treat
  the order as gone.
* ``terminated`` — terminal *and* outside the retention window.

We don't persist that bucket in a separate column — it's a derived
view computed from ``state``/``terminal_at``/``not_found_streak``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.ids import event_id as _new_event_id, fill_id as _new_fill_id, order_id as _new_order_id
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect

log = logging.getLogger(__name__)


OrderState = Literal[
    "created",
    "reserved",
    "submitted",
    "accepted",
    "open",
    "partially_filled",
    "filled",
    "cancel_requested",
    "canceled",
    "rejected",
    "expired",
    "lost",
    "failed",
]

TERMINAL_STATES: tuple[OrderState, ...] = (
    "filled", "canceled", "rejected", "expired", "failed",
)


# How long terminal orders sit in the cached bucket before being
# treated as fully retired.
DEFAULT_CACHED_RETENTION_S = 24 * 3600
# How many consecutive ``fetch_order`` not-founds count as a "lost"
# order. the connector framework's default is 4 — same here.
LOST_ORDER_NOT_FOUND_THRESHOLD = 4


def make_client_order_id(
    *, strategy_id: str, executor_id: str, leg: str = "0", seq: int = 0
) -> str:
    """stable, traceable client order id.

    ``nerya:{strategy_id}:{executor_id}:{leg}:{seq}`` is a 64-char
    upper bound which fits Binance's 36-char clientOrderId limit *only*
    for short ids — callers should keep ``strategy_id`` and
    ``executor_id`` ≤14 chars each. Longer ids fall back to a hash
    suffix (TODO).
    """
    safe_strategy = strategy_id.replace(":", "_")[:14]
    safe_executor = executor_id.replace(":", "_")[:14]
    return f"nerya:{safe_strategy}:{safe_executor}:{leg}:{seq}"


# ---------------------------------------------------------------------------
# Order record
# ---------------------------------------------------------------------------


@dataclass
class TrackedOrder:
    order_id: str
    client_order_id: str
    account_id: str
    strategy_id: str
    market: str
    side: Literal["buy", "sell"]
    order_type: str
    state: OrderState
    size_base: float | None = None
    notional_usd: float | None = None
    price: float | None = None
    stop_price: float | None = None
    leverage: float = 1.0
    reduce_only: bool = False
    time_in_force: str = "gtc"
    filled_size: float = 0.0
    avg_price: float | None = None
    fee_usd: float = 0.0
    intent_id: str | None = None
    plan_id: str | None = None
    reservation_id: str | None = None
    executor_id: str | None = None
    exchange_order_id: str | None = None
    created_at: float = field(default_factory=time.time)
    submitted_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    terminal_at: float | None = None
    last_seen_at: float | None = None
    not_found_streak: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderEvent:
    event_id: str
    order_id: str
    kind: str
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrackedFill:
    fill_id: str
    order_id: str
    client_order_id: str
    account_id: str
    strategy_id: str
    executor_id: str | None
    market: str
    side: Literal["buy", "sell"]
    price: float
    size_base: float
    notional_usd: float
    fee_usd: float
    funding_usd: float
    source: str
    ts: float
    intent_id: str | None
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class OrderTracker:
    """Durable active/cached/lost order tracker.

    Construct with :class:`WorkspacePaths`; the tracker owns its own
    SQLite connection (lazy). Tests should pass an in-memory path via
    :class:`WorkspacePaths` so each test gets a fresh DB.
    """

    def __init__(
        self,
        paths: WorkspacePaths,
        *,
        cached_retention_s: float = DEFAULT_CACHED_RETENTION_S,
        lost_threshold: int = LOST_ORDER_NOT_FOUND_THRESHOLD,
    ):
        self.paths = paths
        self.cached_retention_s = float(cached_retention_s)
        self.lost_threshold = int(lost_threshold)
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    # -- create -----------------------------------------------------------------
    def register(
        self,
        *,
        client_order_id: str,
        account_id: str,
        strategy_id: str,
        market: str,
        side: Literal["buy", "sell"],
        order_type: str,
        size_base: float | None = None,
        notional_usd: float | None = None,
        price: float | None = None,
        stop_price: float | None = None,
        leverage: float = 1.0,
        reduce_only: bool = False,
        time_in_force: str = "gtc",
        intent_id: str | None = None,
        plan_id: str | None = None,
        reservation_id: str | None = None,
        executor_id: str | None = None,
        meta: dict[str, Any] | None = None,
        order_id: str | None = None,
        initial_state: OrderState = "created",
    ) -> TrackedOrder:
        order = TrackedOrder(
            order_id=order_id or _new_order_id(),
            client_order_id=client_order_id,
            account_id=account_id,
            strategy_id=strategy_id,
            market=market,
            side=side,
            order_type=order_type,
            state=initial_state,
            size_base=size_base,
            notional_usd=notional_usd,
            price=price,
            stop_price=stop_price,
            leverage=leverage,
            reduce_only=reduce_only,
            time_in_force=time_in_force,
            intent_id=intent_id,
            plan_id=plan_id,
            reservation_id=reservation_id,
            executor_id=executor_id,
            meta=dict(meta or {}),
        )
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO orders (
                order_id, client_order_id, exchange_order_id,
                account_id, strategy_id, executor_id,
                market, side, order_type,
                size_base, notional_usd, price, stop_price,
                leverage, reduce_only, time_in_force,
                state, filled_size, avg_price, fee_usd,
                intent_id, plan_id, reservation_id,
                created_at, submitted_at, updated_at, terminal_at,
                last_seen_at, not_found_streak, meta_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                order.order_id, order.client_order_id, order.exchange_order_id,
                order.account_id, order.strategy_id, order.executor_id,
                order.market, order.side, order.order_type,
                order.size_base, order.notional_usd, order.price, order.stop_price,
                order.leverage, int(order.reduce_only), order.time_in_force,
                order.state, order.filled_size, order.avg_price, order.fee_usd,
                order.intent_id, order.plan_id, order.reservation_id,
                order.created_at, order.submitted_at, order.updated_at, order.terminal_at,
                order.last_seen_at, order.not_found_streak, json.dumps(order.meta),
            ),
        )
        self._record_event(order.order_id, "registered", {
            "state": order.state,
            "client_order_id": order.client_order_id,
        })
        return order

    # -- transitions ------------------------------------------------------------
    def mark_submitted(
        self,
        order_id: str,
        *,
        exchange_order_id: str | None = None,
        ts: float | None = None,
    ) -> None:
        ts = ts if ts is not None else time.time()
        con = self._con_lazy()
        con.execute(
            """
            UPDATE orders
               SET state = 'submitted',
                   submitted_at = COALESCE(submitted_at, ?),
                   updated_at = ?,
                   exchange_order_id = COALESCE(?, exchange_order_id),
                   last_seen_at = ?
             WHERE order_id = ?
            """,
            (ts, ts, exchange_order_id, ts, order_id),
        )
        self._record_event(order_id, "submitted", {"exchange_order_id": exchange_order_id})

    def update_state(
        self,
        order_id: str,
        new_state: OrderState,
        *,
        ts: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ts = ts if ts is not None else time.time()
        terminal_at = ts if new_state in TERMINAL_STATES else None
        con = self._con_lazy()
        con.execute(
            """
            UPDATE orders
               SET state = ?,
                   updated_at = ?,
                   terminal_at = COALESCE(terminal_at, ?),
                   last_seen_at = ?
             WHERE order_id = ?
            """,
            (new_state, ts, terminal_at, ts, order_id),
        )
        self._record_event(order_id, f"state.{new_state}", payload or {})

    def request_cancel(self, order_id: str) -> None:
        self.update_state(order_id, "cancel_requested")

    def confirm_cancel(self, order_id: str) -> None:
        self.update_state(order_id, "canceled")

    def mark_rejected(self, order_id: str, reason: str = "") -> None:
        self.update_state(order_id, "rejected", payload={"reason": reason})

    # -- fills ------------------------------------------------------------------
    def record_fill(
        self,
        *,
        order_id: str,
        price: float,
        size_base: float,
        fee_usd: float = 0.0,
        funding_usd: float = 0.0,
        source: str = "paper",
        side: Literal["buy", "sell"] | None = None,
        ts: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> TrackedFill:
        ts = ts if ts is not None else time.time()
        order = self.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id: {order_id}")
        eff_side = side or order.side
        notional = float(price) * float(size_base)
        fill = TrackedFill(
            fill_id=_new_fill_id(),
            order_id=order_id,
            client_order_id=order.client_order_id,
            account_id=order.account_id,
            strategy_id=order.strategy_id,
            executor_id=order.executor_id,
            market=order.market,
            side=eff_side,
            price=float(price),
            size_base=float(size_base),
            notional_usd=notional,
            fee_usd=float(fee_usd),
            funding_usd=float(funding_usd),
            source=source,
            ts=ts,
            intent_id=order.intent_id,
            meta=dict(meta or {}),
        )
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO fills (
                fill_id, order_id, client_order_id, account_id, strategy_id,
                executor_id, market, side, price, size_base, notional_usd,
                fee_usd, funding_usd, source, ts, intent_id, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.fill_id, fill.order_id, fill.client_order_id, fill.account_id,
                fill.strategy_id, fill.executor_id, fill.market, fill.side,
                fill.price, fill.size_base, fill.notional_usd, fill.fee_usd,
                fill.funding_usd, fill.source, fill.ts, fill.intent_id,
                json.dumps(fill.meta),
            ),
        )

        # Roll up filled_size / avg_price / fee_usd onto the order row.
        new_filled = float(order.filled_size or 0.0) + float(size_base)
        prev_avg = float(order.avg_price or 0.0)
        new_avg = ((prev_avg * float(order.filled_size or 0.0)) + (price * size_base)) / new_filled if new_filled else 0.0
        new_fee = float(order.fee_usd or 0.0) + float(fee_usd)
        target_size = float(order.size_base or 0.0)
        new_state: OrderState
        if target_size > 0 and new_filled + 1e-12 >= target_size:
            new_state = "filled"
            terminal_at = ts
        else:
            new_state = "partially_filled"
            terminal_at = None
        con.execute(
            """
            UPDATE orders
               SET filled_size = ?,
                   avg_price = ?,
                   fee_usd = ?,
                   state = ?,
                   updated_at = ?,
                   terminal_at = COALESCE(terminal_at, ?),
                   last_seen_at = ?
             WHERE order_id = ?
            """,
            (new_filled, new_avg, new_fee, new_state, ts, terminal_at, ts, order_id),
        )
        self._record_event(order_id, "fill", {
            "fill_id": fill.fill_id,
            "price": price,
            "size_base": size_base,
            "fee_usd": fee_usd,
            "source": source,
        })
        self._notify_fill(order, fill, new_state)
        return fill

    # -- exchange feedback ------------------------------------------------------
    def mark_seen(self, order_id: str, *, ts: float | None = None) -> None:
        """Reset the not-found streak after a successful fetch."""
        con = self._con_lazy()
        con.execute(
            "UPDATE orders SET not_found_streak = 0, last_seen_at = ? WHERE order_id = ?",
            (ts if ts is not None else time.time(), order_id),
        )

    def mark_not_found(self, order_id: str, *, ts: float | None = None) -> bool:
        """Increment the not-found streak. Returns True if the order
        crossed the lost threshold and was marked ``lost``."""
        ts = ts if ts is not None else time.time()
        con = self._con_lazy()
        con.execute(
            "UPDATE orders SET not_found_streak = not_found_streak + 1, last_seen_at = ? WHERE order_id = ?",
            (ts, order_id),
        )
        order = self.get(order_id)
        if order is None or order.is_terminal:
            return False
        if order.not_found_streak >= self.lost_threshold:
            self.update_state(order_id, "lost", ts=ts, payload={
                "not_found_streak": order.not_found_streak,
            })
            return True
        return False

    # -- queries ----------------------------------------------------------------
    def get(self, order_id: str) -> TrackedOrder | None:
        row = self._con_lazy().execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,),
        ).fetchone()
        return _row_to_order(row) if row else None

    def get_by_client_order_id(self, client_order_id: str) -> TrackedOrder | None:
        row = self._con_lazy().execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,),
        ).fetchone()
        return _row_to_order(row) if row else None

    def active_orders(self, *, account_id: str | None = None) -> list[TrackedOrder]:
        sql = (
            "SELECT * FROM orders WHERE state NOT IN ('filled','canceled','rejected','expired','failed','lost')"
        )
        params: tuple[Any, ...] = ()
        if account_id:
            sql += " AND account_id = ?"
            params = (account_id,)
        rows = self._con_lazy().execute(sql, params).fetchall()
        return [_row_to_order(r) for r in rows]

    def lost_orders(self, *, account_id: str | None = None) -> list[TrackedOrder]:
        sql = "SELECT * FROM orders WHERE state = 'lost'"
        params: tuple[Any, ...] = ()
        if account_id:
            sql += " AND account_id = ?"
            params = (account_id,)
        rows = self._con_lazy().execute(sql, params).fetchall()
        return [_row_to_order(r) for r in rows]

    def cached_orders(
        self, *, account_id: str | None = None, now: float | None = None
    ) -> list[TrackedOrder]:
        """Terminal orders still inside the retention window."""
        cutoff = (now if now is not None else time.time()) - self.cached_retention_s
        sql = (
            "SELECT * FROM orders WHERE terminal_at IS NOT NULL AND terminal_at >= ?"
        )
        params: tuple[Any, ...] = (cutoff,)
        if account_id:
            sql += " AND account_id = ?"
            params = (cutoff, account_id)
        rows = self._con_lazy().execute(sql, params).fetchall()
        return [_row_to_order(r) for r in rows]

    def fills_for_order(self, order_id: str) -> list[TrackedFill]:
        rows = self._con_lazy().execute(
            "SELECT * FROM fills WHERE order_id = ? ORDER BY ts", (order_id,),
        ).fetchall()
        return [_row_to_fill(r) for r in rows]

    def events_for_order(self, order_id: str) -> list[OrderEvent]:
        rows = self._con_lazy().execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY ts", (order_id,),
        ).fetchall()
        return [
            OrderEvent(
                event_id=str(r["event_id"]),
                order_id=str(r["order_id"]),
                kind=str(r["kind"]),
                ts=float(r["ts"]),
                payload=json.loads(str(r["payload_json"] or "{}")),
            )
            for r in rows
        ]

    # -- internal ---------------------------------------------------------------
    def _record_event(self, order_id: str, kind: str, payload: dict[str, Any]) -> None:
        con = self._con_lazy()
        con.execute(
            "INSERT INTO order_events(event_id, order_id, kind, ts, payload_json) VALUES (?, ?, ?, ?, ?)",
            (_new_event_id(), order_id, kind, time.time(), json.dumps(payload)),
        )

    def _notify_fill(self, order: TrackedOrder, fill: TrackedFill, state: OrderState) -> None:
        if bool(fill.meta.get("suppress_trade_notification")):
            return
        try:
            from ..core.config import load_config
            from ..core.time import now_iso
            from ..messaging.trade_notifications import broadcast_trade_event

            config = load_config(self.paths.root)
            broadcast_trade_event(
                config,
                {
                    "kind": "trade.execution",
                    "status": "filled" if state == "filled" else "partial",
                    "strategy_id": fill.strategy_id,
                    "account_id": fill.account_id,
                    "market": fill.market,
                    "side": fill.side,
                    "source": fill.source,
                    "intent_id": fill.intent_id,
                    "plan_id": order.plan_id,
                    "executor_id": fill.executor_id,
                    "order_id": fill.order_id,
                    "session_id": fill.meta.get("session_id") or order.meta.get("session_id"),
                    "avg_price": fill.price,
                    "filled_size": fill.size_base,
                    "notional_usd": fill.notional_usd,
                    "fee_usd": fill.fee_usd,
                    "fill_id": fill.fill_id,
                    "ts": now_iso(),
                },
            )
        except Exception:  # pragma: no cover - notifications are best effort
            log.exception("trade fill notification failed for order %s", fill.order_id)


# ---------------------------------------------------------------------------
# Row -> dataclass helpers
# ---------------------------------------------------------------------------


def _row_to_order(row: Any) -> TrackedOrder:
    return TrackedOrder(
        order_id=str(row["order_id"]),
        client_order_id=str(row["client_order_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        market=str(row["market"]),
        side=str(row["side"]),  # type: ignore[arg-type]
        order_type=str(row["order_type"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        size_base=(float(row["size_base"]) if row["size_base"] is not None else None),
        notional_usd=(float(row["notional_usd"]) if row["notional_usd"] is not None else None),
        price=(float(row["price"]) if row["price"] is not None else None),
        stop_price=(float(row["stop_price"]) if row["stop_price"] is not None else None),
        leverage=float(row["leverage"] or 1.0),
        reduce_only=bool(row["reduce_only"] or 0),
        time_in_force=str(row["time_in_force"] or "gtc"),
        filled_size=float(row["filled_size"] or 0.0),
        avg_price=(float(row["avg_price"]) if row["avg_price"] is not None else None),
        fee_usd=float(row["fee_usd"] or 0.0),
        intent_id=(row["intent_id"] or None),
        plan_id=(row["plan_id"] or None),
        reservation_id=(row["reservation_id"] or None),
        executor_id=(row["executor_id"] or None),
        exchange_order_id=(row["exchange_order_id"] or None),
        created_at=float(row["created_at"] or 0.0),
        submitted_at=(float(row["submitted_at"]) if row["submitted_at"] is not None else None),
        updated_at=float(row["updated_at"] or 0.0),
        terminal_at=(float(row["terminal_at"]) if row["terminal_at"] is not None else None),
        last_seen_at=(float(row["last_seen_at"]) if row["last_seen_at"] is not None else None),
        not_found_streak=int(row["not_found_streak"] or 0),
        meta=json.loads(str(row["meta_json"] or "{}")),
    )


def _row_to_fill(row: Any) -> TrackedFill:
    return TrackedFill(
        fill_id=str(row["fill_id"]),
        order_id=str(row["order_id"]),
        client_order_id=str(row["client_order_id"] or ""),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        executor_id=(row["executor_id"] or None),
        market=str(row["market"]),
        side=str(row["side"]),  # type: ignore[arg-type]
        price=float(row["price"] or 0.0),
        size_base=float(row["size_base"] or 0.0),
        notional_usd=float(row["notional_usd"] or 0.0),
        fee_usd=float(row["fee_usd"] or 0.0),
        funding_usd=float(row["funding_usd"] or 0.0),
        source=str(row["source"] or "paper"),
        ts=float(row["ts"] or 0.0),
        intent_id=(row["intent_id"] or None),
        meta=json.loads(str(row["meta_json"] or "{}")),
    )


__all__ = [
    "OrderTracker",
    "OrderEvent",
    "OrderState",
    "TERMINAL_STATES",
    "TrackedOrder",
    "TrackedFill",
    "make_client_order_id",
    "DEFAULT_CACHED_RETENTION_S",
    "LOST_ORDER_NOT_FOUND_THRESHOLD",
]
