"""VNpy adapter — run a ``CtaTemplate`` strategy from Nerya ticks.

Mapping
-------
VNpy strategies are *event driven*: the CTA engine feeds ``on_bar`` /
``on_tick`` and the strategy calls ``buy/sell/short/cover`` which the
engine routes to the exchange. On Nerya the adapter plays the engine:

1. ``ctx.market.candles(market, timeframe=...)`` returns the candle
   history up to now (in backtest replay, up to the current bar).
2. New bars since the previous tick are converted to
   :class:`~...shims.vnpy_shim.BarData` and fed to ``strategy.on_bar``
   in order, each preceded by a synthesised ``on_tick`` (last_price =
   bar close — Nerya has no intra-bar tick stream). Strategies that
   synthesise higher timeframes internally
   do so with their own ``BarGenerator``, exactly as under VNpy — set
   the package's ``bar_timeframe`` to the strategy's base interval
   (usually ``1m``).
3. ``buy/sell/short/cover`` calls hit the adapter's engine shim, which
   converts each order into a risk-gated Nerya intent *immediately*
   (``buy``→buy/open-long, ``short``→sell/open-short, ``sell``→
   sell/close-long, ``cover``→buy/close-short). Only **filled**
   envelopes synthesise a fill (paper semantics), update
   ``strategy.pos`` and fire ``on_order`` / ``on_trade``; rejected
   intents and ``pending_approval`` envelopes (the normal case when
   the Approval Gate holds an order) never touch ``strategy.pos``.
   Stop orders (``stop=True``) are the exception: like the real VNpy
   CTA engine they are parked locally and only submitted when a bar's
   high/low crosses the stop price. Untriggered stops are dropped at
   the end of a replay.
4. ``on_init`` runs once on the first tick; a ``load_bar(days)`` call
   during init replays the fetched history (excluding the newest bar,
   which is left for the first trading tick) through the strategy
   before live bars arrive, mirroring the CTA engine's warm-up.

Position (``strategy.pos``) is mirrored into ``ctx.state`` each tick
for the dashboard; per-bar processing state lives under
``_compat_vnpy`` so ticks are idempotent (a bar is processed once)
across live runs and backtest replay. Parked stop orders and
``pending_approval`` markers are persisted in the same state key so a
restart does not resurrect or double-fire them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .shims.vnpy_shim import (
    BarData,
    Direction,
    EngineType,
    Exchange,
    Interval,
    Offset,
    OrderData,
    Status,
    StopOrder,
    TickData,
    TradeData,
)

_LOG = logging.getLogger(__name__)

_STATE_KEY = "_compat_vnpy"

_TICK_MS = 60 * 1000
_HOUR_MS = 3600 * 1000

_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


class VnpyAdapterError(Exception):
    """Raised when the foreign strategy cannot be executed as authored."""


class VnpyOrderBridge:
    """The ``cta_engine`` shim handed to ``CtaTemplate`` instances.

    Collects orders/logs for the current tick and translates every
    ``send_order`` into a Nerya intent through ``ctx.trading``.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        market: str,
        settings: dict[str, Any],
        pending_stops: "list[dict[str, Any]] | None" = None,
        seq: int = 0,
    ) -> None:
        self._ctx = ctx
        self.market = market
        self.settings = settings
        self.orders: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.load_bar_request: Optional[dict[str, Any]] = None
        # Parked stop orders (real VNpy holds these locally too). The
        # list is seeded from package state and written back by
        # run_vnpy_tick so stops survive restarts.
        self.pending_stops: list[dict[str, Any]] = list(pending_stops or [])
        # Set when an intent came back ``pending_approval`` — the
        # Approval Gate holds the order, so nothing may be written to
        # ``strategy.pos``. run_vnpy_tick resolves the marker on the
        # next tick.
        self.pending_intent: Optional[dict[str, Any]] = None
        # Order-id sequence, seeded from the persisted counter in the
        # same state blob as ``pending_stops``. The bridge is recreated
        # per tick, so a per-instance counter would restart at 1 every
        # tick and two stops parked in different ticks would share one
        # ``STOP.1`` id — cancel_order would then remove the wrong one.
        self._seq = max(int(seq or 0), 0)

    # -- order flow ------------------------------------------------------

    def send_order(  # noqa: D401 — engine API naming
        self,
        strategy: Any,
        direction: Any,
        offset: Any,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[str]:
        direction = _normalise_direction(direction)
        offset = _normalise_offset(offset)
        try:
            volume = float(volume or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume <= 0:
            return []

        if stop:
            return self._park_stop_order(strategy, direction, offset, float(price or 0.0), volume)

        vt_orderid = self._submit_intent(strategy, direction, offset, float(price or 0.0), volume)
        return [vt_orderid] if vt_orderid else []

    def _park_stop_order(
        self,
        strategy: Any,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
    ) -> list[str]:
        """Park a ``stop=True`` order locally, like the real CTA engine.

        VNpy stop orders never reach the exchange as-is: the engine
        holds them and converts to a market/limit order when the last
        price crosses the stop. We mirror that — the order is parked
        (persisted in package state) and re-evaluated against every
        new bar's high/low by :func:`run_vnpy_tick`.
        """

        self._seq += 1
        vt_orderid = f"STOP.{self._seq}"
        self.pending_stops.append(
            {
                "vt_orderid": vt_orderid,
                "direction": direction.value,
                "offset": offset.value,
                "price": float(price),
                "volume": float(volume),
            }
        )
        self.orders.append(
            {
                "vt_orderid": vt_orderid,
                "direction": direction.value,
                "offset": offset.value,
                "price": float(price),
                "volume": float(volume),
                "stop": True,
                "status": "stop_pending",
                "envelope": None,
            }
        )
        self.write_log(f"停损单已挂起 {vt_orderid} {direction.value}/{offset.value} {volume} @ {price}")
        return [vt_orderid]

    def _stop_triggered(self, rec: dict[str, Any], high: float, low: float) -> bool:
        direction = _normalise_direction(rec.get("direction"))
        stop_price = float(rec.get("price") or 0.0)
        if stop_price <= 0:
            return False
        if direction == Direction.LONG:
            return high >= stop_price  # buy stop fires upwards
        return low <= stop_price  # sell/short stop fires downwards

    def evaluate_stops(self, strategy: Any, bar: BarData) -> None:
        """Activate every parked stop whose trigger the bar crossed."""

        high = float(bar.high_price or 0.0)
        low = float(bar.low_price or 0.0)
        for rec in list(self.pending_stops):
            if self._stop_triggered(rec, high, low):
                self.activate_stop(strategy, rec)

    def activate_stop(self, strategy: Any, rec: dict[str, Any]) -> None:
        """Submit a parked stop whose trigger price was crossed."""
        if rec not in self.pending_stops:
            return
        self.pending_stops.remove(rec)
        direction = _normalise_direction(rec.get("direction"))
        offset = _normalise_offset(rec.get("offset"))
        price = float(rec.get("price") or 0.0)
        volume = float(rec.get("volume") or 0.0)
        vt_orderid = str(rec.get("vt_orderid") or "")
        self.write_log(f"停损单触发 {vt_orderid} @ {price}")
        stop_order = StopOrder(
            vt_orderid=vt_orderid,
            symbol=strategy.symbol,
            exchange=strategy.exchange,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            stop_price=price,
            strategy_name=str(getattr(strategy, "strategy_name", "") or ""),
        )
        on_stop = getattr(strategy, "on_stop_order", None)
        if callable(on_stop):
            on_stop(stop_order)
        status, envelope = self._submit_intent_envelope(
            strategy, direction, offset, price, volume, vt_orderid=vt_orderid
        )
        self.orders.append(
            {
                "vt_orderid": vt_orderid,
                "direction": direction.value,
                "offset": offset.value,
                "price": price,
                "volume": volume,
                "stop": True,
                "status": status,
                "envelope": envelope,
            }
        )

    def _submit_intent(
        self,
        strategy: Any,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
    ) -> str:
        self._seq += 1
        vt_orderid = f"NERYA.{self.market}.{self._seq}"
        status, _envelope = self._submit_intent_envelope(
            strategy, direction, offset, price, volume, vt_orderid=vt_orderid
        )
        self.orders.append(
            {
                "vt_orderid": vt_orderid,
                "direction": direction.value,
                "offset": offset.value,
                "price": float(price or 0.0),
                "volume": volume,
                "stop": False,
                "status": status,
                "envelope": _envelope,
            }
        )
        return vt_orderid if status != "rejected" else ""

    def _submit_intent_envelope(
        self,
        strategy: Any,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        *,
        vt_orderid: str,
    ) -> "tuple[str, dict[str, Any]]":
        """Send one risk-gated Nerya intent for a (direction, offset).

        Returns ``(status, envelope)``. Only a ``filled`` envelope
        synthesises a fill and updates ``strategy.pos`` — rejected,
        ``pending_approval``, ``shadow`` and ``failed`` envelopes must
        never create a phantom position.
        """

        side, plan_action = _map_order(direction, offset)
        if side is None:
            self.write_log(f"忽略订单: {direction}/{offset} {volume}")
            return "rejected", {}

        size_unit = str(self.settings.get("size_unit") or "base")

        envelope: dict[str, Any] = {}
        try:
            envelope = dict(
                self._ctx.trading.submit_intent(
                    market=self.market,
                    side=side,
                    size=volume,
                    size_unit=size_unit,
                    order_type="limit" if price else "market",
                    limit_price=float(price) if price else None,
                    reasoning=f"vnpy order {direction.value}/{offset.value} vol={volume}",
                    metadata={
                        "compat": "vnpy",
                        "vnpy_direction": direction.value,
                        "vnpy_offset": offset.value,
                        "vnpy_vt_orderid": vt_orderid,
                    },
                    plan_action=plan_action,
                )
                or {}
            )
        except Exception as exc:
            _LOG.warning("vnpy adapter intent failed: %s", exc)
            envelope = {"status": "rejected", "reason": str(exc)}

        status = str(envelope.get("status") or "rejected")
        if status == "rejected":
            return status, envelope
        if status == "filled":
            # Adopt the executor's real fill when the envelope carries
            # the order summary (partial fills / fill-price drift make
            # the requested volume wrong); fall back to the requested
            # size/price otherwise.
            order = envelope.get("order") or {}
            filled_size = _positive_float(order.get("filled_size"))
            avg_price = _positive_float(order.get("avg_price"))
            self._synthesize_fill(
                strategy,
                direction,
                offset,
                avg_price if avg_price is not None else float(price or 0.0),
                filled_size if filled_size is not None else volume,
                vt_orderid,
            )
        elif status == "pending_approval":
            # Approval Gate holds the order (the normal case on
            # canary/live). Record a marker; the position is untouched
            # and run_vnpy_tick resolves the marker on the next tick.
            self.pending_intent = {
                "vt_orderid": vt_orderid,
                "direction": direction.value,
                "offset": offset.value,
                "price": float(price or 0.0),
                "volume": volume,
                "approval_id": envelope.get("approval_id"),
            }
            self.write_log(f"订单待审批 {vt_orderid} — pos 未更新")
        return status, envelope

    def _synthesize_fill(
        self,
        strategy: Any,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        vt_orderid: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        order = OrderData(
            symbol=strategy.symbol,
            exchange=strategy.exchange,
            orderid=vt_orderid,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            traded=volume,
            status=Status.ALLTRADED,
            datetime=now,
        )
        strategy.on_order(order)

        trade = TradeData(
            symbol=strategy.symbol,
            exchange=strategy.exchange,
            datetime=now,
            orderid=vt_orderid,
            tradeid=vt_orderid,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
        )
        delta = volume if direction == Direction.LONG else -volume
        # Crypto volumes are fractional — keep ``pos`` a float. Truncating
        # to int made ``self.pos == 0`` gates re-enter every bar and never
        # exit (0.5 BTC became 0, then 1).
        strategy.pos = float(getattr(strategy, "pos", 0.0) or 0.0) + float(delta)
        strategy.on_trade(trade)

    def cancel_order(self, strategy: Any, vt_orderid: str) -> None:
        # Regular orders fill immediately on the Nerya paper bridge —
        # only parked stop orders can still be cancelled.
        for rec in list(self.pending_stops):
            if str(rec.get("vt_orderid")) == str(vt_orderid):
                self.pending_stops.remove(rec)
                self.write_log(f"停损单已撤销 {vt_orderid}")
                return
        self.write_log(f"cancel_order({vt_orderid}) — fills are immediate on Nerya paper bridge")

    def cancel_all(self, strategy: Any) -> None:
        if self.pending_stops:
            self.write_log(f"撤销全部停损单 ({len(self.pending_stops)})")
            self.pending_stops.clear()

    # -- lifecycle support -------------------------------------------------

    def write_log(self, msg: str, strategy: Any = None) -> None:
        self.logs.append(str(msg))
        if len(self.logs) > 200:
            del self.logs[:-200]

    def get_engine_type(self) -> EngineType:
        try:
            mode = str(getattr(self._ctx.config, "mode", "") or "paper")
        except Exception:
            mode = "paper"
        return EngineType.LIVE if mode == "live" else EngineType.BACKTESTING

    def load_bar(
        self,
        strategy: Any,
        days: int,
        hour: int = 0,
        minute: int = 0,
        interval: Interval = Interval.MINUTE,
        callback: Any = None,
    ) -> None:
        self.load_bar_request = {
            "days": int(days or 0),
            "hour": int(hour or 0),
            "minute": int(minute or 0),
            "interval": interval,
            "callback": callback,
        }

    def sync_data(self, strategy: Any) -> None:
        self.write_log(f"pos={getattr(strategy, 'pos', 0)}")


def run_vnpy_tick(
    ctx: Any,
    strategy: Any,
    *,
    market: str,
    settings: dict[str, Any],
) -> Any:
    """Run one tick of a VNpy ``CtaTemplate``; returns a StrategyResult."""

    from ..result import StrategyResult

    bar_timeframe = str(settings.get("bar_timeframe") or settings.get("interval") or "1m")
    history_limit = min(int(settings.get("init_bars") or 500), 990)

    try:
        rows = list(ctx.market.candles(market, timeframe=bar_timeframe, limit=history_limit) or [])
    except Exception as exc:
        return ctx.result.error(message=f"vnpy_compat_candles_failed: {exc}", kind="compat_error")
    if not rows:
        return ctx.result.hold(reason="vnpy_compat_no_candles")

    state = dict(ctx.state.get(_STATE_KEY, {}) or {})
    # Resolve a ``pending_approval`` marker left by the previous tick
    # before evaluating new signals. The strategy context exposes no
    # execution-status polling API, so the marker is dropped with a
    # warning journal entry; the position was never written, and the
    # strategy's own logic re-submits if the setup still holds.
    if state.get("pending_intent"):
        _drop_pending_marker(ctx, "vnpy", state.pop("pending_intent"))
        ctx.state.set(_STATE_KEY, state)
    last_ts = int(state.get("last_ts") or 0)
    latest_ts = _row_ts(rows[-1])
    first_run = not bool(state.get("inited"))

    engine = VnpyOrderBridge(
        ctx,
        market=market,
        settings=settings,
        pending_stops=state.get("pending_stops") or [],
        seq=int(state.get("seq") or 0),
    )
    strategy.cta_engine = engine

    if not first_run:
        # Resume after a process restart. A fresh CtaTemplate (and
        # entrypoint._instantiate) constructs with ``trading=False``,
        # so without re-arming here every buy/sell would silently
        # return [] forever after a restart. Restore the persisted
        # mirrors too — ``pos`` especially: a restarted strategy that
        # still holds a position would otherwise see pos=0 and
        # re-enter on top of it (duplicate exposure).
        strategy.inited = True
        strategy.trading = True
        try:
            strategy.pos = float(state.get("pos") or 0.0)
        except (TypeError, ValueError):
            strategy.pos = 0.0

    if first_run:
        # on_init may call load_bar(days) — the bridge records the
        # request and we replay history through it below, while
        # ``trading`` is still False (orders are ignored during init,
        # matching the real CTA engine's warm-up semantics). The newest
        # bar is excluded from warm-up so it is processed once with
        # ``trading`` on, instead of being silently consumed.
        strategy.inited = False
        strategy.trading = False
        try:
            strategy.on_init()
        except Exception as exc:
            return ctx.result.error(message=f"vnpy_compat_on_init_failed: {exc}", kind="compat_error")

        warmup_rows: list[dict[str, Any]] = []
        request = engine.load_bar_request
        if request is not None:
            needed_bars = _load_bar_count(request, bar_timeframe)
            warmup_end = max(len(rows) - 1, 0)
            warmup_rows = rows[max(0, warmup_end - needed_bars):warmup_end]
            callback = request.get("callback") or strategy.on_bar
            for row in warmup_rows:
                callback(_bar_from_row(row, strategy, market, bar_timeframe))

        strategy.inited = True
        strategy.trading = True
        state["inited"] = True
        state["last_ts"] = _row_ts(warmup_rows[-1]) if warmup_rows else latest_ts
        state["pos"] = float(getattr(strategy, "pos", 0.0) or 0.0)
        state["pending_stops"] = engine.pending_stops
        state["seq"] = engine._seq
        ctx.state.set(_STATE_KEY, state)
        return ctx.result.ok(
            reason="vnpy_compat_inited",
            metadata={
                "compat": "vnpy",
                "bars": len(warmup_rows),
                "pos": float(getattr(strategy, "pos", 0.0) or 0.0),
                "logs": engine.logs[-20:],
            },
        )

    new_rows = [row for row in rows if _row_ts(row) > last_ts]
    if not new_rows:
        return ctx.result.hold(
            reason="vnpy_compat_no_new_bars",
            metadata={"compat": "vnpy", "last_ts": last_ts, "pos": float(getattr(strategy, "pos", 0.0) or 0.0)},
        )

    for row in new_rows:
        bar = _bar_from_row(row, strategy, market, bar_timeframe)
        try:
            # Parked stop orders trigger on the bar's range, before the
            # strategy sees the bar — the bar-based equivalent of the
            # CTA engine's tick-crossing check.
            engine.evaluate_stops(strategy, bar)
            strategy.on_tick(_tick_from_row(row, market))
            strategy.on_bar(bar)
        except Exception as exc:
            # Keep last_ts at the last *successful* bar so the failing
            # bar is retried on the next tick; persist any stop-order
            # state that mutated before the exception.
            ctx.state.set(
                _STATE_KEY,
                {**state, "pending_stops": engine.pending_stops, "seq": engine._seq},
            )
            return ctx.result.error(message=f"vnpy_compat_on_bar_failed: {exc}", kind="compat_error")
        state["last_ts"] = _row_ts(row)

    state["pos"] = float(getattr(strategy, "pos", 0.0) or 0.0)
    state["pending_stops"] = engine.pending_stops
    state["seq"] = engine._seq
    if engine.pending_intent:
        state["pending_intent"] = engine.pending_intent
    ctx.state.set(_STATE_KEY, state)

    if engine.orders:
        metadata = {
            "compat": "vnpy",
            "orders": [
                {k: v for k, v in o.items() if k != "envelope"} for o in engine.orders
            ],
            "pos": float(getattr(strategy, "pos", 0.0) or 0.0),
            "logs": engine.logs[-20:],
        }
        enveloped = [o for o in engine.orders if o.get("envelope")]
        if enveloped:
            return StrategyResult.from_trade_envelope(
                enveloped[-1]["envelope"],
                reason="vnpy_compat_orders",
                metadata=metadata,
            )
        # Only parked stop orders (or gate-held orders) this tick — no
        # trade envelope to surface.
        return ctx.result.ok(reason="vnpy_compat_orders", metadata=metadata)
    return ctx.result.ok(
        reason="vnpy_compat_processed",
        metadata={
            "compat": "vnpy",
            "bars": len(new_rows),
            "pos": float(getattr(strategy, "pos", 0.0) or 0.0),
            "logs": engine.logs[-10:],
        },
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _positive_float(value: Any) -> Optional[float]:
    """Coerce to a strictly positive float, else ``None`` (absent field)."""

    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0.0 else None


def _row_ts(row: dict[str, Any]) -> int:
    """Row timestamp normalised to epoch milliseconds (seconds accepted)."""

    try:
        ts = int(row.get("ts") or row.get("time") or 0)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    return ts if ts > 1_000_000_000_000 else ts * 1000


def _bar_from_row(row: dict[str, Any], strategy: Any, market: str, timeframe: str) -> BarData:
    ts = _row_ts(row)
    symbol, exchange = _parse_market(market)
    minutes = _INTERVAL_MINUTES.get(timeframe, 1)
    return BarData(
        symbol=symbol,
        exchange=exchange,
        datetime=datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
        interval=Interval.MINUTE if minutes < 60 else (Interval.HOUR if minutes < 1440 else Interval.DAILY),
        open_price=float(row.get("open") or 0.0),
        high_price=float(row.get("high") or 0.0),
        low_price=float(row.get("low") or 0.0),
        close_price=float(row.get("close") or 0.0),
        volume=float(row.get("volume") or 0.0),
    )


def _tick_from_row(row: dict[str, Any], market: str) -> TickData:
    """Synthesise the once-per-bar tick the adapter feeds ``on_tick``.

    Nerya has no intra-bar tick stream, so the bar's close stands in
    for ``last_price`` (documented behaviour; the importer warns when
    a strategy defines ``on_tick``).
    """

    ts = _row_ts(row)
    symbol, exchange = _parse_market(market)
    close = float(row.get("close") or 0.0)
    return TickData(
        symbol=symbol,
        exchange=exchange,
        datetime=datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
        last_price=close,
        open_price=float(row.get("open") or 0.0),
        high_price=float(row.get("high") or 0.0),
        low_price=float(row.get("low") or 0.0),
        bid_price_1=close,
        ask_price_1=close,
        volume=float(row.get("volume") or 0.0),
    )


def _drop_pending_marker(ctx: Any, framework: str, marker: dict[str, Any]) -> None:
    """Journal the drop of an unresolved ``pending_approval`` marker.

    There is no execution-status polling API on the strategy context,
    so a marker that survives into the next tick cannot be resolved —
    leave a warning trail and move on with the position untouched.
    """

    audit = getattr(ctx, "audit", None)
    if audit is None:
        _LOG.warning(
            "compat pending_approval marker dropped without resolution: %s", marker
        )
        return
    try:
        audit.log(
            "compat_pending_dropped",
            {
                "framework": framework,
                "marker": marker,
                "note": "no execution-status API on ctx; position left untouched",
            },
            level="warn",
        )
    except Exception:
        _LOG.exception("failed to journal dropped compat pending marker")


def _parse_market(market: str) -> "tuple[str, Exchange]":
    venue, _, symbol = str(market).partition(":")
    try:
        return symbol or market, Exchange(venue.upper())
    except ValueError:
        return symbol or market, Exchange.LOCAL


def _load_bar_count(request: dict[str, Any], bar_timeframe: str) -> int:
    days = max(int(request.get("days") or 0), 0)
    hour = int(request.get("hour") or 0)
    minute = int(request.get("minute") or 0)
    interval = request.get("interval")
    minutes = _INTERVAL_MINUTES.get(bar_timeframe, 1)
    if isinstance(interval, Interval) and interval == Interval.HOUR and minutes < 60:
        minutes = 60
    total_minutes = days * 1440 + hour * 60 + minute
    if total_minutes <= 0:
        return 0
    return max(total_minutes // max(minutes, 1), 1)


def _normalise_direction(value: Any) -> Direction:
    if isinstance(value, Direction):
        return value
    text = str(getattr(value, "value", value))
    return Direction(text) if text in {"多", "空", "净"} else (Direction.LONG if text.upper() in {"LONG", "多"} else Direction.SHORT)


def _normalise_offset(value: Any) -> Offset:
    if isinstance(value, Offset):
        return value
    text = str(getattr(value, "value", value) or "")
    try:
        return Offset(text)
    except ValueError:
        return Offset.OPEN


def _map_order(direction: Direction, offset: Offset) -> "tuple[Optional[str], Optional[str]]":
    """Map (Direction, Offset) onto a Nerya (side, plan_action).

    Entries are tagged ``open_position`` explicitly — a bare ``sell`` is
    misread as a close by downstream heuristics, and short entries would
    be dropped in backtest settle.
    """

    if direction == Direction.LONG and offset == Offset.OPEN:
        return "buy", "open_position"
    if direction == Direction.SHORT and offset == Offset.OPEN:
        return "sell", "open_position"
    if direction == Direction.SHORT and offset in {Offset.CLOSE, Offset.CLOSETODAY}:
        return "sell", "close_position"
    if direction == Direction.LONG and offset in {Offset.CLOSE, Offset.CLOSETODAY}:
        return "buy", "close_position"
    return None, None


__all__ = ["VnpyAdapterError", "VnpyOrderBridge", "run_vnpy_tick"]
