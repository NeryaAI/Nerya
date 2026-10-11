"""Strategy-owned spot swaps routed through wallet approval and receipt accounting."""
from __future__ import annotations

from contextlib import closing
import math
from .errors import WalletPolicyDenied

_STABLES = {
    'solana':'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
    'bsc':'0x55d398326f99059fF775485246999027B3197955',
    'base':'0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
    'ethereum':'0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
}


def is_wallet_account(config, account_id):
    from ..trading.accounts import get_account_profile
    from ..core.errors import TradingError
    try:
        profile = get_account_profile(config.paths,account_id)
    except TradingError:
        return False
    return profile.kind in ('chain','dex') and bool(profile.wallet_id)


def _price(config, market):
    from ..data.candles import fetch_candles
    if market.split(':',1)[0].upper() in {'BYREAL_ONCHAIN','BYREAL','BYREAL_CLI','BYREAL_SOLANA'}:
        parts=market.split(':',2)
        if len(parts)!=3 or '@' not in parts[2]:
            raise WalletPolicyDenied('Byreal USD pricing requires pool@token_mint')
        market='ONCHAIN:'+parts[1]+':'+parts[2].split('@',1)[1]
    rows=fetch_candles(market,interval='1m',count=1,config_like=config,allow_mock=False)
    if not rows or (rows[-1].get('_envelope') or {}).get('mode')!='live':
        raise WalletPolicyDenied('wallet strategy requires live token USD price evidence')
    import time
    row=rows[-1]; price=float(row.get('close') or 0)
    age=max(0,time.time()-int(row.get('ts') or 0))
    if price<=0 or not math.isfinite(price) or age>120 or row.get('price_currency','USD')!='USD':
        raise WalletPolicyDenied('wallet strategy token price is missing or stale')
    return {'price':price,'age_s':age,'_envelope':row['_envelope']}


def submit_plan(config, plan, *, quote_only=False):
    from ..trading.accounts import get_account_profile
    from ..trading.submit import _plan_to_intent, _resolve_position_sized_plan
    from ..trading.risk import RiskGate
    from .bindings import resolve_binding
    from .swap_approval import prepare_swap, request_approval
    profile=get_account_profile(config.paths,plan.account_id)
    if plan.entry.order_type!='market':
        raise WalletPolicyDenied('on-chain swaps support market execution; use strategy triggers for conditional entry')
    if plan.side=='short' and plan.action=='open_position':
        raise WalletPolicyDenied('wallet spot swaps cannot open short positions')
    if plan.protection is not None:
        raise WalletPolicyDenied('on-chain TP/SL must be scheduled as explicit strategy exit swaps; no exchange bracket exists')
    parts=plan.market.split(':',2)
    if len(parts)!=3:
        raise WalletPolicyDenied('on-chain strategy market must be VENUE:chain:token_mint')
    _,chain,token=parts
    if '@' in token:
        _,token=token.split('@',1)  # explicit pool@mint market
    elif parts[0].upper() in {'BYREAL_ONCHAIN','BYREAL','BYREAL_CLI','BYREAL_SOLANA'}:
        raise WalletPolicyDenied('Byreal pool market requires pool@token_mint for execution')
    _,_,wallet_cfg=resolve_binding(config,{'account_id':plan.account_id,'strategy_id':plan.strategy_id})
    funding=(wallet_cfg.get('funding_tokens') or {}).get(chain)
    if funding:
        if not isinstance(funding,dict) or funding.get('symbol') not in {'USDC','USDT','DAI','FDUSD','BUSD','TUSD'} or not funding.get('token'):
            raise WalletPolicyDenied('funding_tokens requires a USD stablecoin symbol and full token address')
        funding_token=funding['token']
    else:funding_token=_STABLES.get(chain)
    if not funding_token:raise WalletPolicyDenied('configure funding_tokens for this chain')
    plan=_resolve_position_sized_plan(config.paths,plan)
    snapshot=_price(config,plan.market)
    from ..trading.strategies import load_strategy
    strategy=load_strategy(config.paths,plan.strategy_id)
    if plan.market not in strategy.markets:
        raise WalletPolicyDenied('swap market is outside the reviewed strategy markets')
    slippage=int((plan.meta or {}).get('slippage_bps',50))
    if slippage<0 or slippage>strategy.limits.max_slippage_bps:
        raise WalletPolicyDenied('swap slippage exceeds the strategy limit')
    price=snapshot['price'];side=plan.buy_or_sell
    if plan.sizing.method=='fixed_usd':
        notional=float(plan.sizing.fixed_usd or 0)
        token_size=notional/price
    elif plan.sizing.method=='fixed_base':
        token_size=float(plan.sizing.fixed_base or 0);notional=token_size*price
    else:
        raise WalletPolicyDenied('wallet strategy requires resolved fixed_usd/fixed_base sizing')
    if side=='sell':
        from ..trading.position_book import PositionBook
        with closing(PositionBook(config.paths)) as book:
            share=book.get_share(account_id=plan.account_id,strategy_id=plan.strategy_id,market=plan.market)
        if not share or token_size>abs(share.size_share_base)+1e-12:
            raise WalletPolicyDenied('swap sell exceeds the strategy-owned token position')
    intent=_plan_to_intent(plan)
    risk=RiskGate(config).evaluate(intent,market_snapshot=snapshot,wallet_swap=True,preview=quote_only)
    if risk.decision=='reject':
        return {'status':'rejected','risk_decision':risk.asdict(),'intent':intent.asdict()}
    if profile.mode=='shadow':
        return {'status':'shadow','risk_decision':risk.asdict(),'intent':intent.asdict()}
    wid,provider,_=resolve_binding(config,{'account_id':plan.account_id})
    request={
        'provider':provider,'wallet_id':wid,'account_id':plan.account_id,'strategy_id':plan.strategy_id,
        'market':plan.market,'side':side,'intent_id':intent.intent_id,'confidence':plan.confidence,
        'source':plan.source,'plan_action':plan.action,'chain':chain,'token_in':funding_token if side=='buy' else token,
        'token_out':token if side=='buy' else funding_token,
        'amount_in':notional if side=='buy' else token_size,
        'slippage_bps':slippage,
    }
    request,quote=prepare_swap(config,request)
    if quote_only:
        return {"status":"quoted","wallet_request":request,"wallet_quote":quote,"plan":plan.asdict(),
                "risk_decision":risk.asdict(),"quantity_base":token_size,"notional_usd":notional,"market_snapshot":snapshot}
    if profile.mode=='paper':
        result={'provider':provider,'chain':chain,'ok':True,'amount_in':request['amount_in'],
                'amount_out':quote['expected_out'],'tx_hash':'','extra':{'confirmed':True,'amount_out_source':'paper_quote'}}
        record_fill(config,request,result,'paper_'+intent.intent_id,source='paper')
        return {'status':'filled','execution_mode':'paper','quote':quote,'result':result,'risk_decision':risk.asdict()}
    from ..trading.order_tracker import OrderTracker
    with closing(OrderTracker(config.paths)) as tracker:
        pending=tracker.active_orders(account_id=plan.account_id)
        if any(order.strategy_id==plan.strategy_id and order.market==plan.market for order in pending):
            raise WalletPolicyDenied('strategy already has an unresolved order for this token')
    if not profile.live_trading_enabled or not profile.can_place_order:
        raise WalletPolicyDenied('wallet account is not enabled for live orders')
    response=request_approval(config,request=request,quote=quote,actor_id='strategy:'+plan.strategy_id)
    response['risk_decision']=risk.asdict()
    return response


def recheck_strategy(config, request):
    if not request.get('strategy_id'):
        return
    from ..trading.risk import RiskGate
    from ..trading.intents import TradeIntent
    from ..trading.accounts import get_account_profile
    profile=get_account_profile(config.paths,request['account_id'])
    if not profile.is_real_money or not profile.live_trading_enabled or not profile.can_place_order:
        raise WalletPolicyDenied('wallet account live permission changed')
    from ..trading.strategies import load_strategy
    strategy=load_strategy(config.paths,request['strategy_id'])
    if request['market'] not in strategy.markets or int(request['slippage_bps'])>strategy.limits.max_slippage_bps:
        raise WalletPolicyDenied('strategy markets/slippage policy changed after approval')
    if request['side']=='sell':
        from ..trading.position_book import PositionBook
        with closing(PositionBook(config.paths)) as book:
            share=book.get_share(account_id=request['account_id'],strategy_id=request['strategy_id'],market=request['market'])
        if not share or float(request['amount_in'])>abs(share.size_share_base)+1e-12:
            raise WalletPolicyDenied('approved sell now exceeds strategy token balance')
    price=_price(config,request['market'])
    intent=TradeIntent.new(strategy_id=request['strategy_id'],account_id=request['account_id'],
        market=request['market'],side=request['side'],size=request['amount_in'],
        size_unit='usd' if request['side']=='buy' else 'base',order_type='market',
        confidence=float(request.get('confidence') or 0),source=request.get('source') or 'strategy_runtime',
        meta={'plan_action':request.get('plan_action') or 'open_position'})
    risk=RiskGate(config).evaluate(intent,market_snapshot=price,resume=True,wallet_swap=True)
    if risk.decision=='reject':
        raise WalletPolicyDenied('wallet strategy risk rejected: '+','.join(risk.reasons))


def record_fill(config, request, result, execution_id, *, source='live'):
    from ..trading.locks import trading_lock
    with trading_lock(config.paths,'wallet_book:'+execution_id) as acquired:
        if not acquired:
            raise WalletPolicyDenied('wallet receipt accounting in progress')
        return _record_fill_locked(config,request,result,execution_id,source=source)


def _record_fill_locked(config,request,result,execution_id,*,source):
    if not request.get('strategy_id'):
        return
    from ..trading.position_book import PositionBook
    from ..trading.order_tracker import OrderTracker
    side=request['side'];size=float(result['amount_out'] if side=='buy' else request['amount_in'])
    dollars=float(request['amount_in'] if side=='buy' else result['amount_out'])
    if size<=0 or dollars<=0:
        raise WalletPolicyDenied('cannot book a swap without observed execution quantities')
    from .fees import price_network_fee
    extra = result.get('extra') or {}
    costs = {'status': 'paper', 'fee_usd': 0.0} if source == 'paper' else price_network_fee(extra.get('network_fee'))
    # Reuse the persisted valuation on recovery rather than repricing a historical
    # gas cost or counting a net-of-router-fees swap output twice.
    fee_usd = float(costs.get('fee_usd', 0.0))
    with closing(OrderTracker(config.paths)) as tracker:
        cid='wallet_'+execution_id
        order=tracker.get_by_client_order_id(cid)
        if order is None:
            order=tracker.register(client_order_id=cid,account_id=request['account_id'],strategy_id=request['strategy_id'],
                market=request['market'],side=side,order_type='market',size_base=size,notional_usd=dollars,
                intent_id=request.get('intent_id'),meta={'wallet_swap':True,'tx_hash':result.get('tx_hash'),
                    'fee_status':costs['status'], 'cost_evidence':costs,
                    'amount_out_source':(result.get('extra') or {}).get('amount_out_source')})
        tracker.mark_submitted(order.order_id,exchange_order_id=result.get('tx_hash') or None)
        if (order.meta or {}).get('cost_evidence'):
            costs = order.meta['cost_evidence']
            fee_usd = float(costs.get('fee_usd', 0.0))
        fill=tracker.record_fill(order_id=order.order_id,price=dollars/size,size_base=size,fee_usd=fee_usd,source=source,cumulative_filled=size,
            meta={'fee_status':costs['status'], 'cost_evidence':costs,
                  'amount_out_source':(result.get('extra') or {}).get('amount_out_source')})
        if fill is None:
            fill=next((f for f in tracker.fills_for_order(order.order_id) if abs(f.size_base-size)<1e-12),None)
        if fill is not None:
            with closing(PositionBook(config.paths)) as book:
                book.apply_fill(account_id=request['account_id'],strategy_id=request['strategy_id'],market=request['market'],
                    side=side,price=dollars/size,size_base=size,fee_usd=fill.fee_usd,source=source,order_id=order.order_id,fill_id=fill.fill_id)
        if source=='paper':
            from ..trading.virtual_ledger import open_ledger
            from ..trading.accounts import get_account_profile
            profile=get_account_profile(config.paths,request['account_id'])
            # paper fill submission is synchronous and uses an intent-specific id
            if not order.filled_size:
                open_ledger(config.paths,profile.id,profile.initial_balance_usd).apply_fill(
                    market=request['market'],side=side,price=dollars/size,size=size,fee_usd=0)
        tracker.update_state(order.order_id,'filled')
