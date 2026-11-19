"""Outcome-share semantics for the common plan submission pipeline."""
from decimal import Decimal, ROUND_FLOOR

from ...core.errors import IntentValidationError


def validate(plan):
    if plan.side != 'long' or plan.entry.order_type not in ('market', 'limit'):
        raise IntentValidationError('prediction outcome shares support long market/limit entry and owned-share exits; native short/TP/SL is unsupported')
    if plan.protection is not None and plan.protection.mode == 'hard_exchange':
        raise IntentValidationError('prediction protection requires a local executor; native protection is unsupported')


def execution_snapshot(plan, snapshot):
    if not isinstance(snapshot, dict):
        return snapshot
    result = dict(snapshot)
    executable = snapshot.get('ask' if plan.buy_or_sell == 'buy' else 'bid')
    if executable is not None:
        result['price'] = executable
    if plan.entry.order_type == 'limit' and plan.entry.limit_price:
        result['price'] = plan.entry.limit_price
    return result


def budget_mark(plan, profile, price):
    if price and plan.buy_or_sell == 'buy' and plan.entry.order_type == 'market':
        cap = int((profile.raw.get('provider_config') or {}).get('max_slippage_bps', 50))
        return float(price) * (1 + cap / 10000)
    return price


def size_candidate(candidate, snapshot, mark_price):
    if candidate.size_base is None:
        return None
    original = Decimal(str(candidate.size_base))
    shares = original.quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    if shares <= 0:
        return 'prediction_share_size_below_precision'
    candidate.size_base = float(shares)
    candidate.notional_usd *= float(shares / original)
    candidate.meta['mark_price'] = float((snapshot or {}).get('price') or mark_price or 0)
    return None
