"""Real funds transports. Capability absence never fabricates a receipt."""
from __future__ import annotations

import re
import time
from decimal import Decimal, localcontext
from types import SimpleNamespace

from .contracts import FinancialError,amount


def usd_price(symbol,*,transport=None):
    from ..connectors.dex_base import UrllibHttp
    http=transport or UrllibHttp()
    if not re.fullmatch(r"[A-Z0-9._-]{1,20}",symbol):raise FinancialError("valuation_asset_not_supported",422)
    status,doc=http.request("GET",f"https://api.coinbase.com/v2/exchange-rates?currency={symbol}",timeout=15)
    if status!=200 or (doc.get("data") or {}).get("currency")!=symbol:raise FinancialError("financial_valuation_unavailable",503)
    rate=(doc.get("data") or {}).get("rates",{}).get("USD")
    return amount(rate)


def owned_wallet(config,request):
    from ..wallet.bindings import resolve_binding
    from ..wallet.registry import build_provider
    wid,name,raw=resolve_binding(config,request)
    provider=build_provider(name,raw,workspace=config.paths.root,vault_passphrase=getattr(config,"vault_passphrase",None))
    return wid,name,raw,provider


def validate_evm_address(value):
    if not isinstance(value,str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}",value) or int(value,16)==0:
        raise FinancialError("invalid_evm_address",400)
    from eth_utils import to_checksum_address
    return to_checksum_address(value)


def units(value,decimals):
    if not isinstance(decimals,int) or not 0<=decimals<=255:
        raise FinancialError("asset_decimals_invalid",400)
    quantity=amount(value,zero=True)
    with localcontext() as context:
        context.prec=max(160,len(quantity.as_tuple().digits)+decimals+2)
        number=quantity.scaleb(decimals)
        if number!=number.to_integral_value() or number>=2**256-1:
            raise FinancialError("asset_amount_precision_or_unlimited",400)
        return int(number)


class CexFunds:
    def __init__(self,config,request,*,connector=None):
        self.config=config
        from ..trading.accounts import get_account_profile
        self.profile=get_account_profile(config.paths,request["account_id"])
        if connector is None:
            from ..skills._connector_helpers import account_connector
            connector=account_connector(SimpleNamespace(config=config,extras={}),request["account_id"])
        from ..connectors.ccxt_adapter import CcxtConnector
        if not isinstance(connector,CcxtConnector):raise FinancialError("funds_require_ccxt_venue",422)
        self.connector=connector

    def quote(self,request):
        client=self.connector.client;client.load_markets()
        currency=client.currencies.get(request["asset"])
        if not currency:raise FinancialError("unknown_venue_asset",422)
        caps=self.connector.funds_capabilities()
        if not caps.get("withdraw" if request["kind"]=="withdraw" else "transfer") or not caps.get('fetchWithdrawals' if request['kind']=='withdraw' else 'fetchTransfers'):
            raise FinancialError("unsupported_financial_capability",422)
        balance=client.fetch_balance();free=(balance.get("free") or {}).get(request["asset"])
        if free is None:raise FinancialError("asset_balance_unavailable",503)
        quantity=amount(request["amount"])
        if Decimal(str(client.currency_to_precision(request['asset'],request['amount'])))!=quantity:
            raise FinancialError('asset_amount_precision',400)
        fee=Decimal(0)
        if request["kind"]=="withdraw":
            networks=currency.get("networks") or {}
            network=networks.get(request.get("chain"))
            if not network or network.get("withdraw") is not True:raise FinancialError("withdrawal_network_unsupported",422)
            if network.get("percentage"):raise FinancialError("percentage_withdrawal_fee_unsupported",422)
            fee=amount(network.get("fee"),zero=True)
            if network.get("memo") and not request.get("memo"):raise FinancialError("withdrawal_memo_required",400)
            client.check_address(request['recipient'])
            destination=self.config.get('financial.withdrawal_networks',{}).get(self.profile.venue,{}).get(request['chain'])
            if not destination or destination.get('decimals') is None:raise FinancialError('withdrawal_destination_verifier_unconfigured',422)
            if destination.get('memo_required') and not request.get('memo'):raise FinancialError('withdrawal_memo_required',400)
            if quantity<=fee:raise FinancialError('withdrawal_amount_below_fee',400)
        price=usd_price(request["asset"])
        return {"risk_usd":str(quantity*price),"spend_usd":str(quantity*price),"fee_usd":str(fee*price),
            "asset_amounts":{request["asset"]:str(quantity+fee)},"available_asset_amounts":{request["asset"]:str(amount(free,zero=True))},
            "fee_asset":str(fee),"price_usd":str(price),"expires_at":time.time()+60,"since":int(time.time()*1000),
            **({'destination':destination,'minimum_received_base':str(units(quantity-fee,int(destination['decimals'])))} if request['kind']=='withdraw' else {})}

    def validate(self,request,quote):
        from ..trading.accounts import get_account_profile
        profile=get_account_profile(self.config.paths,request["account_id"])
        if not profile.is_real_money or not profile.live_trading_enabled or profile.status!="active":raise FinancialError("financial_account_not_live",403)
        policies=self.config.get("financial.account_permissions",{}).get(request["account_id"],{})
        key="withdraw" if request["kind"]=="withdraw" else "transfer"
        if policies.get(key) is not True:raise FinancialError("account_funds_permission_disabled",403)
        if request["kind"]=="withdraw" and not profile.permissions.withdraw:raise FinancialError("account_withdrawal_permission_disabled",403)
        if quote["expires_at"]<=time.time():raise FinancialError("financial_quote_expired")

    def execute(self,request,quote,submitted):
        self.validate(request,quote)
        # Exchanges with no client id still retain the attempt marker. A lost
        # response is unconfirmed and is never repeated automatically.
        ref={"kind":request["kind"],"since":quote["since"]}
        submitted(ref)
        if request["kind"]=="withdraw":
            row=self.connector.withdraw_assets(request["asset"],request["amount"],request["recipient"],memo=request.get("memo"),network=request.get("chain"))
        else:row=self.connector.transfer_assets(request["asset"],request["amount"],request["from_account"],request["to_account"])
        ref.update(id=row.get("id"),transaction_hash=row.get("txid"));submitted(ref)
        return {"state":"submitted","submission":ref,"provider_status":row.get("status")}

    def status(self,request,quote,submission):
        if not submission.get("id"):return {"state":"unconfirmed","reason":"venue_reference_missing"}
        row=self.connector.funds_status(request["kind"],request["asset"],submission)
        if not row:return {"state":"unconfirmed","reason":"venue_receipt_not_found"}
        if row.get('currency')!=request['asset'] or amount(row.get('amount'),zero=True)!=amount(request['amount']):return {'state':'needs_recovery','reason':'venue_funds_receipt_mismatch'}
        if row.get('status') in {'failed','canceled','rejected'}:return {'state':'rejected','venue_id':row.get('id')}
        if request['kind']=='exchange_transfer' and (row.get('fromAccount')!=request['from_account'] or row.get('toAccount')!=request['to_account']):return {'state':'needs_recovery','reason':'transfer_accounting_mismatch'}
        complete=row.get("status")=="ok" and request["kind"]=="exchange_transfer"
        credit=None
        if request['kind']=='withdraw' and row.get('status')=='ok':
            if row.get('address')!=request['recipient']:return {'state':'needs_recovery','reason':'withdrawal_destination_mismatch'}
            if row.get('txid'):
                from .credit import verify_credit
                credit=verify_credit(quote['destination'],receiver=request['recipient'],transaction_hash=row['txid'],minimum_base=quote['minimum_received_base'])
                complete=credit.get('confirmed') is True
        # Withdrawal 'ok' proves venue completion, not destination credit.
        return {"state":"confirmed" if complete else "confirming","venue_completed":row.get("status")=="ok",
                "transaction_hash":row.get("txid"),"venue_id":row.get("id"),'destination_credit':credit,
                **({'state':'needs_recovery','reason':'destination_credit_verification_failed'} if credit and credit.get('failed') else {})}


class EvmFunds:
    def __init__(self,config,request,*,connector=None,provider=None,wallet_config=None):
        self.config=config
        if provider is None:_,name,raw,provider=owned_wallet(config,request)
        else:raw=wallet_config or {};name=getattr(provider,"id","")
        if not callable(getattr(provider,"_resolve_signer_key",None)):raise FinancialError("wallet_owned_signer_unavailable",422)
        self.provider=provider;self.raw=raw;self.chain=str(request.get("chain") or "")
        from ..connectors.evm_native import EVMNative
        from ..connectors.chains import ChainRegistry
        chain_spec=ChainRegistry(config).get(self.chain)
        self.chain=chain_spec.name;self.chain_spec=chain_spec
        self.sender=validate_evm_address(str(raw.get("address") or ""))
        if connector is None:
            rpc=(raw.get("rpc_urls") or {}).get(self.chain)
            if not rpc:
                for name,url in (raw.get("rpc_urls") or {}).items():
                    try:match=ChainRegistry(config).get(name).name==self.chain
                    except Exception:continue
                    if match:rpc=url;break
            if not rpc:raise FinancialError("wallet_rpc_unconfigured",422)
            connector=EVMNative(chain=self.chain,chain_id=chain_spec.chain_id,rpc_url=rpc,live=True)
        self.connector=connector

    def asset(self,asset):
        native=asset.upper()=="NATIVE"
        address=None if native else validate_evm_address(asset)
        decimals=18 if native else self.connector.get_erc20_decimals(address)
        symbol=self.raw.get("native_symbol") if native else next((v for k,v in (self.raw.get("token_symbols") or {}).items() if k.lower()==address.lower()),None)
        if not symbol:raise FinancialError("asset_valuation_symbol_unconfigured",422)
        return native,address,decimals,symbol

    def quote(self,request):
        native,token,decimals,symbol=self.asset(request["asset"])
        quantity=units(request["amount"],decimals)
        if request["kind"]=="contract_approval":
            if native:raise FinancialError("native_asset_has_no_allowance",422)
            destination=validate_evm_address(request["spender"])
            data="0x095ea7b3"+destination[2:].lower().rjust(64,"0")+hex(quantity)[2:].rjust(64,"0")
            tx={"from":self.sender,"to":token,"value":"0x0","data":data}
        elif native:tx={"from":self.sender,"to":validate_evm_address(request["recipient"]),"value":hex(quantity),"data":"0x"}
        else:
            destination=validate_evm_address(request["recipient"])
            data="0xa9059cbb"+destination[2:].lower().rjust(64,"0")+hex(quantity)[2:].rjust(64,"0")
            tx={"from":self.sender,"to":token,"value":"0x0","data":data}
        self.connector._verify_chain_id()
        gas=int(self.connector._rpc("eth_estimateGas",[tx]),16)
        gas=(gas*120+99)//100;gas_price=int(self.connector._rpc("eth_gasPrice",[]),16)
        fee_units=Decimal(gas*gas_price)/Decimal(10**18)
        native_balance=Decimal(int(self.connector._rpc("eth_getBalance",[self.sender,"latest"]),16))/Decimal(10**18)
        if native:balance=native_balance
        else:
            result=self.connector._rpc("eth_call",[{"to":token,"data":"0x70a08231"+self.sender[2:].lower().rjust(64,"0")},"latest"])
            balance=Decimal(int(result,16))/Decimal(10**decimals)
        price=usd_price(symbol);native_price=usd_price(self.raw.get("native_symbol") or "ETH")
        value=amount(request["amount"],zero=True)*price
        movement=Decimal(0) if request["kind"]=="contract_approval" else amount(request["amount"])
        assets={"NATIVE":str(fee_units+movement if native else fee_units)}
        available={"NATIVE":str(native_balance)}
        if not native and movement:assets[request["asset"]]=str(movement);available[request["asset"]]=str(balance)
        return {"risk_usd":str(value),"spend_usd":str(movement*price),"fee_usd":str(fee_units*native_price),
            "asset_amounts":assets,"available_asset_amounts":available,"transaction":tx,"gas_limit":gas,
            "gas_price_wei":str(gas_price),'native_price_usd':str(native_price),"chain_id":self.chain_spec.chain_id,"expires_at":time.time()+60,
            "sender":self.sender,"steps":[{"spender":request["spender"]}] if request["kind"]=="contract_approval" else []}

    def validate(self,request,quote):
        if not self.config.get("financial.wallet_permissions",{}).get(request["wallet_id"],{}).get(request["kind"],False):
            raise FinancialError("wallet_funds_permission_disabled",403)
        if quote["expires_at"]<=time.time():raise FinancialError("financial_quote_expired")
        self.connector._verify_chain_id()
        tx=quote["transaction"]
        if tx["from"]!=self.sender:raise FinancialError("wallet_sender_mismatch",403)
        native,token,decimals,_=self.asset(request["asset"])
        if request["kind"]=="contract_approval":
            expected="0x095ea7b3"+validate_evm_address(request["spender"])[2:].lower().rjust(64,"0")+hex(units(request["amount"],decimals))[2:].rjust(64,"0")
            if tx["to"]!=token or tx["data"]!=expected:raise FinancialError("allowance_plan_mismatch",403)
        elif native:
            if tx["to"]!=validate_evm_address(request["recipient"]) or int(tx["value"],16)!=units(request["amount"],decimals):raise FinancialError("transfer_plan_mismatch",403)
        else:
            expected="0xa9059cbb"+validate_evm_address(request["recipient"])[2:].lower().rjust(64,"0")+hex(units(request["amount"],decimals))[2:].rjust(64,"0")
            if tx["to"]!=token or tx["data"]!=expected:raise FinancialError("transfer_plan_mismatch",403)

    def execute(self,request,quote,submitted):
        from eth_account import Account
        self.validate(request,quote)
        key=self.provider._resolve_signer_key()
        try:
            if Account.from_key(key).address!=self.sender:raise FinancialError("wallet_signer_mismatch",403)
            tx=quote["transaction"]
            result=self.connector.send_raw_transaction(to=tx["to"],data=tx["data"],value=int(tx["value"],16),signer_private_key=key,
                gas_limit=quote["gas_limit"],gas_price_wei=int(quote["gas_price_wei"]),confirm=False,
                on_broadcast=lambda ref:submitted({"transaction_hash":ref["tx_hash"],"chain":self.chain,"nonce":ref["nonce"]}))
        finally:key=""
        return {"state":"submitted","submission":{"transaction_hash":result["tx_hash"],"chain":self.chain}}

    def status(self,request,quote,submission):
        ref=submission.get("transaction_hash")
        if not ref:return {"state":"unconfirmed"}
        receipt=self.connector._rpc("eth_getTransactionReceipt",[ref])
        if not receipt:return {"state":"confirming"}
        if int(receipt.get("status","0x0"),16)!=1:
            required=int(self.config.get('financial.confirmations',{}).get(request['chain'],12))
            if self.connector.get_block_number()-int(receipt['blockNumber'],16)+1<required:return {'state':'confirming','transaction_hash':ref}
            if receipt.get('gasUsed') and receipt.get('effectiveGasPrice') and quote.get('native_price_usd'):
                fee=Decimal(int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16))/Decimal(10**18)*amount(quote['native_price_usd'])
                return {'state':'rejected','reason':'chain_reverted','transaction_hash':ref,'settled_notional_usd':'0','settled_fee_usd':str(fee)}
            return {"state":"needs_recovery","reason":"chain_reverted_fee_unverified","transaction_hash":ref}
        tx=self.connector._rpc("eth_getTransactionByHash",[ref])
        planned=quote["transaction"]
        if not tx or tx.get("from","").lower()!=self.sender.lower() or tx.get("to","").lower()!=planned["to"].lower() or tx.get("input","0x").lower()!=planned["data"].lower() or int(tx.get("value","0x0"),16)!=int(planned["value"],16):
            return {"state":"needs_recovery","reason":"transaction_parameters_mismatch","transaction_hash":ref}
        required=int(self.config.get("financial.confirmations",{}).get(request["chain"],12))
        if self.connector.get_block_number()-int(receipt["blockNumber"],16)+1<required:return {"state":"confirming","transaction_hash":ref}
        native,token,decimals,_=self.asset(request["asset"])
        if request["kind"]=="wallet_transfer" and not native:
            from eth_utils import keccak
            topic="0x"+keccak(text="Transfer(address,address,uint256)").hex()
            logs=receipt.get("logs") or []
            matched=any(log.get("address","").lower()==token.lower() and len(log.get("topics",[]))>=3
                and log["topics"][0].lower()==topic.lower() and log["topics"][1][-40:].lower()==self.sender[2:].lower()
                and log["topics"][2][-40:].lower()==request["recipient"][2:].lower()
                and int(log.get("data","0x0"),16)==units(request["amount"],decimals) for log in logs)
            if not matched:return {"state":"needs_recovery","reason":"token_transfer_event_missing","transaction_hash":ref}
        if request["kind"]=="contract_approval":
            call="0xdd62ed3e"+self.sender[2:].lower().rjust(64,"0")+request["spender"][2:].lower().rjust(64,"0")
            observed=int(self.connector._rpc("eth_call",[{"to":request["asset"],"data":call},receipt["blockNumber"]]),16)
            if observed!=units(request["amount"],self.asset(request["asset"])[2]):return {"state":"needs_recovery","reason":"allowance_state_mismatch"}
        return {"state":"confirmed","transaction_hash":ref,"block_number":receipt["blockNumber"],"observed_parameters":True}


def adapter_for(config,request,*,quote=None,recovery=False):
    from ..trading.components import trading_components
    component=trading_components(config).resolve(request,binding=(quote or {}).get("component_binding"),recovery=recovery)
    return component.build(config,request)


def financial_capabilities(config):
    from ..wallet.registry import list_configured_providers
    from ..trading.accounts import load_account_profiles
    rows=[]
    for profile in load_account_profiles(config.paths).values():
        if profile.kind!='cex':continue
        has={}
        try:
            import ccxt
            venue=getattr(ccxt,profile.venue,None)
            if venue:has=venue().has
        except (ImportError,AttributeError):pass
        for kind,execute,query in (('trade','createOrder','fetchOrder'),('exchange_transfer','transfer','fetchTransfers'),('withdraw','withdraw','fetchWithdrawals')):
            supported=has.get(execute) is True and has.get(query) is True
            if kind=='trade':
                from ..connectors.provider_spec import get_registry
                spec=get_registry(config.paths.root).find(profile.venue)
                supported=supported and bool(spec and spec.supports.get('place_order'))
            permission=config.get('financial.account_permissions',{}).get(profile.id,{}).get('withdraw' if kind=='withdraw' else 'transfer',False) if kind!='trade' else profile.can_place_order
            rows.append({'resource_id':profile.id,'resource_type':'account','provider':profile.venue,'kind':kind,
                'supported':supported,'permission_enabled':bool(permission),'live_verified':False,
                'ready':supported and bool(permission) and config.get('financial.enabled',False) and profile.is_real_money and profile.live_trading_enabled,
                'reason':None if supported else 'provider_missing_write_or_status_capability'})
    for binding in list_configured_providers(config.data):
        wid=binding['wallet_id'];provider=binding['provider'];raw=binding.get('config') or {}
        owned=provider in {'self_custody','metamask','evm_v2'} and bool(raw.get('signer_ref') or raw.get('keypair_path'))
        for kind in ('swap','wallet_transfer','contract_approval','bridge_swap'):
            supported=owned and bool(raw.get('rpc_urls'))
            reason=None if supported else 'owned_signer_or_rpc_unavailable'
            if kind=='bridge_swap' and not all(config.get('financial.lifi.'+key) for key in ('allowed_bridges','allowed_routers','selectors')):
                supported=False;reason='bridge_routes_not_reviewed'
            permission=config.get('financial.wallet_permissions',{}).get(wid,{}).get(kind,False)
            rows.append({'resource_id':wid,'resource_type':'wallet','provider':provider,'kind':kind,
                'supported':supported,'permission_enabled':bool(permission),'live_verified':False,'reason':reason,
                'ready':supported and bool(permission) and config.get('financial.enabled',False)})
    return rows
