"""LI.FI quote → decoded immutable plan → owned signer → two-chain receipts."""
from __future__ import annotations
from decimal import Decimal
import copy
import time
from urllib.parse import urlencode

from .contracts import FinancialError,amount,digest
from .adapters import EvmFunds,owned_wallet,validate_evm_address,units

BRIDGE_DATA="(bytes32,string,string,address,address,address,uint256,uint256,bool,bool)"
SWAP_DATA="(address,address,address,address,uint256,bytes,bool)[]"


def _at(values,path):
    try:
        for index in path:values=values[int(index)]
        return values
    except (TypeError,ValueError,IndexError):raise FinancialError('bridge_reviewed_path_invalid',422)


def decode_swap_call(swap,*,receiver,routers,now=None):
    """Reviewed Uniswap V2 token swaps; no arbitrary multicall/delegatecall."""
    from eth_abi import decode
    selector='0x'+swap[5][:4].hex()
    if selector!='0x38ed1739':raise FinancialError('bridge_dex_selector_not_reviewed',422)
    try:quantity,minimum,path,to,deadline=decode(['uint256','uint256','address[]','address','uint256'],swap[5][4:])
    except Exception:raise FinancialError('bridge_dex_calldata_invalid',422)
    if swap[0].lower() not in {r.lower() for r in routers} or swap[1].lower()!=swap[0].lower():raise FinancialError('bridge_dex_router_or_spender_mismatch',403)
    if not path or len(path)>5 or path[0].lower()!=swap[2].lower() or path[-1].lower()!=swap[3].lower():raise FinancialError('bridge_dex_path_mismatch',403)
    if to.lower()!=receiver.lower() or quantity!=swap[4] or minimum<=0:raise FinancialError('bridge_dex_receiver_or_amount_mismatch',403)
    if deadline< (time.time() if now is None else now):raise FinancialError('bridge_dex_deadline_expired')
    return {'router':swap[0],'spender':swap[1],'from_asset':swap[2],'to_asset':swap[3],
        'from_amount':str(quantity),'minimum_output_base':str(minimum),'receiver':to,'deadline':deadline,
        'call_data':'0x'+swap[5].hex(),'requires_deposit':swap[6]}


def decode_bridge_transaction(transaction,request,from_chain,to_chain,quantity,*,selectors,minimum_output=None):
    """Untrusted route JSON cannot replace checks on actual transaction bytes."""
    from eth_abi import decode
    encoded=transaction.get("data","")
    selector=encoded[:10].lower()
    config=selectors.get(selector)
    if not config:raise FinancialError("bridge_selector_not_reviewed",422)
    types=[BRIDGE_DATA]+([SWAP_DATA] if config.get("source_swaps") else [])+list(config.get("extra_types",[]))
    try:decoded=decode(types,bytes.fromhex(encoded[10:]),strict=False)
    except Exception:raise FinancialError("bridge_calldata_invalid",422)
    bridge=decoded[0]
    if minimum_output is not None and not bridge[9]:
        path=config.get("minimum_output_path")
        if not isinstance(path,list) or not path:raise FinancialError("bridge_destination_floor_not_reviewed",422)
        try:
            observed=decoded
            for index in path:observed=observed[int(index)]
            if int(observed)<int(minimum_output):raise ValueError()
        except (ValueError,TypeError,IndexError):raise FinancialError("bridge_destination_floor_mismatch",403)
    if bridge[7]!=to_chain:
        raise FinancialError("bridge_receiver_or_destination_mismatch",403)
    destination=[]
    if bridge[9]:
        path=config.get('destination_swaps_path')
        routers=config.get('destination_routers',[])
        if not path or bridge[5].lower() not in {r.lower() for r in routers}:raise FinancialError('bridge_destination_call_decoder_required',422)
        nested=_at(decoded,path)
        if not isinstance(nested,(tuple,list)) or not nested:raise FinancialError('bridge_destination_swaps_missing',422)
        for index,swap in enumerate(nested):
            if swap[6]:raise FinancialError('bridge_destination_wallet_deposit_forbidden',403)
            destination.append(decode_swap_call(swap,receiver=request['recipient'] if index==len(nested)-1 else bridge[5],routers=routers))
            if index and swap[2].lower()!=nested[index-1][3].lower():raise FinancialError('bridge_destination_asset_path_mismatch',403)
        if nested[-1][3].lower()!=request.get('to_asset','').lower() or int(destination[-1]['minimum_output_base'])<int(minimum_output or 1):
            raise FinancialError('bridge_destination_floor_mismatch',403)
    elif bridge[5].lower()!=request['recipient'].lower():raise FinancialError('bridge_receiver_or_destination_mismatch',403)
    swaps=decoded[1] if config.get("source_swaps") else []
    if bool(bridge[8])!=bool(swaps):raise FinancialError("bridge_swap_flags_mismatch",403)
    if not swaps and (bridge[4].lower()!=request["asset"].lower() or bridge[6]!=quantity):
        raise FinancialError("bridge_asset_or_quantity_mismatch",403)
    if swaps and (swaps[0][2].lower()!=request["asset"].lower() or swaps[0][4]!=quantity):
        raise FinancialError("bridge_source_swap_quantity_mismatch",403)
    if int(transaction.get("chainId",0))!=from_chain:raise FinancialError("bridge_source_chain_mismatch",403)
    source=[]
    if swaps:
        routers=config.get('source_routers',[])
        for index,swap in enumerate(swaps):
            if index and (swap[6] or swap[2].lower()!=swaps[index-1][3].lower()):raise FinancialError('bridge_source_asset_path_mismatch',403)
            source.append(decode_swap_call(swap,receiver=transaction['to'],routers=routers))
        if swaps[-1][3].lower()!=bridge[4].lower() or int(source[-1]['minimum_output_base'])<bridge[6]:raise FinancialError('bridge_source_output_mismatch',403)
    return {"bridge":bridge[1],"receiver":bridge[5],"minimum_bridge_amount":str(bridge[6]),
        "destination_chain_id":bridge[7],"source_swaps":source,'destination_swaps':destination}


class LifiFunds:
    def __init__(self,config,request,*,transport=None,source=None):
        from ..connectors.dex_base import UrllibHttp
        from ..connectors.evm_native import EVM_CHAIN_IDS
        self.config=config;self.transport=transport or UrllibHttp()
        self.settings=config.get("financial.lifi",{}) or {}
        self.from_chain=EVM_CHAIN_IDS.get(request["chain"]);self.to_chain=EVM_CHAIN_IDS.get(request["to_chain"])
        if not self.from_chain or not self.to_chain:raise FinancialError("bridge_chain_decoder_not_available",422)
        self.source=source or EvmFunds(config,{**request,"kind":"wallet_transfer"})

    def _get(self,path,params):
        status,doc=self.transport.request("GET","https://li.quest/v1/"+path+"?"+urlencode(params,doseq=True),timeout=20)
        if status!=200:raise FinancialError("bridge_provider_unavailable",503)
        return doc

    def quote(self,request):
        bridges=self.settings.get("allowed_bridges")
        routers=self.settings.get("allowed_routers",{}).get(str(self.from_chain),[])
        if not bridges or not routers:raise FinancialError("bridge_routes_not_reviewed",422)
        native,token,decimals,_=self.source.asset(request["asset"])
        if native:raise FinancialError("bridge_native_source_decoder_required",422)
        quantity=units(request["amount"],decimals)
        doc=self._get("quote",{"fromChain":self.from_chain,"toChain":self.to_chain,"fromToken":token,
            "toToken":request["to_asset"],"fromAmount":str(quantity),"fromAddress":self.source.sender,
            "toAddress":validate_evm_address(request["recipient"]),"slippage":str(Decimal(request["slippage_bps"])/10000),
            "allowBridges":bridges,"integrator":"nerya","order":"SAFEST"})
        action=doc["action"];estimate=doc["estimate"];tx=doc["transactionRequest"]
        if doc.get("tool") not in bridges:raise FinancialError("bridge_tool_not_allowed",403)
        if action["fromChainId"]!=self.from_chain or action["toChainId"]!=self.to_chain or int(action["fromAmount"])!=quantity:
            raise FinancialError("bridge_quote_input_mismatch",403)
        if action["fromToken"]["address"].lower()!=token.lower() or action["toToken"]["address"].lower()!=request["to_asset"].lower():
            raise FinancialError("bridge_quote_asset_mismatch",403)
        if action["fromAddress"].lower()!=self.source.sender.lower() or action["toAddress"].lower()!=request["recipient"].lower():
            raise FinancialError("bridge_quote_address_mismatch",403)
        if tx["to"].lower() not in {r.lower() for r in routers} or tx["from"].lower()!=self.source.sender.lower():
            raise FinancialError("bridge_router_or_sender_not_allowed",403)
        minimum=int(estimate["toAmountMin"])
        if minimum<=0:raise FinancialError("bridge_minimum_output_missing",422)
        decoded=decode_bridge_transaction(tx,request,self.from_chain,self.to_chain,quantity,selectors=self.settings.get("selectors",{}),minimum_output=minimum)
        for swap in decoded["source_swaps"]:
            if swap["router"].lower() not in {r.lower() for r in routers}:raise FinancialError("bridge_dex_router_not_allowed",403)
        minimum=int(estimate["toAmountMin"])
        if minimum<=0:raise FinancialError("bridge_minimum_output_missing",422)
        from .adapters import usd_price
        from_price=usd_price(self.source.asset(request['asset'])[3])
        fee=sum((amount(r.get("amountUSD"),zero=True) for r in estimate.get("feeCosts",[])),Decimal(0))
        fee+=sum((amount(r.get("amountUSD"),zero=True) for r in estimate.get("gasCosts",[])),Decimal(0))
        balance=self.source.connector._rpc("eth_call",[{"to":token,"data":"0x70a08231"+self.source.sender[2:].lower().rjust(64,"0")},"latest"])
        available=Decimal(int(balance,16))/Decimal(10**decimals)
        gas=int(str(tx["gasLimit"]),0) if str(tx["gasLimit"]).startswith("0x") else int(tx["gasLimit"])
        gas_price=int(str(tx["gasPrice"]),0) if str(tx["gasPrice"]).startswith("0x") else int(tx["gasPrice"])
        native_fee=Decimal(gas*gas_price)/Decimal(10**18)
        native_balance=Decimal(int(self.source.connector._rpc("eth_getBalance",[self.source.sender,"latest"]),16))/Decimal(10**18)
        spender=validate_evm_address(estimate["approvalAddress"])
        return {"risk_usd":str(amount(request["amount"])*from_price),"spend_usd":str(amount(request["amount"])*from_price),
            "fee_usd":str(fee),"asset_amounts":{request["asset"]:request["amount"],"NATIVE":str(native_fee)},
            "available_asset_amounts":{request["asset"]:str(available),"NATIVE":str(native_balance)},
            "route":doc,"route_hash":digest(doc),"transaction":tx,"decoded":decoded,"quantity_base":str(quantity),'sender':self.source.sender,
            "minimum_received_base":str(minimum),"receiver":request["recipient"],"expires_at":time.time()+45,
            "steps":[{"kind":"contract_approval","spender":spender,"router":tx["to"]}]+decoded["source_swaps"]+decoded['destination_swaps']}

    def validate(self,request,quote):
        permissions=self.config.get("financial.wallet_permissions",{}).get(request["wallet_id"],{})
        if not permissions.get("bridge_swap"):raise FinancialError("wallet_bridge_permission_disabled",403)
        if quote["expires_at"]<=time.time():raise FinancialError("financial_quote_expired")
        if digest(quote["route"])!=quote["route_hash"]:raise FinancialError("bridge_route_changed",403)
        decoded=decode_bridge_transaction(quote["transaction"],request,self.from_chain,self.to_chain,int(quote["quantity_base"]),selectors=self.settings.get("selectors",{}),minimum_output=quote["minimum_received_base"])
        if decoded!=quote["decoded"]:raise FinancialError("bridge_decoded_plan_changed",403)
        self.source.connector._verify_chain_id()
        # A separate finite allowance action must complete before bridge send.
        spender=quote["steps"][0]["spender"]
        call="0xdd62ed3e"+self.source.sender[2:].lower().rjust(64,"0")+spender[2:].lower().rjust(64,"0")
        allowance=int(self.source.connector._rpc("eth_call",[{"to":request["asset"],"data":call},"latest"]),16)
        if allowance<int(quote["quantity_base"]):raise FinancialError("finite_bridge_allowance_required")

    def execute(self,request,quote,submitted):
        from eth_account import Account
        self.validate(request,quote)
        key=self.source.provider._resolve_signer_key()
        try:
            if Account.from_key(key).address.lower()!=self.source.sender.lower():raise FinancialError("wallet_signer_mismatch",403)
            tx=quote["transaction"]
            value=int(str(tx.get("value","0")),0) if str(tx.get("value","0")).startswith("0x") else int(tx.get("value",0))
            gas=int(str(tx["gasLimit"]),0) if str(tx["gasLimit"]).startswith("0x") else int(tx["gasLimit"])
            price=int(str(tx["gasPrice"]),0) if str(tx["gasPrice"]).startswith("0x") else int(tx["gasPrice"])
            result=self.source.connector.send_raw_transaction(to=tx["to"],data=tx["data"],value=value,
                signer_private_key=key,gas_limit=gas,gas_price_wei=price,confirm=False,
                on_broadcast=lambda ref:submitted({"transaction_hash":ref["tx_hash"],"chain":request["chain"],"bridge":quote["route"]["tool"],"nonce":ref["nonce"]}))
        finally:key=""
        return {"state":"submitted","submission":{"transaction_hash":result["tx_hash"],"chain":request["chain"],"bridge":quote["route"]["tool"]}}

    def status(self,request,quote,submission):
        ref=submission.get("transaction_hash")
        if not ref:return {"state":"unconfirmed"}
        source=self.source.connector._rpc("eth_getTransactionReceipt",[ref])
        if not source:return {"state":"confirming","source_confirmed":False}
        if int(source.get("status","0x0"),16)!=1:return {"state":"needs_recovery","reason":"bridge_source_reverted"}
        source_tx=self.source.connector._rpc('eth_getTransactionByHash',[ref]);planned=quote['transaction']
        if not source_tx or source_tx.get('from','').lower()!=self.source.sender.lower() or source_tx.get('to','').lower()!=planned['to'].lower() or source_tx.get('input','').lower()!=planned['data'].lower():
            return {'state':'needs_recovery','reason':'bridge_source_parameters_mismatch'}
        required=int(self.config.get('financial.confirmations',{}).get(request['chain'],12))
        if self.source.connector.get_block_number()-int(source['blockNumber'],16)+1<required:return {'state':'confirming','source_confirmed':False}
        doc=self._get("status",{"txHash":ref,"bridge":submission.get("bridge") or quote["route"]["tool"],"fromChain":self.from_chain,"toChain":self.to_chain})
        receiving=doc.get("receiving") or {}
        if doc.get("status")=="FAILED":return {"state":"needs_recovery","source_confirmed":True,"provider_status":"FAILED"}
        if doc.get("status")!="DONE":return {"state":"confirming","source_confirmed":True,"provider_status":doc.get("status")}
        token=receiving.get("token") or {}
        if receiving.get("toAddress",receiving.get("address","")).lower()!=request["recipient"].lower() or token.get("address","").lower()!=request["to_asset"].lower() or int(receiving.get("amount",0))<int(quote["minimum_received_base"]):
            return {"state":"needs_recovery","source_confirmed":True,"reason":"bridge_destination_receipt_mismatch"}
        from ..connectors.evm_native import EVMNative
        rpc=(self.source.raw.get("rpc_urls") or {}).get(request["to_chain"])
        if not rpc:return {"state":"confirming","source_confirmed":True,"reason":"destination_rpc_unconfigured"}
        destination=EVMNative(chain=request["to_chain"],chain_id=self.to_chain,rpc_url=rpc)
        target_hash=receiving.get("txHash")
        receipt=destination._rpc("eth_getTransactionReceipt",[target_hash])
        if not receipt or int(receipt.get("status","0x0"),16)!=1:return {"state":"confirming","source_confirmed":True}
        required=int(self.config.get('financial.confirmations',{}).get(request['to_chain'],12))
        if destination.get_block_number()-int(receipt['blockNumber'],16)+1<required:return {'state':'confirming','source_confirmed':True}
        from eth_utils import keccak
        topic='0x'+keccak(text='Transfer(address,address,uint256)').hex()
        credited=sum(int(log.get('data','0x0'),16) for log in receipt.get('logs',[]) if log.get('address','').lower()==request['to_asset'].lower()
            and len(log.get('topics',[]))>=3 and log['topics'][0].lower()==topic.lower() and log['topics'][2][-40:].lower()==request['recipient'][2:].lower())
        if credited<int(quote['minimum_received_base']):return {'state':'needs_recovery','source_confirmed':True,'reason':'bridge_destination_credit_missing_or_below_floor'}
        return {"state":"confirmed","source_confirmed":True,"destination_confirmed":True,"transaction_hash":ref,
            "destination_transaction_hash":target_hash,"amount_received_base":str(credited),"receiver":request["recipient"]}
