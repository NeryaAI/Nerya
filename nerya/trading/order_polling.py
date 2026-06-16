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

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..connectors import ConnectorRegistry
from ..core.config import Config
from ..core.ids import fill_id as _new_fill_id
from .accounts import load_accounts
from .order_tracker import OrderTracker, TERMINAL_STATES, TrackedOrder
from .position_book import PositionBook

log = logging.getLogger(__name__)


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


def _safe_get_order(
    connector,
    *,
    market: str,
    order_id: str,
):
    """Call ``connector.get_order`` with a tiny shim that returns
    ``(ack, error)`` instead of raising. Keeps the poll loop linear."""

    try:
        return connector.get_order(market=market, order_id=order_id), None
    except NotImplementedError as exc:
        return None, ("unsupported", str(exc))
    except Exception as exc:
        return None, ("error", str(exc))


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
        active = tracker.active_orders()
        for order in active:
            if account_filter is not None and order.account_id not in account_filter:
                out.skipped += 1
                continue
            out.scanned += 1
            per: dict[str, Any] = {
                "order_id": order.order_id,
                "exchange_order_id": order.exchange_order_id,
                "market": order.market,
            }

            account = accounts.get(order.account_id)
            if account is None:
                # Account was deleted under us. Mark the order ``lost`` so
                # operators see it in /incidents and can decide whether to
                # cancel via the venue manually.
                tracker.mark_not_found(order.order_id, ts=now)
                out.not_found += 1
                per["state"] = "account_missing"
                out.per_order.append(per)
                continue

            try:
                if connector_factory is not None:
                    connector = connector_factory(account.id, account.connector_cfg())
                else:
                    connector = registry.get(account.id, account.connector_cfg())
            except Exception as exc:
                out.errors += 1
                tracker.mark_not_found(order.order_id, ts=now)
                per["state"] = f"connector_error:{exc}"
                out.per_order.append(per)
                continue

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
                else:
                    out.errors += 1
                    tracker.mark_not_found(order.order_id, ts=now)
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
                # Record the late fill on the tracker (rolls up
                # filled_size / avg_price / fee_usd) AND mirror it onto
                # the PositionBook so the merged position stays in lock-step
                # with the broker.
                fill = tracker.record_fill(
                    order_id=order.order_id,
                    price=float(price_for_fill),
                    size_base=float(new_filled_delta),
                    fee_usd=float(ack_fee),
                    source="live",
                    meta={
                        "via": "background_poller",
                        "intent_id": order.intent_id,
                        "exchange_order_id": order.exchange_order_id,
                    },
                )
                try:
                    book.apply_fill(
                        account_id=order.account_id,
                        strategy_id=order.strategy_id,
                        market=order.market,
                        side=order.side,
                        price=float(price_for_fill),
                        size_base=float(new_filled_delta),
                        fee_usd=float(ack_fee),
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

            terminal_status = _normalize_ack_status(ack)
            if terminal_status in TERMINAL_STATES:
                tracker.update_state(order.order_id, terminal_status, ts=now)
                out.terminal += 1
                per["state"] = terminal_status

            if "state" not in per:
                per["state"] = terminal_status or "open"
            out.per_order.append(per)
    finally:
        if owns_tracker:
            tracker.close()
        if owns_book:
            book.close()

    return out


__all__ = ["PollResult", "poll_active_live_orders"]
