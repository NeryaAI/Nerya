import time

import pytest
from eth_abi import decode,encode
from eth_utils import keccak

from nerya.financial.defi.calls import aave_call, v3_call, v4_call, permit2_approval,v4_receipt,POOL_KEY,topic_address
from nerya.financial.contracts import FinancialError

pytestmark = pytest.mark.smoke
OWNER = "0x" + "1"*40
TARGET = "0x" + "2"*40
TOKEN0 = "0x" + "3"*40
TOKEN1 = "0x" + "4"*40


@pytest.mark.parametrize("kind", ["supply", "withdraw", "borrow", "repay"])
def test_aave_calls_pin_amount_owner_and_variable_rate_debt(kind):
    tx = aave_call(TARGET, OWNER, kind, TOKEN0, 1000000)
    assert tx["from"] == OWNER and tx["to"] == TARGET and tx["value"] == "0x0"
    data = bytes.fromhex(tx["data"][2:])
    signature = {"supply": "supply(address,uint256,address,uint16)", "withdraw": "withdraw(address,uint256,address)",
                 "borrow": "borrow(address,uint256,uint256,uint16,address)", "repay": "repay(address,uint256,uint256,address)"}[kind]
    assert data[:4] == keccak(text=signature)[:4]
    types = {"supply": ["address","uint256","address","uint16"], "withdraw": ["address","uint256","address"],
             "borrow": ["address","uint256","uint256","uint16","address"], "repay": ["address","uint256","uint256","address"]}[kind]
    args = decode(types, data[4:])
    assert args[0] == TOKEN0 and args[1] == 1000000
    assert OWNER in args
    if kind in {"borrow", "repay"}:assert args[2] == 2


def test_uniswap_v3_mint_binds_sorted_tokens_ticks_and_minimum_amounts():
    p = {"token0": TOKEN0, "token1": TOKEN1, "fee": 500, "tick_lower": -100, "tick_upper": 100,
         "amount0_max": 1000, "amount1_max": 1000, "amount0_min": 990, "amount1_min": 990, "deadline": int(time.time())+60}
    tx = v3_call(TARGET, OWNER, "mint", p, spacing=10)
    typ = "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"
    args = decode([typ], bytes.fromhex(tx["data"][10:]))[0]
    assert args[:5] == (TOKEN0, TOKEN1, 500, -100, 100)
    assert args[5:10] == (1000, 1000, 990, 990, OWNER)
    with pytest.raises(FinancialError):v3_call(TARGET, OWNER, "mint", {**p, "token0": TOKEN1, "token1": TOKEN0}, spacing=10)


def test_uniswap_v4_actions_are_explicit_and_unreviewed_hooks_never_compile():
    key = (TOKEN0,TOKEN1,500,10,"0x"+"0"*40)
    p = {"tick_lower": -100, "tick_upper": 100, "liquidity": 100, "amount0_max": 1000,
         "amount1_max": 1000, "deadline": int(time.time())+60}
    tx = v4_call(TARGET, OWNER, "mint", key, p)
    packed, deadline = decode(["bytes", "uint256"], bytes.fromhex(tx["data"][10:]))
    actions, params = decode(["bytes", "bytes[]"], packed)
    assert actions == bytes([2, 18, 18]) and len(params) == 3 and deadline == p["deadline"]
    with pytest.raises(FinancialError):v4_call(TARGET, OWNER, "mint", (*key[:4], OWNER), p)


def test_allowances_and_protocol_operations_never_use_unlimited_spend():
    with pytest.raises(FinancialError):aave_call(TARGET, OWNER, "supply", TOKEN0, 2**256-1)
    with pytest.raises(FinancialError):permit2_approval(TARGET,OWNER,TOKEN0,TARGET,2**160-1,int(time.time())+60)


def test_v4_receipt_binds_pool_manager_emitter_and_position_manager_sender():
    key=(TOKEN0,TOKEN1,500,10,"0x"+"0"*40)
    pool_id="0x"+keccak(encode([POOL_KEY],[key])).hex()
    topic="0x"+keccak(text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)").hex()
    log={"address":TARGET,"topics":[topic,pool_id,topic_address(TOKEN0)],
         "data":"0x"+encode(["int24","int24","int256","bytes32"],[-100,100,123,(9).to_bytes(32,"big")]).hex(),"logIndex":"0x4"}
    proof=v4_receipt([log],TARGET,TOKEN0,key,9,-100,100,123)
    assert proof["liquidity_delta"]=="123" and proof["log_index"]==4
    with pytest.raises(FinancialError):v4_receipt([log],TARGET,OWNER,key,9,-100,100,123)
