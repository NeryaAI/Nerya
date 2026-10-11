from decimal import Decimal
import time

import pytest
from eth_abi import encode,decode
from eth_utils import keccak

from nerya.financial.contracts import FinancialError
from nerya.financial.defi.permit2 import Permit2Funds
from nerya.financial.defi.calls import topic_address
from nerya.financial.gateway import FinancialGateway
from test_aave_execution import setup as aave_setup,Rpc,SENDER,ASSET,POOL,DATA,CODE_HASH,BLOCK_HASH,TX_HASH

pytestmark=pytest.mark.smoke


class PermitRpc(Rpc):
    permitted=0
    expiration=0
    def _rpc(self,method,args):
        selector="0x"+keccak(text="allowance(address,address,address)")[:4].hex()
        if method=="eth_call" and args[0]["data"].startswith(selector):
            return "0x"+encode(["uint160","uint48","uint48"],[self.permitted,self.expiration,0]).hex()
        return super()._rpc(method,args)


def setup(tmp_path,monkeypatch):
    cfg,_,provider,ctx=aave_setup(tmp_path,monkeypatch)
    rpc=PermitRpc()
    cfg.data["financial"]["defi"]["deployments"]["base"]["uniswap_v4"]={"reviewed":True,
        "contracts":{"permit2":POOL,"position_manager":DATA},"code_hashes":{"permit2":CODE_HASH,"position_manager":CODE_HASH}}
    cfg.data["financial"]["defi"]["max_permit2_seconds"]=3600
    cfg.data["financial"]["wallet_permissions"]["wallet"]["permit2_approval"]=True
    raw=cfg.data["wallet"]["providers"]["wallet"]["config"]
    real=Permit2Funds
    monkeypatch.setattr("nerya.financial.defi.permit2.Permit2Funds",lambda c,r:real(c,r,connector=rpc,provider=provider,wallet_config=raw))
    monkeypatch.setattr("nerya.financial.defi.permit2.usd_price",lambda _:Decimal(1))
    return cfg,rpc,ctx


def request():
    return {"kind":"permit2_approval","wallet_id":"wallet","chain":"base","protocol":"uniswap_v4",
            "asset":ASSET,"amount":"10","spender":DATA,"parameters":{"expiration":int(time.time())+120}}


def test_permit2_has_separate_expiring_authorization_and_exact_calldata(tmp_path,monkeypatch):
    cfg,rpc,ctx=setup(tmp_path,monkeypatch)
    payload=request();gateway=FinancialGateway(cfg);action=gateway.prepare(ctx,payload,action_key="finite")
    quote=gateway.store.get_action(action["action_id"],ctx,internal=True)["quote"]
    assert quote["component_binding"]["id"]=="builtin:permit2"
    assert quote["risk_usd"]=="10" and quote["spend_usd"]=="0"
    assert ASSET not in quote["asset_amounts"]  # Approval spends gas only.
    calldata_bytes=bytes.fromhex(quote["transaction"]["data"][10:])
    decoded=decode(["address","address","uint160","uint48"],calldata_bytes)
    assert decoded==(ASSET,DATA,10000000,payload["parameters"]["expiration"])
    assert gateway.execute(ctx,action["action_id"],quote_hash=action["quote_hash"])["status"]=="approval_required"


@pytest.mark.parametrize("field,value,reason",[("spender",SENDER,"not_reviewed"),("parameters",{"expiration":1},"expiry_out_of_policy"),
    ("parameters",{"expiration":int(time.time())+86400},"expiry_out_of_policy")])
def test_permit2_wrong_spender_and_unbounded_expiration_fail(tmp_path,monkeypatch,field,value,reason):
    cfg,_,ctx=setup(tmp_path,monkeypatch)
    with pytest.raises(FinancialError,match=reason):
        FinancialGateway(cfg).prepare(ctx,{**request(),field:value},action_key="blocked")


def test_permit2_receipt_requires_exact_emitter_spender_amount_and_expiry(tmp_path,monkeypatch):
    cfg,rpc,ctx=setup(tmp_path,monkeypatch)
    gateway=FinancialGateway(cfg);action=gateway.prepare(ctx,request(),action_key="finite")
    quote=gateway.store.get_action(action["action_id"],ctx,internal=True)["quote"]
    rpc.permitted=int(quote["amount_base"]);rpc.expiration=quote["expiration"]
    topic="0x"+keccak(text="Approval(address,address,address,uint160,uint48)").hex()
    rpc.receipt={"status":"0x1","blockNumber":"0x64","blockHash":BLOCK_HASH,"gasUsed":hex(150000),"effectiveGasPrice":hex(10**9),
        "logs":[{"address":POOL,"topics":[topic,topic_address(SENDER),topic_address(ASSET),topic_address(DATA)],
        "data":"0x"+encode(["uint160","uint48"],[rpc.permitted,rpc.expiration]).hex(),"logIndex":"0x5"}]}
    rpc.transaction={"hash":TX_HASH,"from":SENDER,"to":POOL,"input":quote["transaction"]["data"],"value":"0x0"}
    gateway.store.mark(action["action_id"],state="submitted",submission={"transaction_hash":TX_HASH,"action_id":action["action_id"]})
    result=gateway.reconcile(ctx,action["action_id"])
    assert result["state"]=="confirmed"
    assert result["receipt"]["allowance_amount_base"]=="10000000"


def test_chain_aliases_normalize_before_wallet_reservations(tmp_path,monkeypatch):
    cfg,_,ctx=setup(tmp_path,monkeypatch)
    action=FinancialGateway(cfg).prepare(ctx,{**request(),"chain":"BASE"},action_key="case")
    assert action["request"]["chain"]=="base"
