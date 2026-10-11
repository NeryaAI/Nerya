from copy import deepcopy

import pytest
from eth_abi import encode
from eth_utils import keccak

from nerya.connectors.prediction_fees import EVENT, confirmed_order_fees
from nerya.core.errors import TradingError

pytestmark=pytest.mark.smoke
HASH='0x'+'a'*64
TX='0x'+'b'*64
BLOCK='0x'+'c'*64
OWNER='0x'+'1'*40
EXCHANGE='0x'+'2'*40
TOKEN='1111111111111111111111111'
CODE='0x6001'
CODE_HASH='0x'+keccak(bytes.fromhex(CODE[2:])).hex()


def event(side='buy', *, shares=10_000_000, collateral=5_000_000, fee=100_000):
    amounts=[collateral,shares] if side=='buy' else [shares,collateral]
    return {'address':EXCHANGE,'topics':[EVENT,HASH,'0x'+OWNER[2:].rjust(64,'0'),'0x'+EXCHANGE[2:].rjust(64,'0')],
            'logIndex':'0x1','data':'0x'+encode(['uint8','uint256','uint256','uint256','uint256','bytes32','bytes32'],
                [0 if side=='buy' else 1,int(TOKEN),*amounts,fee,bytes(32),bytes(32)]).hex()}


class Rpc:
    def __init__(self, side='buy'):
        self.receipt={'transactionHash':TX,'blockNumber':'0x64','blockHash':BLOCK,'status':'0x1','logs':[event(side)]}
        self.code=CODE
        self.block=BLOCK
        self.calls=[]
    def __call__(self, method, params):
        self.calls.append(method)
        return {'eth_chainId':'0x89','eth_blockNumber':'0x66','eth_getTransactionReceipt':self.receipt,
                'eth_getBlockByNumber':{'hash':self.block},'eth_getCode':self.code}[method]


def verify(rpc, **kwargs):
    return confirmed_order_fees(rpc,transactions=[TX,TX],order_hash=HASH,owner=OWNER,token_id=TOKEN,
        side=kwargs.get('side','buy'),filled_shares=kwargs.get('filled_shares',10),
        exchange_code_hashes={EXCHANGE:CODE_HASH})


@pytest.mark.parametrize('side',['buy','sell'])
def test_v2_fees_are_collateral_for_both_sides_and_not_counted_twice(side):
    rpc=Rpc(side)
    rpc.receipt['logs'].append(deepcopy(rpc.receipt['logs'][0]))
    result=verify(rpc,side=side)
    assert result['fee_collateral']=='0.1' and result['fee_asset']=='PUSD'
    assert result['cumulative_notional']=='5' and len(result['evidence'])==1
    assert rpc.calls.count('eth_getTransactionReceipt')==1


@pytest.mark.parametrize('problem',['owner','code','event_missing','reorg','wrong_token','not_confirmed','wrong_amount','layout'])
def test_missing_or_mismatched_fee_evidence_never_becomes_zero(problem):
    rpc=Rpc()
    if problem=='owner': rpc.receipt['logs'][0]['topics'][2]='0x'+'3'*64
    elif problem=='code': rpc.code='0x6002'
    elif problem=='event_missing': rpc.receipt['logs']=[]
    elif problem=='reorg': rpc.block='other'
    elif problem=='wrong_token': rpc.receipt['logs'][0]['data']='0x'+encode(['uint8','uint256','uint256','uint256','uint256','bytes32','bytes32'],[0,999,5_000_000,10_000_000,0,bytes(32),bytes(32)]).hex()
    elif problem=='not_confirmed': rpc.receipt['status']='0x0'
    elif problem=='wrong_amount': rpc.receipt['logs'][0]=event(shares=9_000_000)
    else: rpc.receipt['logs'][0]['data']='0x00'
    with pytest.raises(TradingError): verify(rpc)


def test_zero_is_verified_only_when_reported_by_the_matching_onchain_event():
    rpc=Rpc()
    rpc.receipt['logs'][0]=event(fee=0)
    assert verify(rpc)['fee_collateral']=='0'


def test_connector_uses_chain_notional_and_cumulative_fee(monkeypatch,tmp_path):
    from test_prediction_execution import sdk_connector
    from test_polymarket_connector import TOKEN_ID
    p,_=sdk_connector(monkeypatch,tmp_path)
    client=p._sdk()
    client.get_order=lambda oid:{'id':oid,'asset_id':TOKEN_ID,'side':'BUY','status':'LIVE',
        'original_size':'10','size_matched':'4','price':'.5','associate_trades':['t1']}
    client.get_trades=lambda params:[{'id':'t1','taker_order_id':HASH,'asset_id':TOKEN_ID,
        'size':'4','price':'.48','status':'CONFIRMED','transaction_hash':TX}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    p.settlement_rpc_url='https://rpc.invalid'
    p.settlement_exchange_code_hashes={EXCHANGE:CODE_HASH}
    p.funder=OWNER
    rpc=Rpc()
    doc=event(shares=4_000_000,collateral=1_910_000,fee=40_000)
    # Rebind the exact tested connector outcome rather than relying on a slug.
    doc['data']='0x'+encode(['uint8','uint256','uint256','uint256','uint256','bytes32','bytes32'],
        [0,int(TOKEN_ID),1_910_000,4_000_000,40_000,bytes(32),bytes(32)]).hex()
    rpc.receipt['logs']=[doc]
    monkeypatch.setattr('nerya.connectors.evm_native.EVMNative._rpc',lambda self,method,params:rpc(method,params))
    ack=p.get_order(market=TOKEN_ID,order_id=HASH)
    assert ack.fee_usd==.04 and ack.avg_price==pytest.approx(1.91/4)
    assert ack.raw['fee_status']=='chain_verified'
