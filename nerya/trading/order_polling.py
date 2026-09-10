"""Background poller for live orders that the legacy submit path
registered with :class:`OrderTracker`.

The :class:`MarketOrderExecutor` is event-driven inside a TradePlan run;
once that run hands off, late fills on a placed order are only visible
to the broker. This module walks the tracker's ``active_orders`` slice
every tick (driven by the same background loop infrastructure as
``_start_account_refresh_loop``), calls ``connector.get_order``,
applies any new fills to the :class:`PositionBook`, and promotes the
tracker row through terminal states.

The module exports :func:`poll_active_live_orders` so unit tests can
drive a single tick deterministically without spinning up a thread.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..connectors import ConnectorRegistry
from ..core.config import Config
from .accounts import load_accounts
from .capital import CapitalReservationStore
from .order_tracker import OrderTracker, TERMINAL_STATES, TrackedOrder
from .position_book import PositionBook

log = logging.getLogger(__name__)


# R2B3: how long a ``place_unknown`` order may stay un-adopted before
# the poller gives up and marks it ``lost`` (terminal — releases the
# reservation and fails the run with ``place_unknown_expired``). Market
# orders that the venue never received must not wedge the poller (and
# hold capital) forever.
PLACE_UNKNOWN_MAX_AGE_S = 600.0

# R3T4: how many consecutive adoption cycles where every queried source
# SUCCEEDED and the order was genuinely absent are required before the
# ``lost`` age bound may fire. Any cycle where an adoption source ERRORED
# resets the streak — a failed fetch is not evidence of absence, and
# aging on it used to let R2B9 refuse the late fill forever.
ADOPT_ABSENT_MIN_CYCLES = 2


@dataclass
class PollResult:
    """Outcome of a single poll pass for observability + tests."""

    scanned: int = 0
    fills_applied: int = 0
    terminal: int = 0
    errors: int = 0
    not_found: int = 0
    skipped: int = 0
    per_order: list[dict[str, Any]] = field(default_factory=list)


def _venue_of(market: str) -> str:
    """Best-effort venue extraction from a ``VENUE:SYMBOL`` market id."""
    if ":" in market:
        return market.split(":", 1)[0].lower()
    return ""


def is_definitive_not_found_error(exc: BaseException | str) -> bool:
    """True when ``get_order`` provably reported 'no such order'.

    B7: only venue not-found evidence may advance the 4-strike ``lost``
    counter — transport, auth, and unknown errors must leave the streak
    untouched so a flaky connection can't mark a live order lost.

    R3T3: an explicit ``not_found=True`` attribute on the exception
    (set by the ccxt adapter when ccxt classified the failure as
    ``OrderNotFound``) is definitive — message substrings stay as the
    fallback for connectors that cannot classify. The
    ``order not exists`` phrase covers Bybit's retMsg
    ("order not exists or too late to be cancelled"), which none of the
    older substrings matched.
    """
    if bool(getattr(exc, "not_found", False)):
        return True
    text = exc if isinstance(exc, str) else f"{type(exc).__name__} {exc}"
    haystack = text.lower()
    return (
        "ordernotfound" in haystack
        or "order not found" in haystack
        or "order does not exist" in haystack
        or "order not exists" in haystack
        or "not found" in haystack
    )


def is_place_unknown_order(order: TrackedOrder) -> bool:
    """True when the order's place outcome is still unknown (B1).

    Set by the executor when ``place_order`` fails ambiguously (e.g. a
    timeout after the venue may have accepted): the row carries a
    ``place_unknown`` meta flag and/or ``place_unknown:`` notes. Such
    orders must be adopted by client id — never re-placed, and never
    treated as venue not-founds.
    """
    meta = order.meta or {}
    if meta.get("place_unknown"):
        return True
    return any(
        str(note).startswith("place_unknown")
        for note in (meta.get("notes") or [])
    )


def _acks_matching_client_id(acks: Any, client_order_id: str) -> list[Any]:
    return [
        ack
        for ack in (acks or [])
        if str(getattr(ack, "client_order_id", "") or "") == client_order_id
    ]


def _fetch_list(
    connector,
    method: str,
    *,
    errors: list[str] | None = None,
) -> list[Any] | None:
    """Call an optional list-returning connector method defensively.

    Returns ``None`` when the connector doesn't implement the method or
    the call fails — callers then simply skip that adoption source.
    When ``errors`` is supplied, fetch failures are appended as
    ``"<method>:<exception>"`` so callers can tell "venue says absent"
    from "we could not ask" (R3T4) instead of conflating both.
    """
    fetch = getattr(connector, method, None)
    if not callable(fetch):
        return None
    try:
        return fetch() or []
    except Exception as exc:
        if errors is not None:
            errors.append(f"{method}:{exc}")
        return None


def adopt_venue_order(
    connector,
    tracker: OrderTracker,
    order: TrackedOrder,
    *,
    query_state: dict[str, Any] | None = None,
):
    """Look up a venue order by client id when the exchange id is unknown.

    B1 recovery: after an ambiguous ``place_order`` (e.g. a timeout
    after the venue accepted) the tracker row has no
    ``exchange_order_id``. ``CcxtAdapter.get_order`` cannot fetch by
    client id, so we scan, in order:

    1. open orders (resting orders) — adopt and return the ack;
    2. closed orders (``fetch_closed_orders`` when the connector
       implements it) — the dominant ambiguous case is a MARKET order
       that filled instantly and never rested in open orders (R2B3);
    3. recent fills (``fetch_my_trades`` when available) — trades carry
       the client order id even when the order endpoints do not.

    Returns the ack when found (the caller's normal flow then applies
    fills / terminal status), else ``None`` — callers must keep the
    order tracked and try again next tick.

    R3T4: when ``query_state`` is supplied it is populated with
    ``{"errors": [...], "queried": n}`` so callers can distinguish a
    genuine absence (all queried sources succeeded, order not there)
    from a lookup failure (a source raised — its answer was unknown).
    """

    def _sources() -> tuple[list[Any], list[Any]]:
        errors: list[str] = []
        open_orders = _fetch_list(connector, "fetch_open_orders", errors=errors)
        closed_orders = _fetch_list(connector, "fetch_closed_orders", errors=errors)
        trades = _fetch_list(connector, "fetch_my_trades", errors=errors)
        if query_state is not None:
            query_state["errors"] = errors
            query_state["queried"] = sum(
                1 for r in (open_orders, closed_orders, trades) if r is not None
            )
        return (open_orders or []) + (closed_orders or []), trades or []

    client_id = order.client_order_id

    # 1 + 2. Resting open orders first, then closed orders —
    # instantly-filled market orders never show up in open orders, so
    # closed orders are where most adoptions actually happen.
    orders, trades = _sources()
    for ack in _acks_matching_client_id(orders, client_id):
        tracker.mark_submitted(
            order.order_id, exchange_order_id=getattr(ack, "order_id", None),
        )
        tracker.annotate(order.order_id, "adopted_place_unknown")
        return ack

    # 3. Trades by client id. Each trade is one fill of the order; fold
    # the matching trades into a synthetic cumulative ack. A trade ack
    # carries no order status, so ``filled`` is treated as terminal —
    # a *partially* filled order would still be resting in open orders
    # and be adopted by (1) instead.
    matched = _acks_matching_client_id(trades, client_id)
    if matched:
        total_filled = sum(float(getattr(t, "filled", None) or 0.0) for t in matched)
        total_fee = sum(float(getattr(t, "fee_usd", None) or 0.0) for t in matched)
        notional = sum(
            float(getattr(t, "filled", None) or 0.0)
            * float(getattr(t, "avg_price", None) or getattr(t, "price", None) or 0.0)
            for t in matched
        )
        avg_price = (notional / total_filled) if total_filled > 0 else 0.0
        first = matched[0]
        tracker.mark_submitted(
            order.order_id, exchange_order_id=getattr(first, "order_id", None),
        )
        tracker.annotate(
            order.order_id,
            f"adopted_place_unknown_via_trades:{len(matched)}",
        )
        from ..connectors.base import OrderAck as _OrderAck

        return _OrderAck(
            order_id=str(getattr(first, "order_id", "") or order.order_id),
            client_order_id=client_id,
            status="filled",
            market=order.market,
            side=order.side,
            price=avg_price or None,
            size=total_filled or None,
            filled=total_filled,
            avg_price=avg_price,
            fee_usd=total_fee,
        )
    return None


def place_unknown_age_s(
    order: TrackedOrder,
    *,
    now: float | None = None,
    tracker: OrderTracker | None = None,
) -> float | None:
    """Seconds since the order's place outcome first became unknown.

    Resolution order (R2B3): an explicit ``place_unknown_ts`` in the
    order meta (stamped by newer executors), the first ``place_unknown``
    note event in the tracker's event log, then the submit/creation
    timestamps. Returns ``None`` when nothing timestamps the flag
    (callers should then leave the order alone rather than invent an
    age).
    """
    now = now if now is not None else time.time()
    meta = order.meta or {}
    try:
        explicit = float(meta.get("place_unknown_ts"))
        if explicit > 0:
            return max(0.0, now - explicit)
    except (TypeError, ValueError):
        pass
    if tracker is not None:
        try:
            for event in tracker.events_for_order(order.order_id):
                note = str((event.payload or {}).get("note") or "")
                if note.startswith("place_unknown"):
                    return max(0.0, now - float(event.ts))
        except Exception:
            pass
    anchor = order.submitted_at or order.created_at
    if anchor and anchor > 0:
        return max(0.0, now - float(anchor))
    return None


def expire_place_unknown_order(
    tracker: OrderTracker,
    order: TrackedOrder,
    *,
    now: float | None = None,
    max_age_s: float = PLACE_UNKNOWN_MAX_AGE_S,
) -> bool:
    """Mark a long-unadopted ``place_unknown`` order ``lost`` (R2B3).

    Terminal, so ``active_orders`` stops polling it, the executor's
    lost-finalization path releases the reservation, and the run fails
    with reason ``place_unknown_expired`` for operator review. Returns
    True when the order was expired.
    """
    age = place_unknown_age_s(order, now=now, tracker=tracker)
    if age is None or age < float(max_age_s):
        return False
    tracker.update_state(order.order_id, "lost", ts=now, payload={
        "reason": "place_unknown_expired",
        "place_unknown_age_s": round(age, 3),
    })
    tracker.annotate(
        order.order_id,
        f"place_unknown_expired:unadopted_for_{int(age)}s",
    )
    log.warning(
        "order %s place_unknown for %.0fs without adoption — marked lost",
        order.order_id, age,
    )
    return True


# ---------------------------------------------------------------------------
# R3T4 helpers — adoption-cycle bookkeeping
# ---------------------------------------------------------------------------


def _update_order_meta(
    tracker: OrderTracker,
    order_id: str,
    updates: dict[str, Any],
) -> None:
    """Merge ``updates`` into the tracked order's durable meta.

    Package-local write path (the tracker exposes no public meta
    mutator) used to persist the adoption-streak counters once per poll
    cycle without spamming the note/event log.
    """
    order = tracker.get(order_id)
    if order is None:
        return
    meta = dict(order.meta or {})
    meta.update(updates)
    try:
        con = tracker._con_lazy()
        con.execute(
            "UPDATE orders SET meta_json = ?, updated_at = ? WHERE order_id = ?",
            (json.dumps(meta), time.time(), order_id),
        )
    except Exception:  # pragma: no cover - bookkeeping must not break polls
        log.exception("could not persist adoption meta for order %s", order_id)


def _adopt_absent_reset(
    tracker: OrderTracker,
    order: TrackedOrder,
    now: float,
    reason: str,
) -> None:
    """Reset the clean-absent streak after a failed adoption lookup.

    A fetch ERROR is not evidence of absence: aging must not advance on
    a cycle where a source could not be queried (R3T4). The annotate is
    once per broken streak — an operator-visible trace, not per-tick
    noise.
    """
    meta = order.meta or {}
    if int(meta.get("adopt_absent_streak") or 0):
        tracker.annotate(order.order_id, f"adopt_absent_streak_reset:{reason}")
    _update_order_meta(tracker, order.order_id, {
        "adopt_absent_streak": 0,
        "adopt_absent_last_error": f"{now:.0f}:{reason}",
    })


def _adopt_absent_bump(
    tracker: OrderTracker,
    order: TrackedOrder,
    now: float,
) -> int:
    """Count one clean cycle where all queried sources succeeded and the
    order was genuinely absent. Returns the new streak length."""
    streak = int((order.meta or {}).get("adopt_absent_streak") or 0) + 1
    _update_order_meta(tracker, order.order_id, {
        "adopt_absent_streak": streak,
        "adopt_absent_last_cycle": f"{now:.0f}",
    })
    return streak


def _settle_reservation(paths: Any, reservation_id: str, *, filled: bool) -> None:
    """Consume (on fill) or release (any other terminal state) the
    reservation linked to a tracker order the poller just drove terminal.

    R3T1/R3T2: when the executor run is already terminal (cancel raced a
    partial fill, or a transient cancel failure left the order in
    ``cancel_requested``), nobody else settles this reservation — the
    poller is the last writer. Both transitions are guarded (R2B6), so
    an executor that is still alive settles identically afterwards and
    the duplicate transition is a no-op.
    """
    try:
        store = CapitalReservationStore(paths)
        if filled:
            store.consume(reservation_id)
        else:
            store.release(reservation_id)
    except Exception:
        # Never break the poll tick for bookkeeping — reconciliation
        # and the reservation TTL sweep catch any drift.
        log.exception(
            "reservation settle failed for %s (filled=%s)",
            reservation_id, filled,
        )


def _safe_get_order(
    connector,
    *,
    market: str,
    order_id: str,
):
    """Call ``connector.get_order`` with a tiny shim that returns
    ``(ack, error)`` instead of raising. Keeps the poll loop linear.
    ``error`` is ``(kind, exception_or_str)`` so callers can classify
    definitive venue not-founds from transport noise."""

    try:
        return connector.get_order(market=market, order_id=order_id), None
    except NotImplementedError as exc:
        return None, ("unsupported", str(exc))
    except Exception as exc:
        return None, ("error", exc)


def _normalize_ack_status(ack) -> str:
    """Normalize broker-reported statuses into tracker states.

    CCXT and direct REST adapters return mixed casings (``filled`` vs.
    ``closed`` vs. ``CANCELED``) — fold them into a stable set so the
    tracker transition logic stays boring.
    """
    raw = (getattr(ack, "status", None) or "").strip().lower()
    if raw in {"filled", "closed", "done"}:
        return "filled"
    if raw in {"canceled", "cancelled"}:
        return "canceled"
    if raw == "rejected":
        return "rejected"
    if raw == "expired":
        return "expired"
    return raw  # open / partially_filled / new / etc — pass through


def poll_active_live_orders(
    config: Config,
    *,
    registry: ConnectorRegistry | None = None,
    tracker: OrderTracker | None = None,
    book: PositionBook | None = None,
    now: float | None = None,
    connector_factory: Callable[[str, dict[str, Any]], Any] | None = None,
    account_filter: Iterable[str] | None = None,
) -> PollResult:
    """Run one poll tick across every tracker order that isn't terminal.

    Parameters
    ----------
    config:
        Workspace config (paths + registry resolution).
    registry, tracker, book:
        Optional injection points. Defaults to constructing fresh
        instances against ``config.paths`` — the poller is cheap to
        instantiate so callers don't need to pool these.
    now:
        Override the wall-clock for deterministic tests.
    connector_factory:
        Override the connector lookup. Tests pass a callable
        ``(account_id, connector_cfg) -> Connector`` so they don't need
        to register a real ConnectorRegistry. Production code leaves
        this ``None`` and uses ``registry.get``.
    account_filter:
        When supplied, only poll orders whose ``account_id`` is in this
        set. Useful for per-account refresh hooks.
    """

    paths = config.paths
    owns_tracker = tracker is None
    owns_book = book is None
    tracker = tracker or OrderTracker(paths)
    book = book or PositionBook(paths)
    registry = registry or ConnectorRegistry(workspace=paths.root)
    now = now if now is not None else time.time()
    out = PollResult()

    try:
        accounts = load_accounts(paths)
        # B7(a): profile modes expose paper/shadow accounts whose
        # "orders" never touch the real connector — resolve profiles so
        # those accounts can be skipped below.
        try:
            from .accounts import load_account_profiles

            profiles = load_account_profiles(paths)
        except Exception:
            profiles = {}
        active = tracker.active_orders()
        for order in active:
            per: dict[str, Any] = {
                "order_id": order.order_id,
                "exchange_order_id": order.exchange_order_id,
                "market": order.market,
            }
            profile = profiles.get(order.account_id)
            if profile is not None and profile.mode in ("paper", "shadow"):
                # Paper / shadow resting orders must never be polled via
                # the account's real connector.
                out.skipped += 1
                per["state"] = f"mode_skipped:{profile.mode}"
                out.per_order.append(per)
                continue
            if account_filter is not None and order.account_id not in account_filter:
                out.skipped += 1
                continue
            out.scanned += 1

            account = accounts.get(order.account_id)
            if account is None:
                # R2B5: the account was deleted under us. That is NOT
                # venue not-found evidence — counting a strike here
                # could release the reservation of a live venue order.
                # Leave a durable note for operators and skip until the
                # account is restored or the order is handled manually.
                tracker.annotate(order.order_id, "account_missing")
                out.skipped += 1
                per["state"] = "account_missing"
                out.per_order.append(per)
                continue

            try:
                if connector_factory is not None:
                    connector = connector_factory(account.id, account.connector_cfg())
                else:
                    connector = registry.get(account.id, account.connector_cfg())
            except Exception as exc:
                # Connector construction / auth problems are not venue
                # not-found evidence — skip this round (B7b).
                out.errors += 1
                per["state"] = f"connector_error:{exc}"
                out.per_order.append(per)
                continue

            if order.exchange_order_id is None and is_place_unknown_order(order):
                # B1: the place outcome was ambiguous — try to adopt the
                # venue order by client id, otherwise wait for a later
                # tick. Never treat our internal id as a venue not-found.
                query_state: dict[str, Any] = {}
                ack = adopt_venue_order(
                    connector, tracker, order, query_state=query_state,
                )
                if ack is None:
                    errors = list(query_state.get("errors") or [])
                    if errors:
                        # R3T4: a failed lookup is NOT evidence of
                        # absence — a source that errored may be hiding
                        # a real filled order (R2B9 would then refuse
                        # the late fill forever). Skip this cycle
                        # WITHOUT aging, reset the clean-absent streak
                        # and leave a note.
                        _adopt_absent_reset(tracker, order, now, errors[0])
                        out.errors += 1
                        per["state"] = f"place_unknown_query_error:{errors[0]}"
                        out.per_order.append(per)
                        continue
                    # Every queried source succeeded and the order was
                    # genuinely absent — one clean cycle toward aging.
                    streak = _adopt_absent_bump(tracker, order, now)
                    order = tracker.get(order.order_id) or order
                    # R2B3: bound how long an un-adopted place_unknown
                    # order stays live. A market order the venue never
                    # received will never appear anywhere — age it to
                    # ``lost`` (terminal → reservation released, run
                    # fails with ``place_unknown_expired``) so the row
                    # doesn't wedge the poller forever. R3T4: only after
                    # enough consecutive CLEAN cycles — errors above
                    # reset the streak instead of aging the order.
                    if (
                        streak >= ADOPT_ABSENT_MIN_CYCLES
                        and expire_place_unknown_order(tracker, order, now=now)
                    ):
                        out.terminal += 1
                        per["state"] = "place_unknown_expired"
                    else:
                        out.skipped += 1
                        per["state"] = "place_unknown_pending"
                    out.per_order.append(per)
                    continue
                order = tracker.get(order.order_id) or order
            else:
                ack, err = _safe_get_order(
                    connector,
                    market=order.market,
                    order_id=order.exchange_order_id or order.order_id,
                )
                if err is not None:
                    kind, detail = err
                    if kind == "unsupported":
                        # The venue can't be polled. Best the poller can do is
                        # leave the row alone and trust reconciliation to catch
                        # drift later.
                        out.skipped += 1
                        per["state"] = "unsupported"
                    elif is_definitive_not_found_error(detail):
                        # F5: one recovery attempt by client id before
                        # counting the strike toward ``lost``.
                        adopted = adopt_venue_order(connector, tracker, order)
                        if adopted is not None:
                            ack = adopted
                            order = tracker.get(order.order_id) or order
                        else:
                            tracker.mark_not_found(order.order_id, ts=now)
                            out.not_found += 1
                            per["state"] = f"not_found:{detail}"
                            out.per_order.append(per)
                            continue
                    else:
                        # Transport / auth / unknown errors must NOT
                        # increment the not-found streak (B7b).
                        out.errors += 1
                        per["state"] = f"poll_error:{detail}"
                        out.per_order.append(per)
                        continue

            tracker.mark_seen(order.order_id, ts=now)
            ack_filled = float(getattr(ack, "filled", None) or 0.0)
            ack_avg = float(getattr(ack, "avg_price", None) or 0.0)
            ack_fee = float(getattr(ack, "fee_usd", None) or 0.0)
            new_filled_delta = ack_filled - float(order.filled_size or 0.0)
            if new_filled_delta > 1e-12:
                price_for_fill = ack_avg or order.avg_price or order.price or 0.0
                # B2: ``ack_fee`` is the venue's *cumulative* order fee —
                # only the not-yet-recorded increment may be added.
                incremental_fee = max(0.0, ack_fee - float(order.fee_usd or 0.0))
                # Record the late fill on the tracker (rolls up
                # filled_size / avg_price / fee_usd) AND mirror it onto
                # the PositionBook so the merged position stays in lock-step
                # with the broker. ``cumulative_filled`` makes this
                # idempotent against a concurrent executor tick (B4).
                #
                # R2B8: across processes the executor and this poller can
                # race the same fill — both derive the same deterministic
                # fill id and one of them loses the fills-PK INSERT. An
                # IntegrityError here is a *replay*, not a fault: swallow
                # it (log + skip the mirror) instead of aborting the whole
                # poll tick for every other order.
                try:
                    fill = tracker.record_fill(
                        order_id=order.order_id,
                        price=float(price_for_fill),
                        size_base=float(new_filled_delta),
                        fee_usd=incremental_fee,
                        source="live",
                        cumulative_filled=ack_filled,
                        meta={
                            "via": "background_poller",
                            "intent_id": order.intent_id,
                            "exchange_order_id": order.exchange_order_id,
                        },
                    )
                    if fill is not None:
                        try:
                            book.apply_fill(
                                account_id=order.account_id,
                                strategy_id=order.strategy_id,
                                market=order.market,
                                side=order.side,
                                price=float(price_for_fill),
                                size_base=float(new_filled_delta),
                                fee_usd=incremental_fee,
                                venue=_venue_of(order.market),
                                leverage=float(order.leverage or 1.0),
                                source="live",
                                executor_id=order.executor_id,
                                order_id=order.order_id,
                                fill_id=fill.fill_id,
                            )
                        except Exception:
                            # Don't let a PositionBook hiccup wedge the poll loop.
                            # ``reconciliation`` will surface the drift next pass.
                            log.exception("position book apply_fill failed for order %s", order.order_id)
                    out.fills_applied += 1
                    per["fill_size_base"] = new_filled_delta
                    per["fill_price"] = price_for_fill
                except sqlite3.IntegrityError as exc:
                    # Concurrent executor recorded the same fill first —
                    # treat as replay and keep polling.
                    log.info(
                        "fill for order %s already recorded (replay): %s",
                        order.order_id, exc,
                    )
                    per["state"] = "fill_replay"

            terminal_status = _normalize_ack_status(ack)
            if terminal_status in TERMINAL_STATES:
                tracker.update_state(order.order_id, terminal_status, ts=now)
                out.terminal += 1
                per["state"] = terminal_status
                # R3T1/R3T2: settle the tracked reservation when the
                # poller drives an order terminal. Critical when the
                # executor run is already gone (cancel raced a partial
                # fill / transient cancel failure): consume on a real
                # fill so the capital stays spent for the open position,
                # release on any other terminal state so it frees. Both
                # are guarded transitions (R2B6) — a still-alive
                # executor finalizing afterwards is a no-op duplicate.
                if order.reservation_id:
                    _settle_reservation(
                        paths,
                        order.reservation_id,
                        filled=(terminal_status == "filled"),
                    )
                    per["reservation_settled"] = order.reservation_id

            if "state" not in per:
                per["state"] = terminal_status or "open"
            out.per_order.append(per)
    finally:
        if owns_tracker:
            tracker.close()
        if owns_book:
            book.close()

    return out


__all__ = [
    "ADOPT_ABSENT_MIN_CYCLES",
    "PollResult",
    "PLACE_UNKNOWN_MAX_AGE_S",
    "adopt_venue_order",
    "expire_place_unknown_order",
    "is_definitive_not_found_error",
    "is_place_unknown_order",
    "place_unknown_age_s",
    "poll_active_live_orders",
]
