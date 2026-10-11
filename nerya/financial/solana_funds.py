"""Owned SOL/SPL transfers and finite delegates using the existing signer."""
from __future__ import annotations
import base64
import hashlib
import struct
import time
from decimal import Decimal

from .contracts import FinancialError,amount
from .adapters import owned_wallet,usd_price,units

SYSTEM="11111111111111111111111111111111"
TOKEN="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED="ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def pubkey(value):
    import base58
    try:raw=base58.b58decode(value)
    except Exception:raise FinancialError("invalid_solana_address",400)
    if len(raw)!=32:raise FinancialError("invalid_solana_address",400)
    return raw


def shortvec(value):
    output=bytearray()
    while True:
        byte=value&127;value>>=7;output.append(byte|(128 if value else 0))
        if not value:return bytes(output)


def associated_address(owner,mint):
    import base58
    # PDA rejection uses Edwards decompression, including non-prime-subgroup
    # points. libsodium's validity check is stricter and gives wrong bumps.
    field=2**255-19;d=(-121665*pow(121666,field-2,field))%field
    for bump in range(255,-1,-1):
        candidate=hashlib.sha256(pubkey(owner)+pubkey(TOKEN)+pubkey(mint)+bytes([bump])+pubkey(ASSOCIATED)+b"ProgramDerivedAddress").digest()
        y=(int.from_bytes(candidate,'little')&((1<<255)-1))%field
        denominator=(d*y*y+1)%field
        x2=((y*y-1)*pow(denominator,field-2,field))%field if denominator else None
        on_curve=x2 is not None and (x2==0 or pow(x2,(field-1)//2,field)==1)
        if not on_curve:return base58.b58encode(candidate).decode()
    raise FinancialError("associated_account_derivation_failed")


def message(payer,blockhash,instructions):
    flags={payer:True};readonly=[]
    for instruction in instructions:
        for key,writable in instruction["accounts"]:
            if key==payer:continue
            flags[key]=flags.get(key,False) or writable
        flags.setdefault(instruction["program"],False)
    keys=[payer]+[k for k,v in flags.items() if k!=payer and v]+[k for k,v in flags.items() if k!=payer and not v]
    indexes={key:i for i,key in enumerate(keys)}
    if len(keys)>255:raise FinancialError("too_many_solana_accounts",400)
    header=bytes([1,0,sum(not flags[k] for k in keys)])
    raw=header+shortvec(len(keys))+b"".join(pubkey(k) for k in keys)+pubkey(blockhash)+shortvec(len(instructions))
    for instruction in instructions:
        data=base64.b64decode(instruction["data"])
        accounts=bytes(indexes[k] for k,_ in instruction["accounts"])
        raw+=bytes([indexes[instruction["program"]]])+shortvec(len(accounts))+accounts+shortvec(len(data))+data
    return raw


def instruction(program,accounts,data):
    return {"program":program,"accounts":accounts,"data":base64.b64encode(data).decode()}


class SolanaFunds:
    def __init__(self,config,request,*,connector=None,provider=None,wallet_config=None):
        self.config=config
        if provider is None:_,_,raw,provider=owned_wallet(config,request)
        else:raw=wallet_config or {}
        if not callable(getattr(provider,"_resolve_signer_key",None)):raise FinancialError("wallet_owned_signer_unavailable",422)
        self.provider=provider;self.raw=raw
        self.sender=str(raw.get("address") or "");pubkey(self.sender)
        if connector is None:
            from ..connectors.solana_native import SolanaNative
            rpc=(raw.get("rpc_urls") or {}).get("solana")
            if not rpc:raise FinancialError("wallet_rpc_unconfigured",422)
            connector=SolanaNative(rpc_url=rpc,live=True)
        self.connector=connector

    def token_account(self,owner,mint,*,quantity=None):
        result=self.connector._rpc("getTokenAccountsByOwner",[owner,{"mint":mint},{"encoding":"jsonParsed","commitment":"confirmed"}])
        for row in (result or {}).get("value",[]):
            info=((row.get("account") or {}).get("data") or {}).get("parsed",{}).get("info",{})
            if info.get("owner")!=owner or info.get("mint")!=mint or row["account"].get("owner")!=TOKEN:continue
            balance=amount((info.get("tokenAmount") or {}).get("uiAmountString"),zero=True)
            if quantity is None or balance>=quantity:return row["pubkey"],info,balance
        return None,None,None

    def instructions(self,request):
        target=request.get("recipient") or request.get("spender");pubkey(target)
        native=request["asset"].upper() in {"SOL","NATIVE"}
        qty=amount(request["amount"],zero=request["kind"]=="contract_approval")
        if native:
            if request["kind"]=="contract_approval":raise FinancialError("native_asset_has_no_delegate",422)
            value=units(qty,9)
            if value>=2**64:raise FinancialError("solana_amount_overflow",400)
            return [instruction(SYSTEM,[(self.sender,True),(target,True)],struct.pack("<IQ",2,value))],9,False
        mint=request["asset"];pubkey(mint)
        supply=self.connector._rpc("getTokenSupply",[mint,{"commitment":"confirmed"}])
        decimals=int(supply["value"]["decimals"]);value=units(qty,decimals)
        if value>=2**64-1:raise FinancialError("solana_amount_overflow_or_unlimited",400)
        source,info,balance=self.token_account(self.sender,mint,quantity=qty if request["kind"]!="contract_approval" else None)
        if not source:raise FinancialError("sufficient_owned_token_account_required",422)
        if request["kind"]=="contract_approval":
            if value==0:return [instruction(TOKEN,[(source,True),(self.sender,False)],bytes([5]))],decimals,False
            return [instruction(TOKEN,[(source,True),(mint,False),(target,False),(self.sender,False)],bytes([13])+struct.pack("<QB",value,decimals))],decimals,False
        destination,_,_=self.token_account(target,mint)
        out=[];created=not destination
        if created:
            destination=associated_address(target,mint)
            out.append(instruction(ASSOCIATED,[(self.sender,True),(destination,True),(target,False),(mint,False),(SYSTEM,False),(TOKEN,False)],bytes([1])))
        out.append(instruction(TOKEN,[(source,True),(mint,False),(destination,True),(self.sender,False)],bytes([12])+struct.pack("<QB",value,decimals)))
        return out,decimals,created

    def quote(self,request):
        instructions,decimals,created=self.instructions(request)
        latest=self.connector._rpc("getLatestBlockhash",[{"commitment":"confirmed"}])["value"]
        raw=message(self.sender,latest["blockhash"],instructions)
        unsigned=base64.b64encode(bytes([1])+bytes(64)+raw).decode()
        simulation=self.connector._rpc("simulateTransaction",[unsigned,{"encoding":"base64","sigVerify":False,"replaceRecentBlockhash":True}])
        if simulation["value"]["err"] is not None:raise FinancialError("solana_simulation_failed",422)
        fee=self.connector._rpc("getFeeForMessage",[base64.b64encode(raw).decode(),{"commitment":"confirmed"}])["value"]
        if fee is None:raise FinancialError("solana_fee_unavailable",503)
        rent=int(self.connector._rpc("getMinimumBalanceForRentExemption",[165])) if created else 0
        fee_sol=Decimal(fee+rent)/Decimal(10**9)
        native_balance=Decimal(self.connector._rpc("getBalance",[self.sender,{"commitment":"confirmed"}])["value"])/Decimal(10**9)
        native=request["asset"].upper() in {"SOL","NATIVE"}
        symbol="SOL" if native else (self.raw.get("token_symbols") or {}).get(request["asset"])
        if not symbol:raise FinancialError("asset_valuation_symbol_unconfigured",422)
        qty=amount(request["amount"],zero=True);movement=Decimal(0) if request["kind"]=="contract_approval" else qty
        price=usd_price(symbol);sol_price=usd_price("SOL")
        assets={"NATIVE":str(fee_sol+movement if native else fee_sol)};available={"NATIVE":str(native_balance)}
        if not native and movement:
            _,_,balance=self.token_account(self.sender,request["asset"],quantity=qty)
            assets[request["asset"]]=str(qty);available[request["asset"]]=str(balance)
        return {"risk_usd":str(qty*price),"spend_usd":str(movement*price),"fee_usd":str(fee_sol*sol_price),
            "asset_amounts":assets,"available_asset_amounts":available,"instructions":instructions,
            "unsigned_transaction":unsigned,"sender":self.sender,"decimals":decimals,"blockhash":latest["blockhash"],
            "last_valid_block_height":latest["lastValidBlockHeight"],"expires_at":time.time()+45,
            "steps":[{"spender":request["spender"]}] if request["kind"]=="contract_approval" else []}

    def validate(self,request,quote):
        if not self.config.get("financial.wallet_permissions",{}).get(request["wallet_id"],{}).get(request["kind"],False):raise FinancialError("wallet_funds_permission_disabled",403)
        if quote["expires_at"]<=time.time():raise FinancialError("financial_quote_expired")
        if self.connector._rpc("getBlockHeight",[{"commitment":"confirmed"}])>quote["last_valid_block_height"]:raise FinancialError("solana_blockhash_expired")
        expected,_,_=self.instructions(request)
        if expected!=quote["instructions"] or quote["sender"]!=self.sender:raise FinancialError("solana_instruction_plan_mismatch",403)
        raw=message(self.sender,quote["blockhash"],quote["instructions"])
        if base64.b64encode(bytes([1])+bytes(64)+raw).decode()!=quote["unsigned_transaction"]:raise FinancialError("solana_transaction_plan_mismatch",403)

    def execute(self,request,quote,submitted):
        from ..connectors.solana_native import _sign_solana_v0_tx,_pubkey_from_signer,_read_shortvec_u16
        import base58
        self.validate(request,quote)
        key=self.provider._resolve_signer_key()
        try:
            if _pubkey_from_signer(key)!=self.sender:raise FinancialError("wallet_signer_mismatch",403)
            signed=_sign_solana_v0_tx(quote["unsigned_transaction"],key)
        finally:key=""
        raw=base64.b64decode(signed);_,offset=_read_shortvec_u16(raw,0)
        signature=base58.b58encode(raw[offset:offset+64]).decode()
        ref={"transaction_hash":signature,"chain":"solana","last_valid_block_height":quote["last_valid_block_height"]}
        submitted(ref)
        observed=self.connector._rpc("sendTransaction",[signed,{"encoding":"base64","skipPreflight":False,"maxRetries":0}])
        if observed!=signature:raise FinancialError("solana_submission_signature_mismatch")
        return {"state":"submitted","submission":ref}

    def status(self,request,quote,submission):
        signature=submission.get("transaction_hash")
        if not signature:return {"state":"unconfirmed"}
        status=self.connector._rpc("getSignatureStatuses",[[signature],{"searchTransactionHistory":True}])["value"][0]
        if not status:return {"state":"unconfirmed"}
        if status.get("err") is not None:return {"state":"needs_recovery","reason":"solana_transaction_failed"}
        if status.get("confirmationStatus")!="finalized":return {"state":"confirming"}
        tx=self.connector._rpc("getTransaction",[signature,{"encoding":"base64","maxSupportedTransactionVersion":0,"commitment":"finalized"}])
        if not tx or (tx.get("meta") or {}).get("err") is not None:return {"state":"needs_recovery","reason":"solana_receipt_missing_or_failed"}
        import base58
        from ..connectors.solana_native import _read_shortvec_u16
        observed=base64.b64decode(tx["transaction"][0]);n,offset=_read_shortvec_u16(observed,0)
        if base58.b58encode(observed[offset:offset+64]).decode()!=signature or observed[offset+64*n:]!=message(self.sender,quote["blockhash"],quote["instructions"]):
            return {"state":"needs_recovery","reason":"solana_receipt_instruction_mismatch"}
        return {"state":"confirmed","transaction_hash":signature,"slot":tx["slot"],"finalized":True,"instructions":quote["instructions"]}
