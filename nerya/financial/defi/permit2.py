"""Finite, expiring Permit2 approval as its own authorized funds action."""
import time

from ..adapters import amount,units,usd_price
from ..contracts import FinancialError
from .calls import address,permit2_approval,matching_event,topic_address
from .transport import ProtocolFunds


class Permit2Funds(ProtocolFunds):
    def __init__(self,config,request,**kwargs):
        if request["kind"]!="permit2_approval":raise FinancialError("unsupported_permit2_operation",422)
        super().__init__(config,request,**kwargs)
        target=self.deployment["contracts"].get("permit2")
        manager=self.deployment["contracts"].get("position_manager")
        if not target or not manager:raise FinancialError("permit2_deployment_incomplete",422)
        self.permit2,self.manager=address(target),address(manager)
        if address(request["spender"])!=self.manager:raise FinancialError("permit2_spender_not_reviewed",403)

    def _transaction(self,request):
        expiration=(request.get("parameters") or {}).get("expiration")
        maximum=self.config.get("financial.defi.max_permit2_seconds")
        if type(maximum) is not int or not 1<=maximum<=30*86400:raise FinancialError("permit2_expiry_limit_required",422)
        if type(expiration) is not int or not time.time()<expiration<=time.time()+maximum:
            raise FinancialError("permit2_expiry_out_of_policy",403)
        asset=address(request["asset"])
        quantity=units(request["amount"],self.connector.get_erc20_decimals(asset))
        return permit2_approval(self.permit2,self.sender,asset,self.manager,quantity,expiration),quantity,expiration

    def quote(self,request):
        tx,quantity,expiration=self._transaction(request)
        _,_,_,symbol=self.asset(request["asset"])
        value=amount(request["amount"],zero=True)*usd_price(symbol)
        quote=self.transaction_quote(request,tx,risk_usd=value,spend_assets={},prices={})
        current=self.call(self.permit2,"allowance(address,address,address)",["address"]*3,
                          [self.sender,address(request["asset"]),self.manager],["uint160","uint48","uint48"])
        quote["steps"].append({"kind":"finite_permit2_allowance","spender":self.manager,"asset":request["asset"],
                               "amount_base":str(quantity),"expiration":expiration})
        return {**quote,"amount_base":str(quantity),"expiration":expiration,"previous_allowance":list(current)}

    def validate(self,request,quote):
        tx,quantity,expiration=self._transaction(request)
        if tx!=quote["transaction"] or str(quantity)!=quote["amount_base"] or expiration!=quote["expiration"]:
            raise FinancialError("permit2_plan_mismatch",403)
        super().validate(request,quote)

    def status(self,request,quote,submission):
        result,receipt=self.verified_receipt(quote,submission)
        if receipt is None:return result
        try:
            log,values=matching_event(receipt.get("logs") or [],self.permit2,
                "Approval(address,address,address,uint160,uint48)",
                [topic_address(self.sender),topic_address(request["asset"]),topic_address(self.manager)],["uint160","uint48"])
            if values!=(int(quote["amount_base"]),quote["expiration"]):raise FinancialError("permit2_approval_event_mismatch",403)
            observed=self.call(self.permit2,"allowance(address,address,address)",["address"]*3,
                               [self.sender,address(request["asset"]),self.manager],["uint160","uint48","uint48"],receipt["blockNumber"])
            if observed[:2]!=values:raise FinancialError("permit2_allowance_state_mismatch",403)
            return {"state":"confirmed",**result,"settled_notional_usd":quote["risk_usd"],"allowance_amount_base":str(values[0]),
                    "allowance_expiration":values[1],"log_index":int(log["logIndex"],16)}
        except FinancialError as exc:return {"state":"needs_recovery",**result,"reason":exc.code}
