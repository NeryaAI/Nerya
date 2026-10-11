from decimal import Decimal
from dataclasses import replace
import time

import pytest
from eth_abi import encode
from eth_utils import keccak

from nerya.financial.contracts import FinancialError
from nerya.financial.defi.uniswap import UniswapFunds
from nerya.financial.defi.calls import ZERO,POOL_KEY,topic_address
from nerya.financial.gateway import FinancialGateway
from test_aave_execution import setup as aave_setup,Rpc,SENDER,ASSET,POOL,DATA,ORACLE,ATOKEN,DEBT,FEED,CODE,CODE_HASH,BLOCK_HASH,TX_HASH

pytestmark=pytest.mark.smoke
TOKEN1=POOL
MANAGER=DATA
POOL_ADDRESS=ATOKEN
POOL_MANAGER=DEBT
NFT_ID=123


class LpRpc(Rpc):
    v4=False
    minted=False
    liquidity=0
    owed0=0
    owed1=0
    def _rpc(self,method,args):
        if method=="eth_call":
            target=args[0]["to"].lower();selector=args[0]["data"][:10]
            cases={
                "getPool(address,address,uint24)":(["address"],[POOL_ADDRESS]),
                "tickSpacing()":(["int24"],[10]),
                "slot0()":(["uint160","int24","uint16","uint16","uint16","uint8","bool"],[2**96,0,0,0,0,0,True]),
                "getSlot0(bytes32)":(["uint160","int24","uint24","uint24"],[2**96,0,0,500]),
                "ownerOf(uint256)":(["address"],[SENDER]),
                "positions(uint256)":(["uint96","address","address","address","uint24","int24","int24","uint128","uint256","uint256","uint128","uint128"],
                    [0,ZERO,ASSET,TOKEN1,500,-100,100,self.liquidity,0,0,self.owed0,self.owed1]),
                "getPoolAndPositionInfo(uint256)":([POOL_KEY,"uint256"],[(ASSET,TOKEN1,500,10,ZERO),(((-100)&((1<<24)-1))<<8)|(100<<32)]),
                "getPositionLiquidity(uint256)":(["uint128"],[self.liquidity]),
                "getPositionInfo(bytes32,address,int24,int24,bytes32)":(["uint128","uint256","uint256"],[self.liquidity,0,0]),
                "getFeeGrowthInside(bytes32,int24,int24)":(["uint256","uint256"],[0,0]),
                "decimals()":(["uint8"],[8]),
                "feeGrowthGlobal0X128()":(["uint256"],[0]),
                "feeGrowthGlobal1X128()":(["uint256"],[0]),
                "ticks(int24)":(["uint128","int128","uint256","uint256","int56","uint160","uint32","bool"],[self.liquidity,0,0,0,0,0,0,True]),
                "allowance(address,address,address)":(["uint160","uint48","uint48"],[10**12,int(time.time())+86400,0]),
            }
            if selector=="0x"+keccak(text="balanceOf(address)")[:4].hex() and target==MANAGER.lower():
                return "0x"+encode(["uint256"],[1 if self.minted else 0]).hex()
            for signature,(types,values) in cases.items():
                if selector=="0x"+keccak(text=signature)[:4].hex():return "0x"+encode(types,values).hex()
        return super()._rpc(method,args)


def setup(tmp_path,monkeypatch,v4=False):
    cfg,_,provider,ctx=aave_setup(tmp_path,monkeypatch)
    rpc=LpRpc();rpc.v4=v4
    name="uniswap_v4" if v4 else "uniswap_v3"
    contracts={"position_manager":MANAGER,"price_feed0":FEED,"price_feed1":ORACLE}
    if v4:contracts.update(permit2=POOL_ADDRESS,pool_manager=POOL_MANAGER,state_view=ATOKEN)
    else:contracts["factory"]=POOL_MANAGER
    cfg.data["financial"]["defi"]["deployments"]["base"][name]={"reviewed":True,"contracts":contracts,
        "code_hashes":{key:CODE_HASH for key in contracts},"pools":{"pair":{"reviewed":True,"token0":ASSET,"token1":TOKEN1,
            "fee":500,"tick_spacing":10,"address":POOL_ADDRESS,"code_hash":CODE_HASH}},
        "price_feeds":{ASSET.lower():{"reviewed":True,"address":FEED,"heartbeat_seconds":3600},
                       TOKEN1.lower():{"reviewed":True,"address":ORACLE,"heartbeat_seconds":3600}}}
    cfg.data["financial"]["defi"]["max_pool_deviation_bps"]=100
    cfg.data["financial"]["defi"]["max_permit2_seconds"]=3600
    cfg.data["financial"]["wallet_permissions"]["wallet"].update(lp_add=True,lp_remove=True,lp_collect=True)
    raw=cfg.data["wallet"]["providers"]["wallet"]["config"]
    raw["token_symbols"][TOKEN1]="USDC"
    real=UniswapFunds
    monkeypatch.setattr("nerya.financial.defi.uniswap.UniswapFunds",lambda c,r:real(c,r,connector=rpc,provider=provider,wallet_config=raw))
    return cfg,rpc,ctx,name


def request(name="uniswap_v3"):
    return {"kind":"lp_add","wallet_id":"wallet","chain":"base","protocol":name,
            "parameters":{"pool_id":"pair","tick_lower":-100,"tick_upper":100,"amount0_max":"10","amount1_max":"10",
            "amount0_min":"9.9","amount1_min":"9.9","minimum_liquidity":1,"liquidity":2000000000}}


def log(emitter,signature,indexed,types,values,index):
    return {"address":emitter,"topics":["0x"+keccak(text=signature).hex(),*indexed],"data":"0x"+encode(types,values).hex(),"logIndex":hex(index)}


def receipt(quote,*,v4=False,adding=True):
    events=[]
    if adding:
        events.append({"address":MANAGER,"topics":["0x"+keccak(text="Transfer(address,address,uint256)").hex(),topic_address(ZERO),topic_address(SENDER),"0x"+hex(NFT_ID)[2:].rjust(64,"0")],"data":"0x","logIndex":"0x1"})
    delta=2000000000 if adding else -2000000000
    if v4:
        pid="0x"+keccak(encode([POOL_KEY],[tuple(quote["pool"]["key"])] )).hex()
        events.append(log(POOL_MANAGER,"ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)",[pid,topic_address(MANAGER)],
            ["int24","int24","int256","bytes32"],[-100,100,delta,NFT_ID.to_bytes(32,"big")],2))
    else:
        events.append(log(MANAGER,("IncreaseLiquidity" if adding else "DecreaseLiquidity")+"(uint256,uint128,uint256,uint256)",
            ["0x"+encode(["uint256"],[NFT_ID]).hex()],["uint128","uint256","uint256"],[abs(delta),10000000,10000000],2))
        if not adding:events.append(log(MANAGER,"Collect(uint256,address,uint256,uint256)",["0x"+encode(["uint256"],[NFT_ID]).hex()],
            ["address","uint256","uint256"],[SENDER,10000000,10000000],3))
    for index,token in enumerate((ASSET,TOKEN1)):
        source,destination=(SENDER,POOL_MANAGER if v4 else POOL_ADDRESS) if adding else (POOL_MANAGER if v4 else MANAGER,SENDER)
        events.append(log(token,"Transfer(address,address,uint256)",[topic_address(source),topic_address(destination)],["uint256"],[10000000],4+index))
    return {"status":"0x1","blockNumber":"0x64","blockHash":BLOCK_HASH,"gasUsed":hex(180000),"effectiveGasPrice":hex(10**9),"logs":events}


@pytest.mark.parametrize("v4",[False,True])
def test_lp_mint_quote_binds_pool_asset_amounts_ticks_and_owner(tmp_path,monkeypatch,v4):
    cfg,rpc,ctx,name=setup(tmp_path,monkeypatch,v4)
    action=FinancialGateway(cfg).prepare(ctx,request(name),action_key="mint")
    quote=action["quote"]
    assert quote["risk_usd"]=="20"
    assert quote["asset_amounts"][ASSET]=="10"
    assert quote["operation"]=="mint" and quote["minimum_liquidity"]==1
    assert quote["position_owner"]=="actor:operator"


@pytest.mark.parametrize("v4",[False,True])
def test_confirmed_lp_mint_and_complete_owned_exit(tmp_path,monkeypatch,v4):
    cfg,rpc,ctx,name=setup(tmp_path,monkeypatch,v4)
    gateway=FinancialGateway(cfg);action=gateway.prepare(ctx,request(name),action_key="mint")
    quote=gateway.store.get_action(action["action_id"],ctx,internal=True)["quote"]
    rpc.minted=True;rpc.liquidity=2000000000
    rpc.receipt=receipt(quote,v4=v4)
    rpc.transaction={"hash":TX_HASH,"from":SENDER,"to":MANAGER,"input":quote["transaction"]["data"],"value":"0x0"}
    gateway.store.mark(action["action_id"],state="submitted",submission={"transaction_hash":TX_HASH,"action_id":action["action_id"]})
    minted=gateway.reconcile(ctx,action["action_id"])
    assert minted["state"]=="confirmed" and minted["receipt"]["position_id"]==str(NFT_ID)
    assert minted["receipt"]["settled_notional_usd"]=="20"
    removal={"kind":"lp_remove","wallet_id":"wallet","chain":"base","protocol":name,"position_id":str(NFT_ID),
        "parameters":{"pool_id":"pair","token_id":NFT_ID,"liquidity":2000000000,"amount0_min":"9.9","amount1_min":"9.9",
        "collect0_max":"10","collect1_max":"10"}}
    cfg.data["trading"]["risk_mode"]="reduce_only"
    action=gateway.prepare(ctx,removal,action_key="exit")
    quote=gateway.store.get_action(action["action_id"],ctx,internal=True)["quote"]
    rpc.liquidity=0;rpc.receipt=receipt(quote,v4=v4,adding=False)
    other_hash="0x"+"d"*64
    rpc.transaction={"hash":other_hash,"from":SENDER,"to":MANAGER,"input":quote["transaction"]["data"],"value":"0x0"}
    gateway.store.mark(action["action_id"],state="submitted",submission={"transaction_hash":other_hash,"action_id":action["action_id"]})
    closed=gateway.reconcile(ctx,action["action_id"])
    assert closed["state"]=="confirmed" and closed["receipt"]["position"]["closed"]


def test_lp_cannot_spend_foreign_or_unreconciled_nft(tmp_path,monkeypatch):
    cfg,rpc,ctx,name=setup(tmp_path,monkeypatch)
    rpc.minted=True;rpc.liquidity=2000000000
    payload={**request(),"parameters":{**request()["parameters"],"token_id":NFT_ID}}
    with pytest.raises(FinancialError,match="not_owned_by_strategy"):
        FinancialGateway(cfg).prepare(ctx,payload,action_key="foreign")


def test_unreviewed_pool_and_absent_price_bounds_never_quote(tmp_path,monkeypatch):
    cfg,rpc,ctx,name=setup(tmp_path,monkeypatch)
    with pytest.raises(FinancialError,match="pool_not_reviewed"):
        FinancialGateway(cfg).prepare(ctx,{**request(),"parameters":{**request()["parameters"],"pool_id":"unknown"}},action_key="bad")
    with pytest.raises(FinancialError,match="nonzero_minimum_inputs_required"):
        FinancialGateway(cfg).prepare(ctx,{**request(),"parameters":{**request()["parameters"],"amount0_min":"0"}},action_key="bad2")


def seed_mint(gateway,rpc,ctx,name):
    action=gateway.prepare(ctx,request(name),action_key="seed")
    quote=gateway.store.get_action(action["action_id"],ctx,internal=True)["quote"]
    rpc.minted=True;rpc.liquidity=2000000000;rpc.receipt=receipt(quote,v4=name=="uniswap_v4")
    rpc.transaction={"hash":TX_HASH,"from":SENDER,"to":MANAGER,"input":quote["transaction"]["data"],"value":"0x0"}
    gateway.store.mark(action["action_id"],state="submitted",submission={"transaction_hash":TX_HASH,"action_id":action["action_id"]})
    assert gateway.reconcile(ctx,action["action_id"])["state"]=="confirmed"


def test_empty_liquidity_claim_does_not_grant_another_actor_fee_ownership(tmp_path,monkeypatch):
    cfg,rpc,ctx,name=setup(tmp_path,monkeypatch)
    gateway=FinancialGateway(cfg);seed_mint(gateway,rpc,ctx,name)
    key=f"8453:uniswap_v3:{MANAGER.lower()}:{NFT_ID}"
    with gateway.store.transaction() as con:
        con.execute("UPDATE protocol_position_claims SET units_json=? WHERE position_key=?",('{"liquidity":"0"}',key))
    rpc.liquidity=0;rpc.owed0=1000000
    payload={"kind":"lp_collect","wallet_id":"wallet","chain":"base","protocol":name,
             "parameters":{"pool_id":"pair","token_id":NFT_ID,"collect0_max":"1","collect1_max":"0"}}
    with pytest.raises(FinancialError,match="not_owned_by_strategy"):
        gateway.prepare(replace(ctx,actor_id="other"),payload,action_key="steal-fees")
    assert gateway.prepare(ctx,payload,action_key="collect")["quote"]["risk_usd"]=="1"
