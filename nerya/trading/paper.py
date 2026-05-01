"""Deterministic paper executor. Applies a small slippage and fee, writes a fill."""

from __future__ import annotations

from typing import Any

from .intents import TradeIntent
from .orders import OrderRequest, OrderResult, new_fill
from .virtual_ledger import VirtualLedger


PAPER_FEE_BPS = 5   # 5bps / 0.05%
PAPER_SLIPPAGE_BPS = 2


def execute(
    *,
    intent: TradeIntent,
    request: OrderRequest,
    ledger: VirtualLedger,
    mark_price: float,
) -> OrderResult:
    # apply slippage
    slip = mark_price * (PAPER_SLIPPAGE_BPS / 10000.0)
    fill_price = mark_price + slip if intent.side == "buy" else mark_price - slip

    # convert size to base if needed
    if intent.size_unit == "usd":
        base_size = float(intent.size) / float(fill_price)
    elif intent.size_unit == "quote":
        base_size = float(intent.size) / float(fill_price)
    else:
        base_size = float(intent.size)

    notional = base_size * fill_price
    fee_usd = notional * (PAPER_FEE_BPS / 10000.0)

    ledger.apply_fill(
        market=intent.market,
        side=intent.side,
        price=fill_price,
        size=base_size,
        fee_usd=fee_usd,
    )

    fill = new_fill(
        order_id=request.order_id,
        intent_id=intent.intent_id,
        market=intent.market,
        price=fill_price,
        size=base_size,
        fee_usd=fee_usd,
    )
    return OrderResult(
        order_id=request.order_id,
        intent_id=intent.intent_id,
        status="filled",
        fills=[fill],
        avg_price=fill_price,
        filled_size=base_size,
        notional_usd=notional,
        fee_usd=fee_usd,
        reason="paper_executed",
    )
