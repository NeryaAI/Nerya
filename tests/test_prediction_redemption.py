from contextlib import closing
from types import SimpleNamespace

import pytest
from eth_abi import encode, decode
from eth_utils import keccak

from nerya.core import yaml_io
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.defi.prediction import PredictionRedemptionFunds
from nerya.financial.defi.calls import topic_address
from nerya.financial.gateway import FinancialGateway
from nerya.trading.order_tracker import OrderTracker
from nerya.trading.position_book import PositionBook
from test_aave_execution import setup as aave_setup, Rpc, SENDER, ASSET, POOL, DATA, CODE_HASH, TX_HASH, BLOCK_HASH

pytestmark = pytest.mark.smoke
CONDITION = '0x'+'c'*64
TOKENS = ['11111111111111111111111111','22222222222222222222222222']
CTF, ADAPTER, COLLATERAL = POOL, DATA, ASSET


class SettlementRpc(Rpc):
    resolved = True
    approved = True
    balances = [10_000_000, 5_000_000]
    def _rpc(self, method, args):
        if method == 'eth_chainId':
            return hex(137)
        if method == 'eth_call':
            selector = args[0]['data'][:10]
            arguments = bytes.fromhex(args[0]['data'][10:])
            def matches(signature):
                return selector == '0x'+keccak(text=signature)[:4].hex()
            if matches('payoutDenominator(bytes32)'):
                types, values = ['uint256'], [1 if self.resolved else 0]
            elif matches('payoutNumerators(bytes32,uint256)'):
                index = decode(['bytes32','uint256'], arguments)[1]
                types, values = ['uint256'], [1 if index == 0 and self.resolved else 0]
            elif matches('getOutcomeSlotCount(bytes32)'):
                types, values = ['uint256'], [2]
            elif matches('getCollectionId(bytes32,bytes32,uint256)'):
                index = decode(['bytes32','bytes32','uint256'], arguments)[2]
                types, values = ['bytes32'], [index.to_bytes(32,'big')]
            elif matches('getPositionId(address,bytes32)'):
                index = int.from_bytes(decode(['address','bytes32'], arguments)[1], 'big')
                types, values = ['uint256'], [int(TOKENS[index-1])]
            elif matches('balanceOf(address,uint256)'):
                token = str(decode(['address','uint256'], arguments)[1])
                types, values = ['uint256'], [self.balances[TOKENS.index(token)]]
            elif matches('isApprovedForAll(address,address)'):
                types, values = ['bool'], [self.approved]
            else:
                return super()._rpc(method,args)
            return '0x'+encode(types,values).hex()
        return super()._rpc(method,args)


def setup(tmp_path, monkeypatch):
    cfg, _, _, _ = aave_setup(tmp_path, monkeypatch)
    deployment = {'reviewed':True, 'contracts':{'conditional_tokens':CTF,'collateral_adapter':ADAPTER,'collateral':COLLATERAL},
        'code_hashes':{key:CODE_HASH for key in ('conditional_tokens','collateral_adapter','collateral')},
        'conditions':{CONDITION:{'reviewed':True,'negative_risk':False,'outcome_collateral':COLLATERAL,'token_ids':TOKENS}}}
    cfg.data['financial']['defi']['deployments']['polygon'] = {'polymarket_ctf':deployment}
    cfg.data['financial']['wallet_permissions']['wallet']['redeem'] = True
    cfg.data['wallet']['providers']['wallet']['config']['native_symbol'] = 'POL'
    account = {'id':'pm','venue':'polymarket','kind':'prediction_market','mode':'live','status':'active',
        'wallet_id':'wallet','live_trading_enabled':True,'permissions':{'read_balances':True,'place_order':True},
        'provider_config':{'signature_type':0,'funder':SENDER}}
    yaml_io.dump(cfg.paths.accounts_file, {'accounts':[account]})
    yaml_io.dump(cfg.paths.strategy('s')/'strategy.yml', {'id':'s','accounts':['pm'],
        'markets':['POLYMARKET:'+token for token in TOKENS],'status':'live','live_trading_enabled':True})
    with closing(PositionBook(cfg.paths)) as book:
        for index, token in enumerate(TOKENS):
            book.apply_fill(account_id='pm',strategy_id='s',market='POLYMARKET:'+token,
                side='buy',price=.5,size_base=10 if index==0 else 5,source='live',fill_id='initial:'+token)
    rpc = SettlementRpc()
    rpc.balances = [10_000_000,5_000_000]
    raw = cfg.data['wallet']['providers']['wallet']['config']
    monkeypatch.setattr('nerya.financial.defi.prediction.PredictionRedemptionFunds',
        lambda c,r: PredictionRedemptionFunds(c,r,connector=rpc,
            provider=SimpleNamespace(id='self_custody',_resolve_signer_key=lambda:'unused'),wallet_config=raw))
    ctx = FinancialContext('operator',frozenset({'api:all'}),task_kind='strategy_agent',task_id='s')
    return cfg,rpc,ctx,account


def request():
    return {'kind':'redeem','wallet_id':'wallet','account_id':'pm','chain':'polygon','protocol':'polymarket_ctf',
            'parameters':{'condition_id':CONDITION,'strategy_id':'s'}}


def receipt():
    logs = [{'address':CTF,'topics':['0x'+keccak(text='TransferBatch(address,address,address,uint256[],uint256[])').hex(),
                topic_address(ADAPTER),topic_address(SENDER),topic_address(ADAPTER)],
             'data':'0x'+encode(['uint256[]','uint256[]'],[[int(x) for x in TOKENS],[10_000_000,5_000_000]]).hex(),'logIndex':'0x1'},
            {'address':COLLATERAL,'topics':['0x'+keccak(text='Transfer(address,address,uint256)').hex(),
                topic_address(ADAPTER),topic_address(SENDER)],'data':'0x'+encode(['uint256'],[10_000_000]).hex(),'logIndex':'0x2'}]
    return {'status':'0x1','blockNumber':'0x64','blockHash':BLOCK_HASH,'gasUsed':hex(100000),
            'effectiveGasPrice':hex(10**9),'logs':logs}


def submitted(gateway,ctx,action,rpc):
    quote = gateway.store.get_action(action['action_id'],ctx,internal=True)['quote']
    rpc.receipt = receipt()
    rpc.balances = [0,0]
    rpc.transaction = {'hash':TX_HASH,'from':SENDER,'to':ADAPTER,'input':quote['transaction']['data'],'value':'0x0'}
    gateway.store.mark(action['action_id'],state='submitted',submission={'transaction_hash':TX_HASH,'action_id':action['action_id']})


def test_redemption_quote_has_exact_binary_call_and_no_implicit_approval(tmp_path,monkeypatch):
    cfg,rpc,ctx,_ = setup(tmp_path,monkeypatch)
    action = FinancialGateway(cfg).prepare(ctx,request(),action_key='redeem')
    quote = action['quote']
    assert quote['component_binding']['id']=='builtin:prediction_settlement'
    assert quote['redemption']['balances_base']==['10000000','5000000']
    assert quote['redemption']['payouts_base']==['10000000','0']
    assert quote['redemption']['transaction']['to']==ADAPTER
    assert not quote['prerequisites'] and rpc.sends==0


def test_confirmed_winning_and_zero_value_losing_outcomes_close_once(tmp_path,monkeypatch):
    cfg,rpc,ctx,_ = setup(tmp_path,monkeypatch)
    gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx,request(),action_key='redeem')
    submitted(gateway,ctx,action,rpc)
    result = gateway.reconcile(ctx,action['action_id'])
    assert result['state']=='confirmed', result
    assert result['receipt']['payout_base']=='10000000'
    assert gateway.reconcile(ctx,action['action_id'])['state']=='confirmed'
    with closing(PositionBook(cfg.paths)) as book:
        assert not book.get_share(account_id='pm',strategy_id='s',market='POLYMARKET:'+TOKENS[0])
        assert not book.get_share(account_id='pm',strategy_id='s',market='POLYMARKET:'+TOKENS[1])
    with closing(OrderTracker(cfg.paths)) as tracker:
        for token in TOKENS:
            order = tracker.get_by_client_order_id('redeem_'+TX_HASH+'_'+token)
            assert len(tracker.fills_for_order(order.order_id))==1


@pytest.mark.parametrize('issue',['unresolved','approval','foreign_holdings','wrong_condition','proxy','paper'])
def test_redemption_refuses_unsafe_or_unready_state(tmp_path,monkeypatch,issue):
    cfg,rpc,ctx,account = setup(tmp_path,monkeypatch)
    if issue=='unresolved': rpc.resolved=False
    elif issue=='approval': rpc.approved=False
    elif issue=='foreign_holdings': rpc.balances=[11_000_000,5_000_000]
    elif issue=='wrong_condition': cfg.data['financial']['defi']['deployments']['polygon']['polymarket_ctf']['conditions'][CONDITION]['token_ids']=list(reversed(TOKENS))
    else:
        if issue=='proxy': account['provider_config']['signature_type']=2
        else: account['mode']='paper'
        yaml_io.dump(cfg.paths.accounts_file,{'accounts':[account]})
    with pytest.raises(FinancialError):
        FinancialGateway(cfg).prepare(ctx,request(),action_key='unsafe')
    assert rpc.sends==0


def test_wrong_receipt_stays_unconfirmed_and_does_not_close_position(tmp_path,monkeypatch):
    cfg,rpc,ctx,_ = setup(tmp_path,monkeypatch)
    gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx,request(),action_key='redeem')
    submitted(gateway,ctx,action,rpc)
    rpc.receipt['logs'][1]['data']='0x'+encode(['uint256'],[9_000_000]).hex()
    result = gateway.reconcile(ctx,action['action_id'])
    assert result['state']=='needs_recovery'
    with closing(PositionBook(cfg.paths)) as book:
        assert book.get_share(account_id='pm',strategy_id='s',market='POLYMARKET:'+TOKENS[0]).size_share_base==10


def test_pending_orders_prevent_full_balance_redemption(tmp_path,monkeypatch):
    cfg,rpc,ctx,_ = setup(tmp_path,monkeypatch)
    with closing(OrderTracker(cfg.paths)) as tracker:
        tracker.register(client_order_id='pending',account_id='pm',strategy_id='s',market='POLYMARKET:'+TOKENS[0],
                         side='sell',order_type='limit',size_base=1,notional_usd=1)
    with pytest.raises(FinancialError,match='unresolved'):
        FinancialGateway(cfg).prepare(ctx,request(),action_key='pending')


def test_prediction_binding_does_not_enable_generic_wallet_swaps(tmp_path,monkeypatch):
    from nerya.wallet.bindings import resolve_binding
    from nerya.wallet.errors import WalletPolicyDenied
    cfg,_,_,_ = setup(tmp_path,monkeypatch)
    with pytest.raises(WalletPolicyDenied):
        resolve_binding(cfg,{**request(),'kind':'swap'})
