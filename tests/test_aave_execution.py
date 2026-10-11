from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import time

import pytest
from eth_abi import encode
from eth_utils import keccak

from nerya.core.config import Config,DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.financial.contracts import FinancialContext,FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.financial.defi.aave import AaveFunds,RAY
from nerya.financial.defi.book import ProtocolBook
from nerya.financial.defi.calls import ZERO,calldata,topic_address

pytestmark=pytest.mark.smoke
SENDER="0x"+"1"*40
ASSET="0x"+"2"*40
POOL="0x"+"3"*40
DATA="0x"+"4"*40
ORACLE="0x"+"5"*40
ATOKEN="0x"+"6"*40
DEBT="0x"+"7"*40
IMPLEMENTATION="0x"+"8"*40
FEED="0x"+"9"*40
BLOCK_HASH="0x"+"a"*64
TX_HASH="0x"+"b"*64
CODE="0x6001"
CODE_HASH="0x"+keccak(bytes.fromhex(CODE[2:])).hex()


class Rpc:
    allowance=10**12
    collateral=10000
    debt=1000
    supply=100*10**6
    reserve_debt=0
    paused=False
    stale=False
    emode=0
    receipt=None
    transaction=None
    sends=0

    def _verify_chain_id(self):pass
    def get_erc20_decimals(self,_):return 6
    def get_block_number(self):return 102
    def send_raw_transaction(self,*,to,data,value,signer_private_key,gas_limit,gas_price_wei,confirm,on_broadcast):
        from eth_account import Account
        assert Account.from_key(signer_private_key).address==SENDER
        self.sends+=1
        on_broadcast({"tx_hash":TX_HASH,"nonce":0})
        return {"tx_hash":TX_HASH}
    def _rpc(self,method,args):
        if method=="eth_chainId":return hex(8453)
        if method=="eth_getCode":return CODE
        if method=="eth_getStorageAt":return "0x"+IMPLEMENTATION[2:].rjust(64,"0")
        if method=="eth_estimateGas":return hex(180000)
        if method=="eth_gasPrice":return hex(10**9)
        if method=="eth_getBalance":return hex(10**18)
        if method=="eth_getTransactionReceipt":return self.receipt
        if method=="eth_getTransactionByHash":return self.transaction
        if method=="eth_getBlockByNumber":return {"hash":BLOCK_HASH}
        if method=="eth_call":
            selector=args[0]["data"][:10]
            config=8000|(8500<<16)|(6<<48)|(1<<56)|(1<<58)|((1 if self.paused else 0)<<60)
            cases={
                "BASE_CURRENCY()":(["address"],[ZERO]),
                "BASE_CURRENCY_UNIT()":(["uint256"],[10**8]),
                "getAssetPrice(address)":(["uint256"],[10**8]),
                "getSourceOfAsset(address)":(["address"],[FEED]),
                "latestRoundData()":(["uint80","int256","uint256","uint256","uint80"],[1,10**8,1,int(time.time())-(7200 if self.stale else 0),1]),
                "getUserAccountData(address)":(["uint256"]*6,[self.collateral*10**8,self.debt*10**8,5000*10**8,8500,8000,int(self.collateral*.85/self.debt*10**18) if self.debt else 2**256-1]),
                "getUserReserveData(address,address)":(["uint256"]*7+["uint40","bool"],[self.supply,0,self.reserve_debt,0,self.reserve_debt,0,0,0,True]),
                "getReserveTokensAddresses(address)":(["address"]*3,[ATOKEN,ZERO,DEBT]),
                "scaledBalanceOf(address)":(["uint256"],[self.supply]),
                "getReserveNormalizedIncome(address)":(["uint256"],[RAY]),
                "getReserveNormalizedVariableDebt(address)":(["uint256"],[RAY]),
                "getConfiguration(address)":(["uint256"],[config]),
                "getUserEMode(address)":(["uint256"],[self.emode]),
                "balanceOf(address)":(["uint256"],[1000*10**6]),
                "allowance(address,address)":(["uint256"],[self.allowance]),
            }
            for signature,(types,values) in cases.items():
                if selector=="0x"+keccak(text=signature)[:4].hex():return "0x"+encode(types,values).hex()
        raise AssertionError((method,args))


def setup(tmp_path,monkeypatch):
    data=deepcopy(DEFAULT_CONFIG)
    data["runtime"]["live_trading_enabled"]=True
    deployment={"reviewed":True,"contracts":{"pool":POOL,"data_provider":DATA,"oracle":ORACLE},
        "code_hashes":{name:CODE_HASH for name in ("pool","data_provider","oracle")},
        "implementations":{"pool":{"address":IMPLEMENTATION,"code_hash":CODE_HASH}},"oracle_sources":{ASSET.lower():FEED}}
    data["financial"]={"enabled":True,"wallet_permissions":{"wallet":{kind:True for kind in ("lend_supply","lend_withdraw","borrow","repay")}},
        "portfolio_limits":{"wallet:wallet":{"min_health_factor":"1.5","max_debt_usd":"6000"}},
        "defi":{"max_gas_limit":500000,"deployments":{"base":{"aave_v3":deployment}}}}
    raw={"address":SENDER,"signer_ref":"vault://test-ref","token_symbols":{ASSET:"USDC"},"native_symbol":"ETH"}
    data["wallet"]={"providers":{"wallet":{"provider":"self_custody","config":raw}}}
    config=Config(WorkspacePaths(tmp_path),data)
    rpc=Rpc()
    provider=SimpleNamespace(id="self_custody",_resolve_signer_key=lambda:"unused")
    real=AaveFunds
    monkeypatch.setattr("nerya.financial.defi.aave.AaveFunds",lambda c,r:real(c,r,connector=rpc,provider=provider,wallet_config=raw))
    monkeypatch.setattr("nerya.financial.defi.transport.usd_price",lambda _:Decimal(2000))
    ctx=FinancialContext("operator",frozenset({"api:all"}))
    return config,rpc,provider,ctx


def request(kind="lend_supply",amount="10"):
    return {"kind":kind,"component_id":"builtin:aave","wallet_id":"wallet","protocol":"aave_v3","chain":"base","asset":ASSET,"amount":amount}


def test_real_quote_reads_chain_state_and_frozen_call(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch)
    action=FinancialGateway(cfg).prepare(ctx,request(),action_key="supply")
    quote=action["quote"]
    assert quote["aave_before"]["debt_usd"]=="1000"
    assert quote["aave_before"]["health_factor"]=="8.5"
    assert quote["risk_usd"]=="10"
    assert quote["asset_amounts"][ASSET]=="10"
    assert quote["transaction"]["to"]==POOL
    assert quote["component_binding"]["id"]=="builtin:aave"


def test_missing_allowance_becomes_reviewable_prerequisite(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch);rpc.allowance=0
    gateway=FinancialGateway(cfg)
    action=gateway.prepare(ctx,request(),action_key="supply")
    result=gateway.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    assert result["status"]=="prerequisite_required"
    assert result["required_action"]["amount"]=="10"
    assert result["required_action"]["spender"]==POOL
    assert gateway.store.get_action(action["action_id"],ctx)["state"]=="awaiting_prerequisite"


@pytest.mark.parametrize("attribute,value,reason",[("paused",True,"inactive_or_paused"),("stale",True,"evidence_stale"),("emode",1,"emode_requires")])
def test_unknown_or_risky_state_never_reaches_submission(tmp_path,monkeypatch,attribute,value,reason):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch);setattr(rpc,attribute,value)
    with pytest.raises(FinancialError,match=reason):FinancialGateway(cfg).prepare(ctx,request(),action_key="blocked")


def test_borrow_stress_and_post_action_health_are_enforced(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch);rpc.collateral=2000;rpc.debt=1000
    with pytest.raises(FinancialError,match="health_factor_below"):
        FinancialGateway(cfg).prepare(ctx,request("borrow","500"),action_key="borrow")


def test_withdraw_cannot_claim_another_strategy_supply(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch)
    with pytest.raises(FinancialError,match="exceeds_owned_claim"):
        FinancialGateway(cfg).prepare(ctx,request("lend_withdraw","10"),action_key="withdraw")


def test_confirmed_receipt_books_once_and_survives_duplicate(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch)
    gateway=FinancialGateway(cfg);action=gateway.prepare(ctx,request(),action_key="supply")
    quote=gateway.store.get_action(action["action_id"],ctx,internal=True)["quote"]
    topic="0x"+keccak(text="Supply(address,address,address,uint256,uint16)").hex()
    rpc.receipt={"status":"0x1","blockNumber":"0x64","blockHash":BLOCK_HASH,"gasUsed":hex(150000),"effectiveGasPrice":hex(10**9),
        "logs":[{"address":POOL,"topics":[topic,topic_address(ASSET),topic_address(SENDER),"0x"+"0"*64],
                 "data":"0x"+encode(["address","uint256"],[SENDER,10*10**6]).hex(),"logIndex":"0x1"}]}
    rpc.transaction={"hash":TX_HASH,"from":SENDER,"to":POOL,"input":quote["transaction"]["data"],"value":"0x0"}
    rpc.supply+=10*10**6
    reference={"transaction_hash":TX_HASH,"chain":"base","action_id":action["action_id"]}
    gateway.store.mark(action["action_id"],state="submitted",submission=reference)
    result=gateway.reconcile(ctx,action["action_id"])
    assert result["state"]=="confirmed"
    claims,_=ProtocolBook(cfg).claims(quote["position_key"],"actor:operator")
    assert claims["supply_scaled"]==10*10**6
    assert gateway.reconcile(ctx,action["action_id"])["state"]=="confirmed"
    assert ProtocolBook(cfg).claims(quote["position_key"],"actor:operator")[0]==claims


def test_reorganization_keeps_funds_unconfirmed(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch)
    gateway=FinancialGateway(cfg);action=gateway.prepare(ctx,request(),action_key="supply")
    rpc.receipt={"status":"0x1","blockNumber":"0x64","blockHash":"0x"+"c"*64}
    gateway.store.mark(action["action_id"],state="submitted",submission={"transaction_hash":TX_HASH,"action_id":action["action_id"]})
    result=gateway.reconcile(ctx,action["action_id"])
    assert result["state"]=="unconfirmed"
    assert result["receipt"]["reason"]=="transaction_block_reorganized"


def test_offline_authorized_submission_sends_once_with_owned_signer(tmp_path,monkeypatch):
    from eth_account import Account
    cfg,rpc,provider,ctx=setup(tmp_path,monkeypatch)
    key=bytes([42])*32  # Offline fixture only; never an external account.
    signer=Account.from_key(key).address
    monkeypatch.setattr(__import__(__name__),"SENDER",signer)
    cfg.data["wallet"]["providers"]["wallet"]["config"]["address"]=signer
    provider._resolve_signer_key=lambda:key
    gateway=FinancialGateway(cfg);action=gateway.prepare(ctx,request(),action_key="supply")
    result=gateway.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    assert result["status"]=="approval_required" and rpc.sends==0
    with gateway.store.transaction() as con:
        con.execute("UPDATE approvals SET state='approved' WHERE id=?",(result["approval_id"],))
    result=gateway.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])
    assert result["state"]=="submitted" and rpc.sends==1
    assert result["submission"]["action_id"]==action["action_id"]
    assert gateway.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])["duplicate"]
    assert rpc.sends==1


def test_sdk_component_account_state_does_not_submit(tmp_path,monkeypatch):
    cfg,rpc,_,ctx=setup(tmp_path,monkeypatch)
    state=FinancialGateway(cfg).account_state(ctx,request())
    assert state["health"]=="ok" and state["state"]["debt_usd"]=="1000"
    assert rpc.sends==0
