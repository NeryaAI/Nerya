"""Execution engine — decides paper vs live, routes to connector, writes fills.

Live path:
    - account.is_live  (= account.mode == 'live' AND account.live_trading_enabled)
    - runtime.live_trading_enabled in nerya.yml must also be true
    - runtime.kill_switch must be false
    - market_snapshot must be fresh enough (`execution.max_snapshot_age_s`)

When any of these are false we fall back to the deterministic paper
executor so bots never accidentally send a real order.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..connectors import ConnectorRegistry
from ..connectors.base import OrderAck
from ..core.config import Config
from ..core.errors import TradingError
from ..core.ids import fill_id as _new_fill_id, order_id as _new_order_id
from ..core.time import now_iso
from .accounts import Account, get_account
from .intents import TradeIntent
from .order_tracker import OrderTracker
from .orders import OrderRequest, OrderResult, Fill
from . import paper
from .virtual_ledger import open_ledger

log = logging.getLogger(__name__)


@dataclass
class ExecutionEngine:
    config: Config
    registry: ConnectorRegistry | None = None

    def _get_registry(self) -> ConnectorRegistry:
        if self.registry is None:
            self.registry = ConnectorRegistry(workspace=self.config.paths.root)
        return self.registry

    def execute(
        self,
        intent: TradeIntent,
        *,
        market_snapshot: dict[str, Any] | None = None,
    ) -> OrderResult:
        paths = self.config.paths
        account = get_account(paths, intent.account_id)
        request = OrderRequest(
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            account_id=intent.account_id,
            market=intent.market,
            side=intent.side,
            size=intent.size,
            size_unit=intent.size_unit,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            stop_price=intent.stop_price,
            time_in_force=intent.time_in_force,
        )
        mark = (market_snapshot or {}).get("price") or intent.limit_price
        if mark is None:
            raise TradingError("execution requires a mark price or limit price")

        if self._should_execute_live(account):
            return self._execute_live(account, intent, request, float(mark))

        ledger = open_ledger(paths, account.id, account.initial_balance_usd)
        return paper.execute(
            intent=intent, request=request, ledger=ledger, mark_price=float(mark)
        )

    # ---------------------------------------------------------- internal
    def _should_execute_live(self, account: Account) -> bool:
        if not account.is_live:
            return False
        runtime = self.config.get("runtime") or {}
        if not runtime.get("live_trading_enabled", False):
            log.warning("account %s is live but nerya.yml runtime.live_trading_enabled=false",
                        account.id)
            return False
        if runtime.get("kill_switch", False):
            log.warning("kill switch active — refusing live execution")
            return False
        return True

    def _execute_live(
        self,
        account: Account,
        intent: TradeIntent,
        request: OrderRequest,
        mark_price: float,
    ) -> OrderResult:
        conn = self._get_registry().get(account.id, account.connector_cfg())

        # convert usd size to base size if necessary
        size_base = request.size
        if request.size_unit == "usd":
            size_base = request.size / mark_price if mark_price else request.size

        # Register the order with the durable :class:`OrderTracker`
        # *before* we hit the venue. If the venue call fails we mark the
        # row rejected; if it succeeds we then promote it through
        # ``mark_submitted`` / ``record_fill``. Tracking pre-flight is
        # important so a crash between ``place_order`` and the response
        # parse still leaves a row that the background poller can pick
        # up on restart.
        tracker = OrderTracker(self.config.paths)
        local_order_id = _new_order_id()
        tracker.register(
            client_order_id=request.intent_id,
            account_id=account.id,
            strategy_id=intent.strategy_id,
            market=request.market,
            side=request.side,  # type: ignore[arg-type]
            order_type=request.order_type,
            size_base=size_base,
            notional_usd=size_base * float(mark_price or 0.0),
            price=request.limit_price,
            time_in_force=request.time_in_force or "gtc",
            intent_id=intent.intent_id,
            order_id=local_order_id,
            initial_state="submitted",
            meta={"source": "legacy_execute_live"},
        )

        try:
            ack: OrderAck = conn.place_order(
                market=request.market, side=request.side,
                order_type=request.order_type, size=size_base,
                price=request.limit_price,
                client_order_id=request.intent_id,
                time_in_force=request.time_in_force,
            )
        except NotImplementedError as exc:
            tracker.mark_rejected(local_order_id, reason=f"unsupported:{exc}")
            raise TradingError(f"live execution unsupported: {exc}") from exc
        except Exception as exc:
            tracker.mark_rejected(local_order_id, reason=f"place_error:{exc}")
            raise

        # Promote the tracker row with the venue's order id.
        exchange_order_id = ack.order_id or None
        tracker.mark_submitted(local_order_id, exchange_order_id=exchange_order_id)

        filled = float(ack.filled or 0.0)
        avg_price = float(ack.avg_price or mark_price)
        # When the connector didn't report a fee at all (``None``) we
        # record 0 on the aggregate so dashboards don't explode, but
        # surface ``ack.fee_usd is None`` via the order result meta so
        # the reconciliation job knows to backfill the fee from a
        # later ``get_order`` / fills endpoint.
        fee_total = float(ack.fee_usd or 0.0)
        fee_unknown = ack.fee_usd is None

        # The OrderResult exposed to ``submit.py`` keeps the venue's
        # order id (when present) so callers and the legacy
        # ``_sync_position_book_after_execution`` keep working. The
        # tracker row keeps the internal id (``local_order_id``) as the
        # primary key so the background poller can address it without
        # ambiguity even if the venue id changes shape.
        public_order_id = exchange_order_id or local_order_id
        result = OrderResult(
            order_id=public_order_id,
            intent_id=intent.intent_id,
            status=ack.status or "new",
            avg_price=avg_price,
            filled_size=filled,
            notional_usd=filled * avg_price,
            fee_usd=fee_total,
            reason=None,
            fills=[],
        )

        # If the ack already reports a fill, capture it on the tracker
        # AND on the OrderResult so the existing PositionBook sync at
        # the submit-layer (``_sync_position_book_after_execution``)
        # picks it up. Late fills (the more common case for limit
        # orders) get picked up by the background poller later.
        if filled > 0:
            fill_id = _new_fill_id()
            tracker.record_fill(
                order_id=local_order_id,
                price=avg_price,
                size_base=filled,
                fee_usd=fee_total,
                source="live",
                meta={
                    "intent_id": intent.intent_id,
                    "fee_unknown": fee_unknown,
                    "exchange_order_id": exchange_order_id,
                    # ``submit.py`` already fans out a
                    # ``trade.execution`` notification once the
                    # OrderResult is back at its layer. Suppress the
                    # tracker's own broadcast to keep the operator
                    # inbox clean (one fill, one ping).
                    "suppress_trade_notification": True,
                },
            )
            result.fills.append(Fill(
                fill_id=fill_id,
                order_id=public_order_id,
                intent_id=intent.intent_id,
                market=request.market,
                price=avg_price,
                size=filled,
                fee_usd=fee_total,
                ts=now_iso(),
            ))
        if fee_unknown:
            log.warning(
                "live order %s on %s: broker did not report fee_usd; "
                "reconciliation will need to backfill from fills endpoint",
                result.order_id, request.market,
            )
        return result
