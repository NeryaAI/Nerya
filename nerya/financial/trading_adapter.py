"""Finite funds authorization delegates execution to the existing trading gates."""
import time
from decimal import Decimal

from .contracts import FinancialError,amount


class TradingFunds:
    def __init__(self,config,request):self.config=config

    def quote(self,request):
        if request["kind"]=="swap":
            from ..wallet.swap_approval import prepare_swap
            payload={"wallet_id":request["wallet_id"],"chain":request["chain"],"token_in":request["asset"],
                     "token_out":request["to_asset"],"amount_in":request["amount"],"slippage_bps":request["slippage_bps"],"receiver":request.get("recipient")}
            frozen,quote=prepare_swap(self.config,payload)
            from .adapters import owned_wallet,usd_price
            _,_,raw,provider=owned_wallet(self.config,request)
            symbol=(raw.get("token_symbols") or {}).get(request["asset"])
            if not symbol:raise FinancialError("asset_valuation_symbol_unconfigured",422)
            value=amount(request["amount"])*usd_price(symbol)
            balance=provider.get_balance(chain=request["chain"],address=str(raw.get("address") or ""),token=request["asset"])
            assets={request['asset']:request['amount']}
            available={request['asset']:str(balance.amount)}
            gas_budget=(quote.get('extra') or {}).get('gas_budget') or {}
            if gas_budget:
                # Reserve network fees plus recoverable rent. Do not double
                # count a SOL input under a second alias for the same balance.
                key=request['asset'] if gas_budget.get('native_input') else 'NATIVE'
                gas_balance=provider.get_balance(chain=request['chain'],address=str(raw.get('address') or ''),token='NATIVE')
                assets[key]=str(amount(assets.get(key,0),zero=True)+amount(gas_budget['amount']))
                available[key]=str(gas_balance.amount)
            return {"risk_usd":str(value),"spend_usd":str(value),"fee_usd":str(amount(quote.get("gas_cost_usd",0),zero=True)),
                "asset_amounts":assets,"available_asset_amounts":available,
                "wallet_request":frozen,"wallet_quote":quote,"expires_at":time.time()+45}
        from ..sdk.trading_api import _plan_from_dict
        from ..trading.submit import _resolve_market_snapshot,_plan_to_intent,_resolve_position_sized_plan
        from ..trading.accounts import get_account_profile
        from ..trading.account_snapshots import fresh_snapshot
        from ..trading.capital import BudgetChecker,CapitalReservationStore
        raw=request.get("plan") or {}
        plan=_resolve_position_sized_plan(self.config.paths,_plan_from_dict(raw));plan.meta=dict(raw.get("meta") or {})
        if plan.account_id!=request["account_id"] or plan.market!=request["market"]:raise FinancialError("trade_plan_resource_mismatch",400)
        profile=get_account_profile(self.config.paths,plan.account_id)
        from ..trading.accounts import account_revision
        from ..connectors.provider_spec import get_registry
        provider=get_registry(self.config.paths.root).find(profile.venue)
        if provider is None:raise FinancialError("financial_provider_unavailable",422)
        market=_resolve_market_snapshot(self.config,_plan_to_intent(plan),supplied=None)
        snapshot=fresh_snapshot(self.config,plan.account_id)
        store=CapitalReservationStore(self.config.paths)
        decision=BudgetChecker(profile=profile,snapshot=snapshot,store=store).evaluate(plan_strategy_id=plan.strategy_id,
            market=plan.market,side=plan.buy_or_sell,sizing=plan.sizing,mark_price=market["price"],
            order_price=plan.entry.limit_price,stop_price=plan.entry.stop_price,order_type=plan.entry.order_type,
            reduce_only=plan.action in {"close_position","reduce_position"},leverage=float(plan.meta.get("leverage",1)),
            time_in_force=plan.entry.time_in_force,intent_id=plan.intent_id,plan_id=plan.plan_id)
        if decision.verdict not in {"allow","resize"} or decision.candidate is None:raise FinancialError("trade_quote_budget_not_allowed",422)
        candidate=decision.candidate
        candidate.meta={**dict(candidate.meta or {}),**dict(plan.meta or {})}
        collateral={k:str(amount(v,zero=True)) for k,v in (candidate.required_collateral or {}).items()}
        from ..trading.capital import free_usd_for_account
        spend=Decimal(0) if candidate.reduce_only else Decimal(str((candidate.required_collateral or {}).get(profile.base_currency.upper(),candidate.notional_usd)))
        return {"risk_usd":str(candidate.notional_usd),"spend_usd":str(spend),"fee_usd":str(candidate.estimated_fee_usd),
            "asset_amounts":collateral,"available_asset_amounts":{k:str(v) for k,v in snapshot.free_by_asset.items()},
            "plan":plan.asdict(),"market_snapshot":market,"budget_decision":decision.asdict(),
            "account_binding":account_revision(profile),"provider_binding":provider.binding(),
            "budget_nav_usd":str(snapshot.nav_usd),"budget_free_usd":str(free_usd_for_account(snapshot,profile.base_currency)),
            "minimum_free_usd":str(Decimal(str(snapshot.nav_usd))*Decimal(str(profile.limits.min_free_balance_pct or 0))),"expires_at":time.time()+30}

    def validate(self,request,quote):
        if quote["expires_at"]<=time.time():raise FinancialError("financial_quote_expired")
        if not self.config.live_trading_enabled():raise FinancialError("live_trading_disabled",403)
        if request["kind"]=="trade":
            from ..trading.accounts import get_account_profile,account_revision
            from ..connectors.provider_spec import get_registry
            profile=get_account_profile(self.config.paths,request["account_id"])
            if not profile.is_real_money or not profile.live_trading_enabled:raise FinancialError("financial_account_not_live",403)
            provider=get_registry(self.config.paths.root).find(profile.venue)
            if quote.get("account_binding") and quote["account_binding"]!=account_revision(profile):
                raise FinancialError("financial_account_binding_changed",403)
            if quote.get("provider_binding") and (not provider or provider.binding()!=quote["provider_binding"]):
                raise FinancialError("financial_provider_revision_changed",403)

    def execute(self,request,quote,submitted):
        self.validate(request,quote)
        if request["kind"]=="swap":
            from ..wallet.swap_approval import execute_frozen_swap
            response=execute_frozen_swap(self.config,request=quote["wallet_request"],approved_quote=quote["wallet_quote"],
                                         approval_id_value=self.config.get("runtime.financial_action_id"),expires_at=quote["expires_at"])
            from ..wallet import execution_state
            stored=execution_state.read(self.config,self.config.get("runtime.financial_action_id")) or {}
            transaction=response.get("transaction") or stored.get("transaction") or response.get("result") or {}
            ref=transaction.get("tx_hash") or transaction.get("hash")
            if ref:submitted({"transaction_hash":ref,"chain":request["chain"]})
            return {"state":"confirmed" if response.get("ok") and (response.get("result") or {}).get("extra",{}).get("confirmed") else "submitted" if ref else "unconfirmed",
                    "result":response,"submission":{"transaction_hash":ref,"chain":request["chain"],"execution_id":self.config.get("runtime.financial_action_id")} if ref else None}
        from ..sdk.trading_api import _plan_from_dict
        from ..trading.submit import submit_trade_plan
        plan=_plan_from_dict(quote["plan"]);plan.meta=dict(quote["plan"].get("meta") or {})
        response=submit_trade_plan(self.config,plan,market_snapshot=quote["market_snapshot"])
        ids={key:response[key] for key in ("intent_id","plan_id","order_id","executor_id") if response.get(key)}
        if (response.get('executor') or {}).get('order_ids'):ids['order_ids']=response['executor']['order_ids']
        if ids:submitted(ids)
        if ids.get('order_id') or ids.get('order_ids'):
            return {**self.status(request,quote,ids),'result':response,'submission':ids}
        status=response.get("status")
        state="confirmed" if status=="filled" else "submitted" if ids.get('order_id') or ids.get('order_ids') or status in {"submitted","pending","executing"} else 'rejected' if status=='rejected' else "needs_recovery"
        return {"state":state,"result":response,"submission":ids}

    def status(self,request,quote,submission):
        if request["kind"]=="swap":
            from ..wallet import execution_state
            execution_id=submission.get("execution_id") or self.config.get("runtime.financial_action_id")
            if execution_id and execution_state.read(self.config,execution_id):
                from ..wallet.swap_approval import reconcile_execution
                result=reconcile_execution(self.config,execution_id)
                return {"state":"confirmed" if result.get("status")=="confirmed" and result.get("ok") else "needs_recovery" if result.get("status")=="confirmed" else "confirming", "result":result}
            from ..wallet.confirmation import read_transaction
            from .adapters import owned_wallet
            _,provider,raw,_=owned_wallet(self.config,request)
            result=read_transaction(provider,raw,quote["wallet_request"],{**submission,'tx_hash':submission.get('transaction_hash')})
            raw=result.to_dict()
            return {"state":"confirmed" if raw.get("extra",{}).get("confirmed") and raw.get("ok") else "confirming","result":raw}
        from ..trading.order_tracker import OrderTracker
        order_ids=submission.get('order_ids') or [submission.get('order_id')]
        if not all(order_ids):return {"state":"unconfirmed","reason":"order_reference_missing"}
        tracker=OrderTracker(self.config.paths)
        try:
            orders=[tracker.get(order_id) for order_id in order_ids]
            fills=[fill for order_id in order_ids for fill in tracker.fills_for_order(order_id)]
        finally:tracker.close()
        if not all(orders):return {'state':'unconfirmed','reason':'order_receipt_missing'}
        settled={'settled_notional_usd':str(sum((amount(fill.notional_usd,zero=True) for fill in fills),Decimal(0))),
            'settled_fee_usd':str(sum((amount(fill.fee_usd,zero=True) for fill in fills),Decimal(0)))}
        if len(orders)>1:
            states={o.state for o in orders}
            return {'state':'confirmed' if states<={'filled','canceled','rejected','expired'} and any(o.filled_size for o in orders) else 'rejected' if states<={'canceled','rejected','expired'} else 'confirming',
                'partial':states!={'filled'},'orders':[o.asdict() for o in orders],**settled}
        order=orders[0];order_id=order.order_id
        state=order.state
        receipt={'order_id':order_id,'order_status':state,'filled_size':str(order.filled_size),'average_price':str(order.avg_price),'fee_usd':str(order.fee_usd)}
        if state=='filled':return {'state':'confirmed',**receipt,**settled} if fills else {'state':'needs_recovery','reason':'fill_accounting_missing',**receipt}
        if state in {'canceled','rejected','expired'}:return {'state':'confirmed' if order.filled_size and fills else 'needs_recovery' if order.filled_size else 'rejected','partial':bool(order.filled_size),**receipt,**settled}
        return {'state':'confirming',**receipt}
