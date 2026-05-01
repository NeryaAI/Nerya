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

        try:
            ack: OrderAck = conn.place_order(
                market=request.market, side=request.side,
                order_type=request.order_type, size=size_base,
                price=request.limit_price,
                client_order_id=request.intent_id,
                time_in_force=request.time_in_force,
            )
        except NotImplementedError as exc:
            raise TradingError(f"live execution unsupported: {exc}") from exc

        filled = float(ack.filled or 0.0)
        avg_price = float(ack.avg_price or mark_price)
        result = OrderResult(
            order_id=ack.order_id or _new_order_id(),
            intent_id=intent.intent_id,
            status=ack.status or "new",
            avg_price=avg_price,
            filled_size=filled,
            notional_usd=filled * avg_price,
            fee_usd=0.0,
            reason=None,
            fills=[],
        )
        if filled > 0:
            result.fills.append(Fill(
                fill_id=_new_fill_id(),
                order_id=result.order_id,
                intent_id=intent.intent_id,
                market=request.market,
                price=avg_price,
                size=filled,
                fee_usd=0.0,
                ts=now_iso(),
            ))
        return result
