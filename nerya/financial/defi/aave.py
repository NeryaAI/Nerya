"""Aave v3 variable-debt lifecycle through the common financial gateway."""
import time
from decimal import Decimal

from ...trading.portfolio_risk import RiskMetrics,evaluate
from ..adapters import units
from ..contracts import FinancialError,amount
from .book import ProtocolBook
from .calls import ZERO,aave_call,aave_receipt,address
from .transport import ProtocolFunds

RAY=10**27
OPERATIONS={"lend_supply":"supply","lend_withdraw":"withdraw","borrow":"borrow","repay":"repay"}


class AaveFunds(ProtocolFunds):
    def __init__(self,config,request,**kwargs):
        if request.get("protocol")!="aave_v3" or request["kind"] not in OPERATIONS:
            raise FinancialError("unsupported_aave_operation",422)
        super().__init__(config,request,**kwargs)
        contracts=self.deployment["contracts"]
        for name in ("pool","data_provider","oracle"):
            if not contracts.get(name):raise FinancialError("aave_deployment_incomplete",422)
        if not (self.deployment.get("implementations") or {}).get("pool"):
            raise FinancialError("aave_pool_implementation_evidence_required",422)
        self.pool,self.data,self.oracle=(address(contracts[name]) for name in ("pool","data_provider","oracle"))
        self.book=ProtocolBook(config)

    def _state(self,asset,block="latest"):
        asset=address(asset)
        currency=self.call(self.oracle,"BASE_CURRENCY()",[],[],["address"],block)[0]
        if currency.lower()!=ZERO:raise FinancialError("aave_non_usd_oracle_unsupported",422)
        unit=self.call(self.oracle,"BASE_CURRENCY_UNIT()",[],[],["uint256"],block)[0]
        if unit<=0:raise FinancialError("aave_oracle_unit_unavailable",503)
        price=Decimal(self.call(self.oracle,"getAssetPrice(address)",["address"],[asset],["uint256"],block)[0])/Decimal(unit)
        if price<=0:raise FinancialError("aave_oracle_price_unavailable",503)
        source=self.call(self.oracle,"getSourceOfAsset(address)",["address"],[asset],["address"],block)[0]
        expected=(self.deployment.get("oracle_sources") or {}).get(asset.lower())
        if not expected or source.lower()!=expected.lower():raise FinancialError("aave_oracle_source_not_reviewed",422)
        round_id,answer,_,updated,answered=self.call(source,"latestRoundData()",[],[],["uint80","int256","uint256","uint256","uint80"],block)
        if block=="latest" and (answer<=0 or answered<round_id or not 0<=time.time()-updated<=float(self.config.get("financial.defi.max_oracle_age_seconds",3600))):
            raise FinancialError("aave_oracle_evidence_stale",503)
        aggregate=self.call(self.pool,"getUserAccountData(address)",["address"],[self.sender],["uint256"]*6,block)
        reserve=self.call(self.data,"getUserReserveData(address,address)",["address","address"],[asset,self.sender],
                          ["uint256"]*7+["uint40","bool"],block)
        tokens=self.call(self.data,"getReserveTokensAddresses(address)",["address"],[asset],["address"]*3,block)
        scaled=self.call(tokens[0],"scaledBalanceOf(address)",["address"],[self.sender],["uint256"],block)[0]
        income=self.call(self.pool,"getReserveNormalizedIncome(address)",["address"],[asset],["uint256"],block)[0]
        debt_index=self.call(self.pool,"getReserveNormalizedVariableDebt(address)",["address"],[asset],["uint256"],block)[0]
        reserve_config=self.call(self.pool,"getConfiguration(address)",["address"],[asset],["uint256"],block)[0]
        mode=self.call(self.pool,"getUserEMode(address)",["address"],[self.sender],["uint256"],block)[0]
        if mode!=0:raise FinancialError("aave_emode_requires_dedicated_risk_model",422)
        if reserve[1]:raise FinancialError("aave_stable_debt_requires_dedicated_risk_model",422)
        decimals=self.connector.get_erc20_decimals(asset)
        if decimals!=((reserve_config>>48)&255):raise FinancialError("aave_asset_decimals_mismatch",422)
        return {"asset":asset,"price_usd":str(price),"decimals":decimals,"collateral_usd":str(Decimal(aggregate[0])/unit),
                "debt_usd":str(Decimal(aggregate[1])/unit),"available_borrow_usd":str(Decimal(aggregate[2])/unit),
                "liquidation_threshold_bps":aggregate[3],"ltv_bps":aggregate[4],
                "health_factor":str(Decimal(aggregate[5])/10**18) if aggregate[1] else None,
                "supply_base":str(reserve[0]),"debt_base":str(reserve[2]),"supply_scaled":str(scaled),"debt_scaled":str(reserve[4]),
                "collateral_enabled":reserve[8],"income_index":str(income),"debt_index":str(debt_index),
                "reserve_config":str(reserve_config),"a_token":tokens[0],"debt_token":tokens[2],"as_of":time.time()}

    def account_state(self,request):
        state=self._state(request["asset"])
        return {"source":"aave_v3:"+self.pool,"as_of":state["as_of"],"health":"ok","coverage":["collateral","debt","interest","health_factor"],"state":state}

    def _key(self,asset):
        return f"{self.chain_spec.chain_id}:aave_v3:{self.pool.lower()}:{self.sender.lower()}:{asset.lower()}"

    def _projection(self,state,operation,value):
        collateral,debt=amount(state["collateral_usd"],zero=True),amount(state["debt_usd"],zero=True)
        weighted=collateral*Decimal(state["liquidation_threshold_bps"])/10000
        after_collateral,after_debt=collateral,debt
        if operation=="withdraw":
            after_collateral=max(Decimal(0),collateral-value) if state["collateral_enabled"] else collateral
            threshold=(int(state["reserve_config"])>>16)&65535
            if state["collateral_enabled"]:weighted=max(Decimal(0),weighted-value*threshold/10000)
        elif operation=="borrow":after_debt+=value
        elif operation=="repay":after_debt=max(Decimal(0),debt-value)
        # Supply is not credited as collateral until chain confirmation proves
        # that the reserve was actually enabled for this account.
        before=RiskMetrics(collateral-debt,collateral+debt,debt_usd=debt,
                           health_factor=Decimal(state["health_factor"]) if state["health_factor"] is not None else None)
        after=RiskMetrics(collateral-debt,after_collateral+after_debt,debt_usd=after_debt,
                          health_factor=weighted/after_debt if after_debt else None)
        return {"before":before.asdict(),"after":after.asdict(),"source":"aave_v3:"+self.pool,"as_of":state["as_of"]}

    def _admit(self,request,state,quantity,owner):
        operation=OPERATIONS[request["kind"]]
        config=int(state["reserve_config"])
        if not ((config>>56)&1) or ((config>>60)&1):raise FinancialError("aave_reserve_inactive_or_paused",422)
        if operation in {"supply","borrow"} and ((config>>57)&1):raise FinancialError("aave_reserve_frozen",422)
        if operation=="borrow" and not ((config>>58)&1):raise FinancialError("aave_reserve_borrowing_disabled",422)
        value=Decimal(quantity)/10**state["decimals"]*amount(state["price_usd"])
        if operation=="borrow" and value>amount(state["available_borrow_usd"],zero=True):raise FinancialError("aave_borrow_capacity_exceeded",403)
        key=self._key(state["asset"])
        claims,total=self.book.claims(key,owner)
        for name in ("supply_scaled","debt_scaled"):
            if total.get(name,Decimal(0))>Decimal(state[name])+1:raise FinancialError("protocol_claim_reconciliation_required",403)
        if operation in {"withdraw","repay"}:
            name="supply_scaled" if operation=="withdraw" else "debt_scaled"
            index=Decimal(state["income_index"] if operation=="withdraw" else state["debt_index"])
            if claims.get(name,Decimal(0))<=0 or Decimal(quantity)>claims[name]*index/RAY:
                raise FinancialError("protocol_exit_exceeds_owned_claim",403)
        effect=self._projection(state,operation,value)
        limits=self.config.get("financial.portfolio_limits",{}).get("wallet:"+request["wallet_id"],{})
        if operation in {"withdraw","borrow"} and (not limits.get("min_health_factor") or amount(limits["min_health_factor"])<=1):
            raise FinancialError("aave_health_factor_buffer_required",403)
        reasons=evaluate(RiskMetrics(**effect["before"]),RiskMetrics(**effect["after"]),limits,mode=str(self.config.get("trading.risk_mode","normal")))
        if reasons:raise FinancialError(reasons[0],403)
        return value,effect

    def quote(self,request):
        quantity=amount(request.get("amount"))
        asset=address(request.get("asset"));state=self._state(asset)
        raw=units(quantity,state["decimals"]);owner=self.owner()
        value,effect=self._admit(request,state,raw,owner)
        operation=OPERATIONS[request["kind"]]
        tx=aave_call(self.pool,self.sender,operation,asset,raw)
        prerequisites=[]
        spend={asset:str(quantity)} if operation in {"supply","repay"} else {}
        if spend:
            allowance=self.call(asset,"allowance(address,address)",["address","address"],[self.sender,self.pool],["uint256"])[0]
            if allowance<raw:
                prerequisites=[{"kind":"contract_approval","wallet_id":request["wallet_id"],"chain":self.chain,"asset":asset,"amount":str(quantity),"spender":self.pool}]
        quote=self.transaction_quote(request,tx,risk_usd=value,spend_assets=spend,prices={asset:amount(state["price_usd"])},prerequisites=prerequisites)
        return {**quote,"aave_before":state,"position_key":self._key(asset),"position_owner":owner,"operation":operation,
                "amount_base":str(raw),"portfolio_effect":effect,"exposure_asset_amounts":{asset:str(quantity)}}

    def validate(self,request,quote):
        asset=address(request["asset"])
        state=self._state(asset)
        quantity=units(request["amount"],state["decimals"])
        expected=aave_call(self.pool,self.sender,OPERATIONS[request["kind"]],asset,quantity)
        if expected!=quote["transaction"] or self._key(asset)!=quote["position_key"]:
            raise FinancialError("aave_plan_mismatch",403)
        owner=self.owner() if self.config.get("runtime.financial_action_id") or self.config.get("runtime.financial_context") else quote["position_owner"]
        if owner!=quote["position_owner"]:raise FinancialError("protocol_owner_changed",403)
        self._admit(request,state,quantity,owner)
        before=amount(quote["aave_before"]["price_usd"]);current=amount(state["price_usd"])
        if abs(current-before)>before*Decimal("0.005"):raise FinancialError("protocol_price_quote_changed")
        super().validate(request,quote)

    def status(self,request,quote,submission):
        result,receipt=self.verified_receipt(quote,submission)
        if receipt is None:return result
        try:
            proof=aave_receipt(receipt.get("logs") or [],self.pool,self.sender,quote["operation"],request["asset"],int(quote["amount_base"]))
            state=self._state(request["asset"],receipt["blockNumber"])
            index=int(state["income_index"] if quote["operation"] in {"supply","withdraw"} else state["debt_index"])
            scaled=(int(proof["amount_base"])*RAY+index//2)//index
            name="supply_scaled" if quote["operation"] in {"supply","withdraw"} else "debt_scaled"
            delta={name:str(scaled if quote["operation"] in {"supply","borrow"} else -scaled)}
            action_id=submission.get("action_id")
            if not action_id:raise FinancialError("protocol_action_identity_required",403)
            self.book.record(action_id,key=quote["position_key"],wallet_id=request["wallet_id"],chain=self.chain,protocol=self.protocol,
                kind="lending",owner_address=self.sender,state=state,block_number=result["block_number"],block_hash=result["block_hash"],delta=delta,
                proof={**proof,"transaction_hash":result["transaction_hash"],"block_hash":result["block_hash"]})
            value=Decimal(proof["amount_base"])/10**state["decimals"]*amount(state["price_usd"])
            return {"state":"confirmed",**result,"settled_notional_usd":str(value),"protocol_effect":proof,"account_state":state,"position_key":quote["position_key"]}
        except FinancialError as exc:return {"state":"needs_recovery",**result,"reason":exc.code}
