"""Real adapter/signing code with fake network transports and test keys only."""
import base64
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import time

import pytest
from eth_account import Account
from eth_utils import keccak

from nerya.core.config import Config,DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.connectors.evm_native import EVMNative
from nerya.connectors.solana_native import SolanaNative,_pubkey_from_signer,_read_shortvec_u16
from nerya.financial.adapters import EvmFunds,CexFunds
from nerya.financial.solana_funds import SolanaFunds,message
from nerya.financial.credit import verify_credit
from nerya.financial.contracts import FinancialError

pytestmark=pytest.mark.smoke
KEY='0x'+'07'*32  # Published deterministic test vector, never a configured key.
SENDER=Account.from_key(KEY).address
DEST='0x'+'2'*40
TOKEN='0x'+'3'*40


def config(tmp_path):
    data=deepcopy(DEFAULT_CONFIG);data['runtime']['live_trading_enabled']=True
    data['financial']={'enabled':True,'wallet_permissions':{'test':{'wallet_transfer':True,'contract_approval':True}},
        'account_permissions':{'test':{'withdraw':True,'transfer':True}},'confirmations':{'base':1},
        'withdrawal_networks':{'fake':{'base':{'chain':'base','token':TOKEN,'decimals':6,'rpc_url':'https://rpc.invalid','confirmations':1}}}}
    return Config(WorkspacePaths(tmp_path),data)


class Rpc:
    def __init__(self):self.sent=[];self.identity=[];self.tx={};self.receipt=None;self.signed=None;self.credit=1000000
    def request(self,method,url,*,body=None,**kw):
        op=body['method'];args=body['params']
        if op=='eth_sendRawTransaction':
            assert self.identity,'identity must be persisted before broadcast'
            self.sent.append(op);result='0x'+keccak(bytes.fromhex(args[0][2:])).hex()
        elif op=='eth_chainId':result='0x2105'
        elif op=='eth_getTransactionCount':result='0x0'
        elif op=='eth_getTransactionReceipt':result=self.receipt
        elif op=='eth_getTransactionByHash':result=self.tx
        elif op=='eth_blockNumber':result='0x64'
        elif op=='eth_call':result=hex(6) if args[0]['data'].startswith('0x313ce567') else hex(1000000)
        elif op=='getBlockHeight':result=1
        elif op=='sendTransaction':
            assert self.identity,'identity must be persisted before broadcast'
            import base58
            self.sent.append(op);self.signed=base64.b64decode(args[0]);_,offset=_read_shortvec_u16(self.signed,0)
            result=base58.b58encode(self.signed[offset:offset+64]).decode()
        elif op=='getSignatureStatuses':result={'value':[{'err':None,'confirmationStatus':'finalized'}]}
        elif op=='getTransaction':result={'transaction':[base64.b64encode(self.signed).decode(),'base64'],'meta':{'err':None},'slot':100}
        else:raise AssertionError('unexpected RPC '+op)
        return 200,{'jsonrpc':'2.0','result':result}


@pytest.mark.parametrize('kind',['wallet_transfer','contract_approval'])
def test_owned_evm_sign_broadcast_and_independent_confirmation(tmp_path,kind):
    cfg=config(tmp_path);rpc=Rpc();connector=EVMNative(chain='base',chain_id=8453,rpc_url='https://rpc.invalid',live=True,transport=rpc)
    provider=SimpleNamespace(_resolve_signer_key=lambda:KEY)
    adapter=EvmFunds(cfg,{'chain':'base'},connector=connector,provider=provider,wallet_config={'address':SENDER,'native_symbol':'ETH','token_symbols':{TOKEN:'USDC'}})
    request={'kind':kind,'wallet_id':'test','chain':'base','asset':TOKEN,'amount':'1','recipient':DEST,'spender':DEST}
    data=('0x095ea7b3' if kind=='contract_approval' else '0xa9059cbb')+DEST[2:].rjust(64,'0')+hex(1000000)[2:].rjust(64,'0')
    quote={'expires_at':time.time()+60,'gas_limit':60000,'gas_price_wei':'1000000001','transaction':{'from':SENDER,'to':TOKEN,'value':'0x0','data':data}}
    out=adapter.execute(request,quote,rpc.identity.append)
    assert rpc.sent==['eth_sendRawTransaction'] and out['submission']['transaction_hash']==rpc.identity[0]['transaction_hash']
    rpc.tx={'from':SENDER,'to':TOKEN,'value':'0x0','input':data}
    rpc.receipt={'status':'0x1','blockNumber':'0x64','logs':[{'address':TOKEN,'topics':['0x'+keccak(text='Transfer(address,address,uint256)').hex(),'0x'+SENDER[2:].lower().rjust(64,'0'),'0x'+DEST[2:].rjust(64,'0')],'data':hex(1000000)}]}
    assert adapter.status(request,quote,out['submission'])['state']=='confirmed'
    rpc.receipt['status']='0x0';assert adapter.status(request,quote,out['submission'])['state']=='needs_recovery'
    assert len(rpc.sent)==1


def test_solana_owned_signer_uses_persisted_signature_and_finalized_message(tmp_path):
    import base58
    cfg=config(tmp_path);rpc=Rpc();payer=_pubkey_from_signer(KEY);receiver=base58.b58encode(bytes([4])*32).decode();blockhash=base58.b58encode(bytes([5])*32).decode()
    adapter=SolanaFunds(cfg,{'chain':'solana'},connector=SolanaNative(rpc_url='https://rpc.invalid',live=True,transport=rpc),provider=SimpleNamespace(_resolve_signer_key=lambda:KEY),wallet_config={'address':payer})
    request={'kind':'wallet_transfer','wallet_id':'test','chain':'solana','asset':'SOL','amount':'1','recipient':receiver}
    instructions,_,_=adapter.instructions(request)
    quote={'expires_at':time.time()+60,'last_valid_block_height':100,'blockhash':blockhash,'instructions':instructions,'sender':payer,
        'unsigned_transaction':base64.b64encode(bytes([1])+bytes(64)+message(payer,blockhash,instructions)).decode()}
    out=adapter.execute(request,quote,rpc.identity.append)
    assert out['submission']['transaction_hash']==rpc.identity[0]['transaction_hash']
    assert adapter.status(request,quote,out['submission'])['state']=='confirmed'
    assert rpc.sent==['sendTransaction']


def test_destination_credit_requires_actual_token_delta(tmp_path):
    rpc=Rpc();rpc.receipt={'status':'0x1','blockNumber':'0x64','logs':[]}
    settings={'chain':'base','token':TOKEN,'rpc_url':'https://rpc.invalid','confirmations':1}
    assert not verify_credit(settings,receiver=DEST,transaction_hash='0x'+'4'*64,minimum_base=1,transport=rpc)['confirmed']
    rpc.receipt['logs']=[{'address':TOKEN,'topics':['0x'+keccak(text='Transfer(address,address,uint256)').hex(),'0x'+SENDER[2:].lower().rjust(64,'0'),'0x'+DEST[2:].rjust(64,'0')],'data':hex(1000000)}]
    assert verify_credit(settings,receiver=DEST,transaction_hash='0x'+'4'*64,minimum_base=1000000,transport=rpc)['confirmed']
    assert not verify_credit(settings,receiver=DEST,transaction_hash='0x'+'4'*64,minimum_base=1000001,transport=rpc)['confirmed']


@pytest.mark.parametrize('kind',['exchange_transfer','withdraw'])
def test_ccxt_transfer_and_withdraw_preserve_network_memo_and_query_identity(tmp_path,monkeypatch,kind):
    from nerya.connectors.ccxt_adapter import CcxtConnector
    calls=[]
    class Venue:
        currencies={'USDC':{'networks':{'base':{'withdraw':True,'fee':'1','memo':True}}}}
        has={key:True for key in ('transfer','withdraw','fetchTransfers','fetchWithdrawals')}
        def load_markets(self):pass
        def fetch_balance(self):return {'free':{'USDC':'100'}}
        def currency_to_precision(self,c,q):return q
        def check_address(self,a):assert a==DEST
        def transfer(self,*args):calls.append(args);return {'id':'venue-ref','status':'ok'}
        def withdraw(self,*args):calls.append(args);return {'id':'venue-ref','status':'pending'}
        def fetch_transfers(self,*args):return [{'id':'venue-ref','currency':'USDC','amount':'10','fromAccount':'spot','toAccount':'funding','status':'ok'}]
        def fetch_withdrawals(self,*args):return [{'id':'venue-ref','currency':'USDC','amount':'10','address':DEST,'status':'ok','txid':'0x'+'4'*64}]
    class Connector(CcxtConnector):
        @property
        def client(self):return Venue()
        def _check_live_and_keys(self):pass
    monkeypatch.setattr('nerya.trading.accounts.get_account_profile',lambda *_:SimpleNamespace(venue='fake',is_real_money=True,live_trading_enabled=True,status='active',permissions=SimpleNamespace(withdraw=True)))
    monkeypatch.setattr('nerya.financial.adapters.usd_price',lambda *_:Decimal('1'))
    monkeypatch.setattr('nerya.financial.credit.verify_credit',lambda *a,**kw:{'confirmed':True,'credit_base':'9000000'})
    adapter=CexFunds(config(tmp_path),{'account_id':'test'},connector=Connector())
    request={'kind':kind,'account_id':'test','asset':'USDC','amount':'10','chain':'base','recipient':DEST,'memo':'123','from_account':'spot','to_account':'funding'}
    quote=adapter.quote(request);refs=[];out=adapter.execute(request,quote,refs.append)
    assert refs[-1]['id']=='venue-ref' and len(calls)==1
    if kind=='withdraw':assert calls[0][3]=='123' and calls[0][4]['network']=='base'
    assert adapter.status(request,quote,out['submission'])['state']=='confirmed'
