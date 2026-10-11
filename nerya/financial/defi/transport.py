"""Owned EVM signing plus frozen cost, deployment and receipt validation."""
import time
from decimal import Decimal

from eth_abi import decode

from ...connectors.chains import ChainRegistry
from ..adapters import EvmFunds, amount, usd_price
from ..contracts import FinancialError, security_revision
from .book import owner_id
from .calls import calldata


class ProtocolFunds(EvmFunds):
    def __init__(self,config,request,**kwargs):
        super().__init__(config,request,**kwargs)
        self.protocol=request["protocol"]
        self.registry=ChainRegistry(config)
        self.deployment=self.registry.deployment(self.chain,self.protocol,self.connector)

    def call(self,target,signature,types,values,result_types,block="latest"):
        response=self.connector._rpc("eth_call",[{"to":target,"data":calldata(signature,types,values)},block])
        if not isinstance(response,str) or response=="0x":raise FinancialError("protocol_state_unavailable",503)
        try:return decode(result_types,bytes.fromhex(response[2:]))
        except (ValueError,TypeError) as exc:raise FinancialError("protocol_state_decode_failed",503) from exc

    def owner(self):
        aid=self.config.get("runtime.financial_action_id")
        if aid:
            from ..store import FinancialStore
            with FinancialStore(self.config).transaction() as con:
                row=con.execute("SELECT context_json FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
                if not row:raise FinancialError("protocol_action_identity_required",403)
                import json
                return owner_id(json.loads(row[0]))
        return owner_id(self.config.get("runtime.financial_context",{}))

    def transaction_quote(self,request,tx,*,risk_usd,spend_assets,prices,prerequisites=()):
        cap=self.config.get("financial.defi.max_gas_limit")
        if type(cap) is not int or not 21000<=cap<=10_000_000:raise FinancialError("protocol_gas_limit_required",422)
        gas=cap if prerequisites else (int(self.connector._rpc("eth_estimateGas",[tx]),16)*120+99)//100
        if gas>cap:raise FinancialError("protocol_gas_limit_exceeded",403)
        gas_price=int(self.connector._rpc("eth_gasPrice",[]),16)
        native_price=usd_price(self.raw.get("native_symbol") or self.chain_spec.native_symbol)
        gas_units=Decimal(gas*gas_price)/Decimal(10**18)
        assets={**spend_assets,"NATIVE":str(gas_units)}
        balances={"NATIVE":str(Decimal(int(self.connector._rpc("eth_getBalance",[self.sender,"latest"]),16))/Decimal(10**18))}
        for asset in spend_assets:
            decimals=self.connector.get_erc20_decimals(asset)
            balance=self.call(asset,"balanceOf(address)",["address"],[self.sender],["uint256"])[0]
            balances[asset]=str(Decimal(balance)/Decimal(10**decimals))
        return {"risk_usd":str(risk_usd),"spend_usd":str(sum((amount(value,zero=True)*prices[asset] for asset,value in spend_assets.items()),Decimal(0))),
                "fee_usd":str(gas_units*native_price),"asset_amounts":assets,"available_asset_amounts":balances,
                "transaction":tx,"gas_limit":gas,"gas_price_wei":str(gas_price),"native_price_usd":str(native_price),
                "chain_id":self.chain_spec.chain_id,"sender":self.sender,"expires_at":time.time()+30,
                "deployment_revision":security_revision(self.deployment),"prerequisites":list(prerequisites),
                "steps":[{"kind":"protocol_call","router":tx["to"],"chain":self.chain}]}

    def validate(self,request,quote):
        if not self.config.get("financial.wallet_permissions",{}).get(request["wallet_id"],{}).get(request["kind"],False):
            raise FinancialError("wallet_funds_permission_disabled",403)
        if quote["expires_at"]<=time.time():raise FinancialError("financial_quote_expired")
        current=self.registry.deployment(self.chain,self.protocol,self.connector)
        if security_revision(current)!=quote["deployment_revision"]:raise FinancialError("protocol_deployment_changed",403)
        tx=quote["transaction"]
        if tx["from"]!=self.sender or quote["chain_id"]!=self.chain_spec.chain_id or int(tx["value"],16)!=0:
            raise FinancialError("protocol_transaction_identity_mismatch",403)
        if int(self.connector._rpc("eth_gasPrice",[]),16)>int(quote["gas_price_wei"]):raise FinancialError("protocol_gas_quote_changed")
        for prerequisite in quote.get("prerequisites",[]):
            if prerequisite["kind"]=="permit2_approval":
                allowance,expiration,_=self.call(self.deployment["contracts"]["permit2"],"allowance(address,address,address)",["address"]*3,
                                                [self.sender,prerequisite["asset"],prerequisite["spender"]],["uint160","uint48","uint48"])
                if expiration<(prerequisite.get("parameters") or {}).get("expiration",0):raise FinancialError("finite_protocol_allowance_required",403)
            else:
                allowance=self.call(prerequisite["asset"],"allowance(address,address)",["address","address"],
                                    [self.sender,prerequisite["spender"]],["uint256"])[0]
            from ..adapters import units
            if allowance<units(prerequisite["amount"],self.connector.get_erc20_decimals(prerequisite["asset"])):
                raise FinancialError("finite_protocol_allowance_required",403)
        if int(self.connector._rpc("eth_estimateGas",[tx]),16)>quote["gas_limit"]:
            raise FinancialError("protocol_gas_limit_changed",403)

    def execute(self,request,quote,submitted):
        aid=self.config.get("runtime.financial_action_id")
        if not aid:raise FinancialError("protocol_action_identity_required",403)
        result=super().execute(request,quote,lambda ref:submitted({**ref,"action_id":aid}))
        result["submission"]["action_id"]=aid
        return result

    def verified_receipt(self,quote,submission):
        ref=submission.get("transaction_hash")
        if not ref:return {"state":"unconfirmed","reason":"transaction_reference_missing"},None
        self.chain_spec.verify(self.connector)
        receipt=self.connector._rpc("eth_getTransactionReceipt",[ref])
        if not receipt:return {"state":"confirming"},None
        number=int(receipt["blockNumber"],16)
        canonical=self.connector._rpc("eth_getBlockByNumber",[receipt["blockNumber"],False])
        if not canonical or canonical.get("hash")!=receipt.get("blockHash"):
            return {"state":"unconfirmed","reason":"transaction_block_reorganized"},None
        confirmations=int(self.config.get("financial.confirmations",{}).get(self.chain,self.chain_spec.confirmations))
        if self.connector.get_block_number()-number+1<confirmations:return {"state":"confirming"},None
        tx=self.connector._rpc("eth_getTransactionByHash",[ref]);planned=quote["transaction"]
        if not tx or tx.get("hash",ref).lower()!=ref.lower() or tx.get("from","").lower()!=self.sender.lower() or tx.get("to","").lower()!=planned["to"].lower() or tx.get("input","0x").lower()!=planned["data"].lower() or int(tx.get("value","0x0"),16)!=0:
            return {"state":"needs_recovery","reason":"transaction_parameters_mismatch"},None
        if receipt.get("gasUsed") is None or receipt.get("effectiveGasPrice") is None:
            return {"state":"needs_recovery","reason":"transaction_fee_evidence_missing"},None
        fee=Decimal(int(receipt["gasUsed"],16)*int(receipt["effectiveGasPrice"],16))/Decimal(10**18)*amount(quote["native_price_usd"])
        result={"transaction_hash":ref,"block_number":number,"block_hash":receipt["blockHash"],"settled_fee_usd":str(fee)}
        if int(receipt.get("status","0x0"),16)!=1:
            return {"state":"rejected","reason":"chain_reverted","settled_notional_usd":"0",**result},None
        return result,receipt
