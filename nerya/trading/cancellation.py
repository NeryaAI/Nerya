"""Cancel tracked orders without losing fills or uncertain venue outcomes."""
from __future__ import annotations

from contextlib import closing
import time

from .accounts import get_account_profile
from .access_control import guard_http_trade_scope
from .capital import CapitalReservationStore
from .order_tracker import OrderTracker
from .position_book import PositionBook


def cancel_tracked_order(config, order_id: str, *, strategy_id=None, auth_context=None, connector=None, executor_locked=False):
    from .locks import trading_lock
    with closing(OrderTracker(config.paths)) as tracker:
        order = tracker.get(order_id)
    if order is not None and order.executor_id and not executor_locked:
        with trading_lock(config.paths, order.executor_id) as acquired:
            if not acquired:
                return {"ok": False, "order_id": order_id, "error": "order_busy_retry"}
            return cancel_tracked_order(config, order_id, strategy_id=strategy_id,
                auth_context=auth_context, connector=connector, executor_locked=True)
    with trading_lock(config.paths, f"cancel:{order_id}") as acquired:
        if not acquired:
            return {"ok": False, "order_id": order_id, "error": "order_busy_retry"}
        return _cancel_locked(config, order_id, strategy_id=strategy_id, auth_context=auth_context, connector=connector)


def _cancel_locked(config, order_id, *, strategy_id, auth_context, connector):
    with closing(OrderTracker(config.paths)) as tracker:
        order = tracker.get(order_id)
        if order is None or (strategy_id and order.strategy_id != strategy_id):
            return {"ok": False, "error": "order_not_found", "order_id": order_id}
        denial = guard_http_trade_scope(config, auth_context, account_id=order.account_id, action="cancel_order")
        if denial is not None:
            return denial
        profile = get_account_profile(config.paths, order.account_id)
        from .accounts import account_revision
        if order.meta.get("account_binding") and order.meta["account_binding"]!=account_revision(profile):
            tracker.annotate(order_id,"account_binding_changed")
            return {"ok":False,"error":"account_binding_changed","order_id":order_id}
        if not profile.permissions.cancel_order:
            return {"ok": False, "error": "cancel_order_disabled", "order_id": order_id}
        if order.is_terminal:
            return {"ok": order.state == "canceled", "state": order.state,
                    "error": None if order.state == "canceled" else "order_not_cancellable", "order_id": order_id}
        tracker.request_cancel(order_id)
        if "cancel_requested" not in (order.meta or {}).get("notes", []):
            tracker.annotate(order_id, "cancel_requested")
        if not profile.is_real_money or (order.submitted_at is None and order.state == "created"):
            tracker.confirm_cancel(order_id)
        else:
            try:
                if connector is None:
                    from ..connectors import ConnectorRegistry
                    connector = ConnectorRegistry(workspace=config.paths.root).get(
                        profile.id, profile.to_connector_account().connector_cfg(),
                        **({"provider_binding":order.meta["provider_binding"]} if order.meta.get("provider_binding") else {}),
                    )
                if not order.exchange_order_id:
                    from .order_polling import adopt_venue_order
                    adopt_venue_order(connector, tracker, order)
                    order = tracker.get(order_id)
                if order.exchange_order_id:
                    attempts = [float(n.partition(":")[2]) for n in (order.meta or {}).get("notes", [])
                                if str(n).startswith("cancel_attempt:")]
                    if not attempts or time.time() - max(attempts) >= 5.0:
                        tracker.annotate(order_id, f"cancel_attempt:{time.time()}")
                        try:
                            connector.cancel_order(market=order.market, order_id=order.exchange_order_id,
                                **({"query_params": order.meta["order_query"]} if order.meta.get("order_query") else {}))
                        except Exception as exc:
                            tracker.annotate(order_id, f"cancel_failed_still_polling:{exc}")
                    # Cancel acknowledgement alone can race a fill. Read the
                    # cumulative execution before releasing capital.
                    ack = connector.get_order(market=order.market, order_id=order.exchange_order_id,
                        **({"query_params": order.meta["order_query"]} if order.meta.get("order_query") else {}))
                    filled = float(getattr(ack, "filled", None) or 0.0)
                    delta = filled - order.filled_size
                    price = float(getattr(ack, "avg_price", None) or order.avg_price or order.price or 0.0)
                    if delta > 1e-12:
                        if price <= 0:
                            raise ValueError("cancel fill has no execution price")
                        fee = max(0.0, float(getattr(ack, "fee_usd", None) or 0.0) - order.fee_usd)
                        fill = tracker.record_fill(order_id=order_id, price=price, size_base=delta,
                                                   fee_usd=fee, source="live", cumulative_filled=filled)
                        if fill is not None:
                            with closing(PositionBook(config.paths)) as book:
                                book.apply_fill(account_id=order.account_id, strategy_id=order.strategy_id,
                                    market=order.market, side=order.side, price=price, size_base=delta,
                                    fee_usd=fee, leverage=order.leverage, source="live",
                                    order_id=order_id, fill_id=fill.fill_id, executor_id=order.executor_id)
                    state = str(getattr(ack, "status", "") or "").lower()
                    if state in ("filled", "closed"):
                        state = "filled" if filled >= float(order.size_base or 0) - 1e-12 else "partially_filled"
                    if state == "cancelled":
                        state = "canceled"
                    if state in ("filled", "canceled", "rejected", "expired", "partially_filled"):
                        tracker.update_state(order_id, state)
            except Exception as exc:
                tracker.annotate(order_id, f"cancel_failed_still_polling:{exc}")
        order = tracker.get(order_id)
        if order.filled_size > 0 and order.executor_id:
            from .executors.market_order import ensure_order_protection
            ensure_order_protection(config, order)
        if order.is_terminal and order.reservation_id:
            store = CapitalReservationStore(config.paths)
            if order.filled_size > 0:
                store.consume(order.reservation_id)
            else:
                store.release(order.reservation_id)
        return {"ok": True, "order_id": order_id, "state": order.state,
                "status": order.state, "pending": not order.is_terminal}
