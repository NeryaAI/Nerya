"""Market-order executor.

Single-leg market order with optional protection rule attached on
fill. Behaves identically in paper, shadow, canary, and live modes:

* paper / shadow — fills via the deterministic paper simulator;
  reservation is consumed and the position book is updated.
* canary / live — places via the configured connector
  (``CcxtConnector`` for CEX). The :class:`OrderTracker` durably
  records every transition; even a crash mid-place doesn't lose state.

Crash recovery is straightforward: when the orchestrator reloads a
run mid-flight, ``step()`` finds an existing :class:`TrackedOrder`
and either polls ``fetch_order`` or, in paper mode, immediately
finalises since paper fills are synchronous.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..account_snapshots import latest_snapshot
from ..accounts import get_account_profile
from ..capital import CapitalReservationStore
from ..order_intents import OrderCandidate
from ..order_tracker import OrderTracker, make_client_order_id
from ..position_book import PositionBook
from ..protection_store import ProtectionStore
from .base import Executor, ExecutorConfig

log = logging.getLogger(__name__)


_PAPER_FEE_BPS = 5.0
_PAPER_SLIPPAGE_BPS = 2.0


@dataclass
class MarketOrderConfig(ExecutorConfig):
    candidate: dict[str, Any] = field(default_factory=dict)
    protection: dict[str, Any] | None = None


class MarketOrderExecutor(Executor):
    kind = "market_order"

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        candidate = self._candidate()
        if candidate.notional_usd <= 0 and candidate.size_base in (None, 0):
            self.transition("rejected", close_type="failed")
            self.store_result({"reason": "candidate_has_no_size"})
            return

        if not candidate.client_order_id:
            candidate.client_order_id = make_client_order_id(
                strategy_id=candidate.strategy_id,
                executor_id=self.run.executor_id,
                seq=0,
            )

        tracker = self._tracker()
        # Idempotent register: if we already have one for this client_order_id
        # (re-running after a crash) re-use it; otherwise create.
        existing = tracker.get_by_client_order_id(candidate.client_order_id)
        if existing is None:
            order = tracker.register(
                client_order_id=candidate.client_order_id,
                account_id=candidate.account_id,
                strategy_id=candidate.strategy_id,
                market=candidate.market,
                side=candidate.side,
                order_type=candidate.order_type,
                size_base=candidate.size_base,
                notional_usd=candidate.notional_usd,
                price=candidate.price,
                leverage=candidate.leverage,
                reduce_only=candidate.reduce_only,
                time_in_force=candidate.time_in_force,
                intent_id=self.run.intent_id,
                plan_id=self.run.plan_id,
                reservation_id=candidate.reservation_id or None,
                executor_id=self.run.executor_id,
                meta={"resized": candidate.resized, "fee_estimate_usd": candidate.estimated_fee_usd},
            )
        else:
            order = existing
        self.attach_order(order.order_id)
        if candidate.reservation_id:
            self.attach_reservation(candidate.reservation_id)

        # persist the (possibly mutated) candidate so the orchestrator
        # row keeps the canonical version
        self.run.config_json["candidate"] = candidate.asdict()

    def step(self) -> bool:
        if self.run.state == "rejected":
            return True

        tracker = self._tracker()
        if not self.run.order_ids:
            self.transition("failed", close_type="failed")
            self.store_result({"reason": "no_tracked_order"})
            return True
        order_id = self.run.order_ids[0]
        order = tracker.get(order_id)
        if order is None:
            self.transition("failed", close_type="failed")
            self.store_result({"reason": "tracked_order_missing"})
            return True

        try:
            profile = get_account_profile(self.paths, self.run.account_id)
        except Exception as exc:  # pragma: no cover
            log.exception("could not resolve account profile")
            self.transition("failed", close_type="failed")
            self.store_result({"reason": f"account_profile_error:{exc}"})
            return True

        candidate = self._candidate()
        venue_mode = profile.mode

        # If we've already reached a terminal order state, finalize.
        if order.state == "filled":
            return self._finalize(filled=True)
        if order.state in ("rejected", "failed", "expired"):
            return self._finalize(filled=False, reason=order.state)
        if order.state == "canceled":
            return self._finalize(filled=False, reason="canceled")

        # ``submitted`` / ``open`` / ``partially_filled``: in paper / shadow
        # we resolve synchronously; in live mode we rely on the connector.
        if venue_mode in ("paper", "shadow"):
            self._paper_resolve(order_id=order_id, candidate=candidate)
            return self._finalize(filled=True)

        # canary / live path
        if order.state == "created":
            self._submit_live(order_id=order_id, candidate=candidate, profile=profile)
            return False
        # Already submitted; poll once.
        polled = self._poll_live(order_id=order_id, profile=profile)
        if polled in ("filled", "rejected", "canceled", "expired", "failed"):
            return self._finalize(filled=(polled == "filled"), reason=polled)
        return False

    def on_cancel(self) -> None:
        tracker = self._tracker()
        try:
            profile = get_account_profile(self.paths, self.run.account_id)
        except Exception:
            profile = None
        registry = None
        conn = None
        # Resolve the connector once for live/canary modes so we can
        # actually cancel at the venue instead of only flipping local state.
        if profile is not None and profile.mode in ("live", "canary"):
            try:
                from ...connectors import ConnectorRegistry
                registry = ConnectorRegistry(workspace=self.paths.root)
                legacy_account = profile.to_connector_account()
                conn = registry.get(profile.id, legacy_account.connector_cfg())
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("cancel: connector unavailable for %s: %s", self.run.account_id, exc)
                conn = None

        for order_id in list(self.run.order_ids):
            order = tracker.get(order_id)
            if order is None or order.is_terminal:
                continue
            tracker.request_cancel(order_id)
            # Paper / shadow: nothing to cancel at a venue.
            if profile is None or profile.mode in ("paper", "shadow"):
                tracker.confirm_cancel(order_id)
                continue
            # Live / canary: hit the venue's cancel endpoint. If the
            # exchange already filled or rejected, the cancel fails and
            # we leave the order state honest (not "canceled").
            if conn is None:
                tracker.update_state(order_id, "failed", payload={"reason": "cancel_connector_unavailable"})
                continue
            try:
                conn.cancel_order(
                    market=order.market,
                    order_id=order.exchange_order_id or order.order_id,
                )
                tracker.confirm_cancel(order_id)
            except Exception as exc:
                log.warning("cancel: venue cancel failed for order %s: %s", order_id, exc)
                # Re-fetch to learn the true state — the order may have
                # filled between our request and the cancel attempt.
                try:
                    ack = conn.get_order(
                        market=order.market,
                        order_id=order.exchange_order_id or order.order_id,
                    )
                    status = (getattr(ack, "status", "") or "").lower()
                    if status in ("filled", "closed"):
                        tracker.update_state(order_id, "filled")
                    elif status in ("canceled", "cancelled"):
                        tracker.confirm_cancel(order_id)
                    else:
                        tracker.update_state(order_id, "failed", payload={"reason": f"cancel_failed:{exc}"})
                except Exception:
                    tracker.update_state(order_id, "failed", payload={"reason": f"cancel_failed:{exc}"})
        self._release_reservations()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _candidate(self) -> OrderCandidate:
        raw = (self.run.config_json or {}).get("candidate") or {}
        return _candidate_from_payload(raw)

    def _tracker(self) -> OrderTracker:
        return OrderTracker(self.paths)

    def _paper_resolve(self, *, order_id: str, candidate: OrderCandidate) -> None:
        """Apply a synchronous deterministic fill for paper/shadow."""
        tracker = self._tracker()
        snap = latest_snapshot(self.paths, self.run.account_id)
        # Decide reference price: candidate.price (limit) > meta.mark_price (market) > tracked order.price.
        order = tracker.get(order_id)
        meta_mark = float(candidate.meta.get("mark_price") or 0.0)
        ref_price = candidate.price or meta_mark or (order.price if order else None)
        if ref_price is None or float(ref_price or 0.0) <= 0:
            ref_price = float(order.avg_price) if (order and order.avg_price) else 0.0
        ref_price = float(ref_price or 0.0)
        if ref_price <= 0:
            tracker.mark_rejected(order_id, reason="no_mark_price")
            self.store_result({"reason": "no_mark_price"})
            return

        slip = ref_price * (_PAPER_SLIPPAGE_BPS / 10_000.0)
        fill_price = ref_price + slip if candidate.side == "buy" else ref_price - slip
        size_base = candidate.size_base or (
            candidate.notional_usd / fill_price if fill_price else 0.0
        )
        if size_base <= 0:
            tracker.mark_rejected(order_id, reason="zero_size")
            self.store_result({"reason": "zero_size"})
            return

        notional = float(size_base) * float(fill_price)
        fee_usd = notional * (_PAPER_FEE_BPS / 10_000.0)

        tracker.mark_submitted(order_id)
        fill = tracker.record_fill(
            order_id=order_id,
            price=fill_price,
            size_base=size_base,
            fee_usd=fee_usd,
            source="paper" if (snap is None or snap.source != "shadow") else "shadow",
        )

        # Update the position book.
        book = PositionBook(self.paths)
        book.apply_fill(
            account_id=candidate.account_id,
            strategy_id=candidate.strategy_id,
            market=candidate.market,
            side=candidate.side,
            price=fill_price,
            size_base=size_base,
            fee_usd=fee_usd,
            venue=_infer_venue(candidate.market),
            leverage=candidate.leverage,
            source="paper" if (snap is None or snap.source != "shadow") else "shadow",
            executor_id=self.run.executor_id,
            order_id=order_id,
            fill_id=fill.fill_id,
        )

        # Sync the legacy virtual ledger so existing dashboards keep working.
        try:
            from ..virtual_ledger import open_ledger
            profile = get_account_profile(self.paths, self.run.account_id)
            ledger = open_ledger(self.paths, profile.id, profile.initial_balance_usd)
            ledger.apply_fill(
                market=candidate.market,
                side=candidate.side,
                price=fill_price,
                size=size_base,
                fee_usd=fee_usd,
            )
        except Exception:  # pragma: no cover
            log.exception("legacy paper ledger sync failed")

        self.store_result({
            "fill_price": fill_price,
            "size_base": size_base,
            "fee_usd": fee_usd,
            "notional_usd": notional,
        })

    def _submit_live(
        self,
        *,
        order_id: str,
        candidate: OrderCandidate,
        profile,
    ) -> None:
        from ...connectors import ConnectorRegistry
        tracker = self._tracker()
        registry = ConnectorRegistry(workspace=self.paths.root)
        legacy_account = profile.to_connector_account()
        try:
            conn = registry.get(profile.id, legacy_account.connector_cfg())
        except Exception as exc:  # pragma: no cover
            tracker.mark_rejected(order_id, reason=f"connector_error:{exc}")
            self.transition("failed", close_type="failed")
            return

        size_base = candidate.size_base
        if size_base is None or size_base <= 0:
            tracker.mark_rejected(order_id, reason="zero_size")
            self.transition("failed", close_type="failed")
            return

        # Derive native SL/TP bracket levels from the protection plan so
        # the entry order carries the exchange-native stop orders. The
        # bracket ids come back on the ack (``attached_bracket_order_ids``)
        # and are recorded by ``_maybe_attach_protection`` for accounting.
        sl_price, tp_price = self._native_bracket_levels(candidate)

        try:
            ack = conn.place_order(
                market=candidate.market,
                side=candidate.side,
                order_type=candidate.order_type,
                size=float(size_base),
                price=candidate.price,
                client_order_id=candidate.client_order_id,
                time_in_force=candidate.time_in_force,
                reduce_only=candidate.reduce_only,
                leverage=candidate.leverage if candidate.leverage and candidate.leverage != 1.0 else None,
                stop_loss=sl_price,
                take_profit=tp_price,
                extra_params=self._connector_extra_params(),
            )
        except NotImplementedError as exc:
            tracker.mark_rejected(order_id, reason=f"unsupported:{exc}")
            self.transition("failed", close_type="failed")
            return
        except Exception as exc:
            tracker.mark_rejected(order_id, reason=f"place_error:{exc}")
            self.transition("failed", close_type="failed")
            return

        tracker.mark_submitted(order_id, exchange_order_id=getattr(ack, "order_id", None))
        # Record any exchange-native bracket order ids so the protection
        # executor / reconciliation can track them. Stored on the run's
        # result_json (persisted) and read by ``_maybe_attach_protection``.
        bracket = dict(getattr(ack, "attached_bracket_order_ids", {}) or {})
        if bracket:
            self.run.result_json["exchange_bracket_order_ids"] = bracket

        # Best-effort immediate fill detection from ack — ccxt-style
        # market orders sometimes return ``filled``+ ``avg_price`` in
        # the create-order response.
        filled = float(getattr(ack, "filled", None) or 0.0)
        avg_price = float(getattr(ack, "avg_price", None) or 0.0)
        if filled > 0 and avg_price > 0:
            fee_usd = float(getattr(ack, "fee_usd", None) or 0.0)
            fill = tracker.record_fill(
                order_id=order_id,
                price=avg_price,
                size_base=filled,
                fee_usd=fee_usd,
                source="live" if profile.mode in ("live", "canary") else "shadow",
            )
            # Atomic PositionBook update — keep the book in lock-step with
            # the broker so protection, exposure caps, and reconciliation
            # see the fill immediately rather than waiting for the
            # background poller side-channel.
            self._apply_fill_to_book(order_id=order_id, fill=fill, candidate=candidate, profile=profile)
            tracker.update_state(order_id, "filled")

        self.transition("submitted")

    def _poll_live(self, *, order_id: str, profile) -> str | None:
        from ...connectors import ConnectorRegistry
        tracker = self._tracker()
        order = tracker.get(order_id)
        if order is None:
            return None
        registry = ConnectorRegistry(workspace=self.paths.root)
        legacy_account = profile.to_connector_account()
        try:
            conn = registry.get(profile.id, legacy_account.connector_cfg())
            ack = conn.get_order(market=order.market, order_id=order.exchange_order_id or order.order_id)
        except NotImplementedError:
            # Connector cannot poll — assume the ack on submit was final.
            return order.state
        except Exception:
            tracker.mark_not_found(order_id)
            return None

        tracker.mark_seen(order_id)
        ack_filled = float(getattr(ack, "filled", None) or 0.0)
        ack_status = (getattr(ack, "status", None) or "").lower()
        if ack_filled > order.filled_size + 1e-12:
            extra = ack_filled - order.filled_size
            fee_usd = float(getattr(ack, "fee_usd", None) or 0.0)
            fill = tracker.record_fill(
                order_id=order_id,
                price=float(getattr(ack, "avg_price", None) or order.price or 0.0),
                size_base=extra,
                fee_usd=fee_usd,
                source="live" if profile.mode in ("live", "canary") else "shadow",
            )
            # Mirror the incremental fill into PositionBook atomically.
            self._apply_fill_to_book(order_id=order_id, fill=fill, candidate=self._candidate(), profile=profile)
        if ack_status in ("filled", "closed"):
            tracker.update_state(order_id, "filled")
            return "filled"
        if ack_status in ("canceled", "cancelled"):
            tracker.update_state(order_id, "canceled")
            return "canceled"
        if ack_status in ("rejected",):
            tracker.update_state(order_id, "rejected")
            return "rejected"
        if ack_status in ("expired",):
            tracker.update_state(order_id, "expired")
            return "expired"
        return None

    def _apply_fill_to_book(self, *, order_id: str, fill, candidate: OrderCandidate, profile) -> None:
        """Mirror a live fill into PositionBook atomically.

        Called from both ``_submit_live`` (immediate ack fill) and
        ``_poll_live`` (incremental late fill) so the book never lags the
        broker. ``PositionBook.apply_fill`` is idempotent on ``fill_id``,
        so a background poller observing the same fill cannot double-apply.
        """
        if fill is None:
            return
        try:
            book = PositionBook(self.paths)
            book.apply_fill(
                account_id=candidate.account_id,
                strategy_id=candidate.strategy_id,
                market=candidate.market,
                side=candidate.side,
                price=float(fill.price or getattr(fill, "price", 0.0) or 0.0),
                size_base=float(fill.size_base or getattr(fill, "size_base", 0.0) or 0.0),
                fee_usd=float(fill.fee_usd or getattr(fill, "fee_usd", 0.0) or 0.0),
                venue=_infer_venue(candidate.market),
                leverage=float(candidate.leverage or 1.0),
                source="live" if profile.mode in ("live", "canary") else "shadow",
                executor_id=self.run.executor_id,
                order_id=order_id,
                fill_id=getattr(fill, "fill_id", None),
            )
        except Exception:
            # Must never break the trading path — reconciliation will
            # surface the drift. The tracker already has the fill.
            log.exception("live apply_fill_to_book failed for order %s", order_id)

    def _native_bracket_levels(self, candidate: OrderCandidate) -> tuple[float | None, float | None]:
        """Derive absolute SL/TP prices from the plan's protection rule.

        The connector forwards these to the venue as native
        ``stopLossPrice`` / ``takeProfitPrice`` so the bracket rests on
        the exchange — surviving a process crash. Only ``price``-type
        levels translate directly; ``pct`` levels are left to the soft
        protection executor (the fallback). Returns ``(sl, tp)`` absolutes.
        """
        plan_protection = (self.run.config_json or {}).get("protection")
        if not isinstance(plan_protection, dict):
            return None, None
        ref = candidate.price or float((candidate.meta or {}).get("mark_price") or 0.0)
        sl_price: float | None = None
        tp_price: float | None = None
        sl = plan_protection.get("stop_loss")
        if isinstance(sl, dict):
            if str(sl.get("type")) == "price":
                sl_price = float(sl.get("value") or 0.0) or None
            elif str(sl.get("type")) == "pct" and ref > 0:
                pct = float(sl.get("value") or 0.0)
                # Long stops below entry, short stops above.
                if candidate.side == "buy":
                    sl_price = ref * (1.0 - pct) if 0 < pct < 1 else None
                else:
                    sl_price = ref * (1.0 + pct) if 0 < pct < 1 else None
        tp = plan_protection.get("take_profit")
        if isinstance(tp, dict):
            if str(tp.get("type")) == "price":
                tp_price = float(tp.get("value") or 0.0) or None
            elif str(tp.get("type")) == "pct" and ref > 0:
                pct = float(tp.get("value") or 0.0)
                if candidate.side == "buy":
                    tp_price = ref * (1.0 + pct) if pct > 0 else None
                else:
                    tp_price = ref * (1.0 - pct) if pct > 0 else None
        return sl_price, tp_price

    def _connector_extra_params(self) -> dict[str, Any] | None:
        """Venue-specific extra params threaded from the plan meta.

        Strategies can set ``meta.connector_params`` (e.g.
        ``{"positionIdx": 1}`` for Bybit V5 hedge mode) to pass through
        arbitrary one-way fields the connector doesn't model explicitly.
        """
        candidate = self._candidate()
        params = dict((candidate.meta or {}).get("connector_params") or {})
        return params or None

    def _finalize(self, *, filled: bool, reason: str | None = None) -> bool:
        store = CapitalReservationStore(self.paths)
        if filled:
            for rid in self.run.reservation_ids:
                store.consume(rid)
            self._maybe_attach_protection()
            self.transition("done", close_type="filled")
        else:
            for rid in self.run.reservation_ids:
                store.release(rid)
            self.store_result({"reason": reason or "not_filled"})
            self.transition("failed", close_type="failed")
        return True

    def _release_reservations(self) -> None:
        store = CapitalReservationStore(self.paths)
        for rid in self.run.reservation_ids:
            store.release(rid)

    def _maybe_attach_protection(self) -> None:
        """If the candidate carried a protection plan, register it
        against the freshly-opened position and spin up a protection
        executor."""
        plan_protection = (self.run.config_json or {}).get("protection")
        if not plan_protection:
            return
        candidate = self._candidate()
        book = PositionBook(self.paths)
        position = book.get_open(
            account_id=candidate.account_id,
            strategy_id=candidate.strategy_id,
            market=candidate.market,
        )
        if position is None:
            return

        from ..order_intents import (
            PartialExitSpec,
            ProtectionRule,
            StopLossSpec,
            TakeProfitSpec,
            TrailingStopSpec,
        )

        sl = plan_protection.get("stop_loss") if isinstance(plan_protection, dict) else None
        tp = plan_protection.get("take_profit") if isinstance(plan_protection, dict) else None
        trail = plan_protection.get("trailing_stop") if isinstance(plan_protection, dict) else None
        partials = plan_protection.get("partial_exits") or []
        # If the entry order placed native exchange brackets, the rule
        # is primarily exchange-armed (the venue enforces it even if we
        # crash). Otherwise soft_runtime — the protection executor
        # evaluates locally on each tick.
        exchange_brackets = dict((self.run.result_json or {}).get("exchange_bracket_order_ids") or {})
        has_native_bracket = bool(exchange_brackets)
        declared_mode = str(plan_protection.get("mode") or "soft_runtime")
        # Promote soft_runtime to exchange_armed when the venue actually
        # returned bracket ids; keep explicit hybrid/hard as declared.
        if has_native_bracket and declared_mode == "soft_runtime":
            declared_mode = "exchange_armed"
        rule = ProtectionRule(
            position_id=position.position_id,
            executor_id=self.run.executor_id,
            strategy_id=candidate.strategy_id,
            account_id=candidate.account_id,
            market=candidate.market,
            side=position.side,
            mode=declared_mode,  # type: ignore[arg-type]
            stop_loss=StopLossSpec(**sl) if isinstance(sl, dict) else None,
            take_profit=TakeProfitSpec(**tp) if isinstance(tp, dict) else None,
            time_limit_sec=plan_protection.get("time_limit_sec"),
            trailing_stop=TrailingStopSpec(**trail) if isinstance(trail, dict) else None,
            partial_exits=[PartialExitSpec(**p) for p in partials if isinstance(p, dict)],
            trigger_source=str(plan_protection.get("trigger_source") or "mark"),  # type: ignore[arg-type]
            status="armed",
            notes=str(plan_protection.get("notes") or ""),
        )
        store = ProtectionStore(self.paths)
        store.upsert(rule)
        if has_native_bracket:
            store.attach_exchange_orders(rule.protection_id, exchange_brackets)
            rule.status = "exchange_armed"
        book.attach_protection(position.position_id, rule.protection_id)
        # Spin up a long-lived protection executor so the orchestrator
        # can restart-recover it. The executor monitors the position and
        # handles the soft-fallback path even when the venue has native
        # brackets (hybrid safety).
        try:
            from .orchestrator import ExecutorOrchestrator
            from ...core.config import load_config
            orch = ExecutorOrchestrator(load_config(self.paths.root))
            orch.create_position_protection(rule=rule, position_id=position.position_id)
            orch.close()
        except Exception:
            log.exception("could not persist protection executor for position %s", position.position_id)
        self.store_result({
            "protection_id": rule.protection_id,
            "position_id": position.position_id,
            "exchange_bracket_order_ids": exchange_brackets,
        })


def _candidate_from_payload(payload: dict[str, Any]) -> OrderCandidate:
    return OrderCandidate(
        account_id=str(payload.get("account_id") or ""),
        strategy_id=str(payload.get("strategy_id") or ""),
        market=str(payload.get("market") or ""),
        side=str(payload.get("side") or "buy"),  # type: ignore[arg-type]
        order_type=str(payload.get("order_type") or "market"),  # type: ignore[arg-type]
        size_base=(float(payload["size_base"]) if payload.get("size_base") is not None else None),
        notional_usd=float(payload.get("notional_usd") or 0.0),
        price=(float(payload["price"]) if payload.get("price") is not None else None),
        leverage=float(payload.get("leverage") or 1.0),
        reduce_only=bool(payload.get("reduce_only") or False),
        time_in_force=str(payload.get("time_in_force") or "gtc"),  # type: ignore[arg-type]
        estimated_fee_usd=float(payload.get("estimated_fee_usd") or 0.0),
        estimated_slippage_bps=float(payload.get("estimated_slippage_bps") or 0.0),
        required_collateral=dict(payload.get("required_collateral") or {}),
        expected_returns=dict(payload.get("expected_returns") or {}),
        resized=bool(payload.get("resized") or False),
        resize_reason=payload.get("resize_reason"),
        rejection_reason=payload.get("rejection_reason"),
        intent_id=str(payload.get("intent_id") or ""),
        plan_id=str(payload.get("plan_id") or ""),
        risk_evaluation_id=str(payload.get("risk_evaluation_id") or ""),
        reservation_id=str(payload.get("reservation_id") or ""),
        executor_id=str(payload.get("executor_id") or ""),
        client_order_id=str(payload.get("client_order_id") or ""),
        meta=dict(payload.get("meta") or {}),
    )


def _infer_venue(market: str) -> str:
    if ":" in market:
        return market.split(":", 1)[0].upper()
    return ""


__all__ = ["MarketOrderConfig", "MarketOrderExecutor"]
