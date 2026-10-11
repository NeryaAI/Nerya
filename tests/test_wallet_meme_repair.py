"""Offline audit probes. Failing assertions express intended trading contracts."""
from pathlib import Path
import base64
import socket
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from nerya.wallet import swap_approval
from nerya.wallet.providers.byreal import ByrealWallet
from nerya.wallet.providers.okx_os import OkxOsWallet
from nerya.wallet.providers.self_custody import SelfCustodyWallet
from nerya.wallet.protocol import WalletSwapResult
from nerya.data_api.types import DataApiContext
from test_wallet_swap_approval import _config, FakeProvider, _payload

pytestmark = pytest.mark.smoke

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs): raise AssertionError('AUDIT_NETWORK_DISABLED')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def test_real_solana_v0_transaction_signs_with_correct_fee_payer():
    from nacl.signing import SigningKey
    from nerya.connectors.solana_native import _sign_solana_v0_tx
    key=SigningKey(bytes.fromhex('11'*32))
    # v0 version prefix, header, account keys, blockhash, instructions, LUTs.
    message=bytes([128,1,0,1,2])+bytes(key.verify_key)+bytes(range(32))+bytes(32)+bytes([0,0])
    raw=bytes([1])+bytes(64)+message
    signed=base64.b64decode(_sign_solana_v0_tx(base64.b64encode(raw).decode(),bytes(key).hex()))
    assert signed[65:]==message
    key.verify_key.verify(message,signed[1:65])


def test_wallet_binding_is_preserved_between_read_and_execution(tmp_path,monkeypatch):
    cfg=_config(tmp_path)
    cfg.data['wallet']={'provider':'self_custody','providers':{
        'wallet_a':{'provider':'self_custody','config':{'signer_ref':'vault://a'}},
        'wallet_b':{'provider':'self_custody','config':{'signer_ref':'vault://b'}},
    }}
    seen=[]
    monkeypatch.setattr(swap_approval,'build_provider',lambda name,config,**kw:seen.append(config) or FakeProvider())
    request,quote=swap_approval.prepare_swap(cfg,{'provider':'self_custody','wallet_id':'wallet_b',
        'chain':'solana','token_in':'SOL','token_out':'MEME','amount_in':1})
    print('selected_binding',seen[0].get('signer_ref'),'frozen_keys',sorted(request))
    assert seen[0]['signer_ref']=='vault://b'
    assert request['wallet_id']=='wallet_b'


def test_approval_freezes_wallet_configuration(tmp_path,monkeypatch):
    cfg=_config(tmp_path)
    cfg.data['wallet']={'provider':'byreal','byreal':{'keypair_path':'fixture-a.json'}}
    chosen=[]
    provider=FakeProvider()
    def build(name,config,**kw): chosen.append(config['keypair_path']); return provider
    monkeypatch.setattr(swap_approval,'build_provider',build)
    request,quote=swap_approval.prepare_swap(cfg,_payload())
    cfg.data['wallet']['byreal']['keypair_path']='fixture-b.json'
    swap_approval.execute_frozen_swap(cfg,request=request,approved_quote=quote,approval_id_value='fixture')
    print('wallet_config_before_after',chosen,'swaps',len(provider.swap_calls))
    assert not provider.swap_calls, 'changed wallet should require a fresh approval'


def test_swap_preserves_token_decimals_at_okx_boundary(tmp_path,monkeypatch):
    cfg=_config(tmp_path)
    provider=OkxOsWallet(api_key='fixture',api_secret='fixture',api_passphrase='fixture')
    requests=[]
    def get(path,params):
        requests.append(params)
        return {'code':'0','data':[{'toTokenAmount':'2000000','fromToken':{'decimal':'6'},'toToken':{'decimal':'6'}}]}
    monkeypatch.setattr(provider,'_signed_get',get)
    monkeypatch.setattr(swap_approval,'build_provider',lambda *a,**kw:provider)
    request,quote=swap_approval.prepare_swap(cfg,{'provider':'okx_os','chain':'solana',
        'token_in':'USDCMint','token_out':'MemeMint','amount_in':1,'decimals_in':6,'decimals_out':6})
    print('okx_quote_amount',requests[0]['amount'],'quote_expected',quote['expected_out'])
    assert requests[0]['amount']=='1000000'
    assert quote['expected_out']==2


def test_zero_slippage_is_not_changed_to_fifty_bps(tmp_path):
    cfg=_config(tmp_path)
    request=swap_approval.normalize_swap_request(cfg,{**_payload(),'slippage_bps':0})
    assert request['slippage_bps']==0


def test_byreal_quote_parses_ui_amount_instead_of_raw_integer(monkeypatch):
    provider=ByrealWallet()
    monkeypatch.setattr(provider,'_run_cli',lambda *a,**kw:{
        'mode':'dry-run','inAmount':'1000000000','outAmount':'2000000',
        'uiInAmount':'1','uiOutAmount':'2','inputMint':'SOLMint','outputMint':'USDCMint'})
    quote=provider.quote(chain='solana',token_in='SOLMint',token_out='USDCMint',amount_in=1)
    print('byreal_quote_ui_expected_2',quote.expected_out)
    assert quote.expected_out==2


def test_byreal_execution_uses_explicit_signer_and_observed_receipt(monkeypatch):
    from nerya.connectors.solana_native import _pubkey_from_signer
    provider=ByrealWallet()
    key='11'*32;owner=_pubkey_from_signer(key);sent=[]
    monkeypatch.setattr(provider,'_signer',lambda:key)
    monkeypatch.setattr(provider,'_api_quote',lambda *a:({'inputMint':'SOLMint','outputMint':'MemeMint',
        'inAmount':'1000000000','outAmount':'2000000','transaction':'tx','routerType':'AMM'},9,6))
    monkeypatch.setattr(provider,'_solana',lambda **kw:SimpleNamespace(send_swap_transaction=lambda *a,**k:
        sent.append(k) or {'signature':'fixture-signature','confirmed':True,'amount_out':2}))
    result=provider.swap(chain='solana',token_in='SOLMint',token_out='MemeMint',amount_in=1,live=True,
        receiver=owner,min_out=1.98)
    assert result.ok and result.tx_hash=='fixture-signature' and result.amount_out==2
    assert sent[0]['user_public_key']==owner


def test_byreal_approved_floor_and_receiver_are_not_silently_ignored(monkeypatch):
    from nerya.wallet.errors import WalletPolicyDenied
    provider=ByrealWallet()
    monkeypatch.setattr(provider,'_signer',lambda:'11'*32)
    with pytest.raises(WalletPolicyDenied,match='receiver'):
        provider.swap(chain='solana',token_in='SOLMint',token_out='MemeMint',amount_in=1,
            min_out=100,receiver='requested-wallet',live=True)


def test_byreal_configured_keypair_is_used_for_rpc_balance(tmp_path,monkeypatch):
    import json
    from nacl.signing import SigningKey
    from nerya.connectors.solana_native import _pubkey_from_signer
    sk=SigningKey(bytes.fromhex('11'*32))
    key=tmp_path/'only-selected-keypair.json';key.write_text(json.dumps(list(bytes(sk)+bytes(sk.verify_key))))
    provider=ByrealWallet(keypair_path=str(key),workspace=str(tmp_path))
    owner=_pubkey_from_signer(bytes(sk).hex());seen=[]
    monkeypatch.setattr(provider,'_solana',lambda **kw:SimpleNamespace(get_balance=lambda address:seen.append(address) or 2))
    result=provider.get_balance(chain='solana',address=owner,token='SOL')
    assert seen==[owner] and result.balance==2


def test_solana_broadcast_minimum_cannot_fall_below_approved_floor(monkeypatch):
    provider=SelfCustodyWallet()
    captured=[]
    conn=SimpleNamespace(get_mint_decimals=lambda mint:6,
        quote_jupiter=lambda **kw:{'inAmount':'1000000000','outAmount':'100000000',
                                   'otherAmountThreshold':'90000000','slippageBps':1000},
        swap=lambda **kw:captured.append(kw) or {'signature':'fixture','confirmed':True,'quote':kw['quote']})
    monkeypatch.setattr(provider,'_solana_connector',lambda **kw:conn)
    from nerya.wallet.errors import WalletPolicyDenied
    with pytest.raises(WalletPolicyDenied,match='minimum'):
        provider._solana_swap(key='11'*32,token_in='SOL',token_out='MemeMint',amount_in=1,
            slippage_bps=1000,receiver=None,min_out=99)
    assert captured==[]


def test_missing_output_decimals_fail_before_broadcast(monkeypatch):
    from nerya.core.errors import TradingError
    provider=SelfCustodyWallet();captured=[]
    def decimals(mint):raise TradingError('RPC unavailable')
    conn=SimpleNamespace(get_mint_decimals=decimals,quote_jupiter=lambda **kw:{'outAmount':'1000000000'},
        swap=lambda **kw:captured.append(kw) or {'signature':'fixture','confirmed':True,'quote':kw['quote']})
    monkeypatch.setattr(provider,'_solana_connector',lambda **kw:conn)
    try:
        provider._solana_swap(key='11'*32,token_in='SOL',token_out='MemeMint',amount_in=1,
            slippage_bps=50,receiver=None,min_out=.1)
    except TradingError:
        pass
    assert not captured,'unknown output decimals must not fall back to 9 for a money-moving call'


def test_synthetic_base_quote_does_not_enter_real_swap_approval(tmp_path,monkeypatch):
    cfg=_config(tmp_path);provider=SelfCustodyWallet()
    monkeypatch.setattr(swap_approval,'build_provider',lambda *a,**kw:provider)
    from nerya.wallet.errors import WalletQuoteError
    with pytest.raises(WalletQuoteError,match='No swap router'):
        swap_approval.prepare_swap(cfg,{'provider':'self_custody','chain':'base',
            'token_in':'0x'+'1'*40,'token_out':'0x'+'2'*40,'amount_in':1})


def test_unknown_wallet_id_does_not_fall_back_to_another_wallet(tmp_path,monkeypatch):
    from nerya.data_api.builtins import _resolve_wallet_provider
    from nerya.data_api.types import DataApiError
    cfg=_config(tmp_path);cfg.data['wallet']={'providers':{'wallet_a':{'provider':'byreal','config':{'cli_path':'fixture'}}}}
    monkeypatch.setattr('nerya.wallet.build_provider',lambda *a,**kw:SimpleNamespace())
    try:
        provider,binding=_resolve_wallet_provider(DataApiContext(cfg),{'wallet_id':'deleted-wallet'})
    except DataApiError:
        return
    print('unknown_wallet_resolved_to',binding.get('wallet_id'))
    assert binding.get('wallet_id')=='deleted-wallet'


def test_generic_onchain_candles_select_requested_quote_side():
    from nerya.data.onchain_klines import fetch_token_klines
    from urllib.parse import parse_qs,urlparse
    class Http:
        def __init__(self):self.urls=[]
        def request(self,method,url,**kw):
            self.urls.append(url)
            if '/pools/MemeMint' in url:return 404,{}
            if '/tokens/' in url:return 200,{'data':[{'attributes':{'address':'pool'},
                'relationships':{'base_token':{'data':{'id':'solana_SOLMint'}},'quote_token':{'data':{'id':'solana_MemeMint'}}}}]}
            return 200,{'data':{'attributes':{'ohlcv_list':[[100,150,150,150,150,1000]]}}}
    http=Http();rows=fetch_token_klines('solana','MemeMint',http=http)
    query=parse_qs(urlparse(http.urls[-1]).query)
    print('ohlcv_query',query,'returned_price',rows[0]['close'])
    assert query.get('token') in (['quote'],['MemeMint'])


def test_generic_onchain_history_honors_requested_window(tmp_path,monkeypatch):
    from nerya.data.candles import fetch_candles
    seen=[]
    def fetch(chain,token,**kw):
        seen.append(kw);return [{'ts':999999,'open':1,'high':1,'low':1,'close':1,'volume':1}]
    monkeypatch.setattr('nerya.data.onchain_klines.fetch_token_klines',fetch)
    rows=fetch_candles('ONCHAIN:solana:MemeMint',interval='1m',count=10,start=100,end=700,config_like=_config(tmp_path),allow_mock=False)
    print('historical_window_result',[r['ts'] for r in rows],seen)
    assert all(100<=row['ts']<=700 for row in rows)


def test_okx_unsigned_swap_has_no_broadcast_success(monkeypatch):
    provider=OkxOsWallet(api_key='fixture',api_secret='fixture',api_passphrase='fixture')
    monkeypatch.setattr(provider,'_signed_get',lambda *a,**kw:{'data':[{'tx':{'to':'router','data':'0x00'},'toTokenAmount':'2000000'}]})
    result=provider.swap(chain='solana',token_in='SOLMint',token_out='MemeMint',amount_in=1,
        receiver='wallet',live=True,decimals_in=9,decimals_out=6)
    assert not result.ok and not result.tx_hash and result.extra['unsigned_tx']


def test_byreal_live_strategy_can_reach_wallet_path(tmp_path):
    from nerya.core import yaml_io
    from nerya.trading.strategy_crud import _ensure_account_tradable
    cfg=_config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file,{'accounts':[{'id':'meme_live','exchange':'byreal','venue':'byreal',
        'kind':'chain','wallet_id':'wallet_b','mode':'live','status':'active','live_trading_enabled':True,
        'permissions':{'read_balances':True,'place_order':True,'cancel_order':True}}]})
    _ensure_account_tradable(cfg.paths,'meme_live')


def test_coinbase_node_swap_preserves_approved_minimum(monkeypatch):
    from nerya.wallet.providers.coinbase import CoinbaseWallet
    provider=CoinbaseWallet()
    captured=[]
    monkeypatch.setattr(provider,'readiness',lambda:SimpleNamespace(ready=True))
    monkeypatch.setattr(provider,'_prefer_node_skill',lambda:True)
    monkeypatch.setattr(provider,'_ref',lambda:SimpleNamespace(invoke=lambda action,payload:
        captured.append(payload) or {'ok':True,'tx_hash':'fixture','amount_out':99}))
    provider.swap(chain='base',token_in='ETH',token_out='MEME',amount_in=1,
        live=True,slippage_bps=50,min_out=100)
    print('coinbase_swap_payload_keys',sorted(captured[0]))
    assert captured[0].get('min_out')==100


def test_missing_actual_output_is_not_reported_fully_executed(tmp_path,monkeypatch):
    cfg=_config(tmp_path)
    provider=FakeProvider()
    provider.swap=lambda **kw:WalletSwapResult(provider='byreal',chain='solana',ok=True,
        tx_hash='fixture',amount_out=0,extra={'confirmed':False})
    monkeypatch.setattr(swap_approval,'build_provider',lambda *a,**kw:provider)
    request=swap_approval.normalize_swap_request(cfg,_payload())
    result=swap_approval.execute_frozen_swap(cfg,request=request,approved_quote={'min_out':99},approval_id_value='fixture')
    print('pending_receipt_effective_ok',result['ok'],'amount_out',result['result']['amount_out'])
    assert not result['ok'] or result.get('status') in ('pending','submitted','broadcast')


def test_wbnb_input_is_not_spent_as_native_bnb(monkeypatch):
    from nerya.connectors.bsc_native import BSCNative,WBNB,USDT_BEP20
    conn=BSCNative(live=True)
    monkeypatch.setattr(conn,'quote_swap',lambda **kw:{'amount_in_wei':10**18,'amount_out_min_wei':1,
        'path':[WBNB,USDT_BEP20]})
    sent=[]
    monkeypatch.setattr(conn,'_sign_and_send',lambda **kw:sent.append(kw) or {'tx_hash':'fixture','confirmed':True})
    conn.swap(token_in=WBNB,token_out=USDT_BEP20,amount_in=1,recipient='0x'+'2'*40,signer_private_key='fixture')
    print('wrapped_bnb_tx_value',sent[0]['value'],'selector',sent[0]['data'][:10])
    assert sent[0]['value']==0,'explicit WBNB token input must transfer ERC20, not spend native BNB'


def test_onchain_history_pages_without_duplicates_and_selects_explicit_mint():
    from urllib.parse import parse_qs,urlparse
    from nerya.data.onchain_klines import fetch_token_klines
    requests=[]
    class Http:
        def request(self,method,url,**kw):
            if '/ohlcv/' not in url:return 200,{}
            query=parse_qs(urlparse(url).query);requests.append(query)
            before=int(query['before_timestamp'][0]);limit=int(query['limit'][0])
            rows=[[ts,1,2,1,1.5,3] for ts in range(before-1,max(0,before-limit-1),-1)]
            return 200,{'data':{'attributes':{'ohlcv_list':rows}}}
    rows=fetch_token_klines('solana','pool@Mint',interval='1m',limit=1500,start=1001,end=2500,http=Http())
    assert len(rows)==1500 and rows[0]['ts']==1001 and rows[-1]['ts']==2500
    assert len({r['ts'] for r in rows})==1500
    assert len(requests)==2 and all(r['token']==['Mint'] and r['currency']==['usd'] for r in requests)


@pytest.mark.parametrize('router_type',['AMM','RFQ'])
def test_byreal_official_response_roundtrip_signs_selected_wallet(monkeypatch,router_type):
    from nacl.signing import SigningKey
    from nerya.connectors.solana_native import SolanaNative,_pubkey_from_signer
    key='11'*32;owner=_pubkey_from_signer(key);sk=SigningKey(bytes.fromhex(key))
    message=bytes([128,1,0,1,2])+bytes(sk.verify_key)+bytes(range(32))+bytes(32)+bytes([0,0])
    raw=bytes([1])+bytes(64)+message
    encoded=base64.b64encode(raw).decode();sent=[];calls=[]
    class Http:
        def request(self,method,url,**kw):
            calls.append((url,kw.get('body')))
            if url.endswith('/router-service/swap'):
                body=kw['body']
                assert body['userPublicKey']==owner and body['amount']=='1000000000'
                assert body['cuPrice']=='100000'
                return 200,{'result':{'inputMint':body['inputMint'],'outputMint':'Mint',
                    'inAmount':body['amount'],'outAmount':'2000001','transaction':encoded,
                    'routerType':router_type,'quoteId':'quote','orderId':'order'}}
            if url.endswith('/rfq/v1/swap'):
                signed=base64.b64decode(kw['body']['transaction'])
                sk.verify_key.verify(message,signed[1:65])
                return 200,{'result':{'data':{'txSignature':'sig'}}}
            raise AssertionError('unexpected endpoint')
    conn=SolanaNative(live=True)
    def rpc(method,params):
        assert method=='sendTransaction'
        signed=base64.b64decode(params[0]);sk.verify_key.verify(message,signed[1:65])
        sent.append(params);return __import__('base58').b58encode(signed[1:65]).decode()
    monkeypatch.setattr(conn,'_rpc',rpc)
    monkeypatch.setattr(conn,'get_mint_decimals',lambda mint:6)
    monkeypatch.setattr(conn,'wait_for_signature',lambda *a:{'confirmationStatus':'confirmed'})
    monkeypatch.setattr(conn,'transaction_output',lambda *a:2)
    monkeypatch.setattr(conn,'transaction_fee',lambda *a:{'status':'unavailable'})
    provider=ByrealWallet(config={'signer_ref':'vault://test'},transport=Http())
    monkeypatch.setattr(provider,'_signer',lambda:key)
    monkeypatch.setattr(provider,'_solana',lambda **kw:conn)
    quote=provider.quote(chain='solana',token_in='SOL',token_out='Mint',amount_in=1)
    assert quote.min_out==1.990000
    events=[]
    result=provider.swap(chain='solana',token_in='SOL',token_out='Mint',amount_in=1,live=True,
        min_out=quote.min_out,on_broadcast=events.append)
    assert result.ok and result.amount_out==2
    if router_type=='RFQ':
        assert events[-1]['tx_hash']=='sig'
    else:
        assert len(sent)==1 and events[0]['owner']==owner


def test_byreal_explicit_signer_does_not_require_cli(monkeypatch):
    provider=ByrealWallet(config={'signer_ref':'vault://test'})
    monkeypatch.setattr(provider,'_missing',lambda:['npm:byreal-cli'])
    assert provider.readiness().ready


def test_solana_output_excludes_ata_rent_and_counts_wrapped_sol(monkeypatch):
    from nerya.connectors.solana_native import SolanaNative
    conn=SolanaNative();owner='owner';mint='So11111111111111111111111111111111111111112'
    tx={'transaction':{'message':{'accountKeys':[owner,'new-ata']}},'meta':{
        'err':None,'fee':5000,'preBalances':[2_000_000_000,0],'postBalances':[1_997_955_720,1_002_039_280],
        'preTokenBalances':[],'postTokenBalances':[{'owner':owner,'mint':mint,'accountIndex':1,
            'uiTokenAmount':{'uiAmountString':'1'}}]}}
    monkeypatch.setattr(conn,'_rpc',lambda *a:tx)
    assert conn.transaction_output('sig',owner,mint)==1


def test_byreal_pool_candles_without_cli_keep_pair_units_and_window(monkeypatch):
    seen=[]
    class Http:
        def request(self,method,url,**kw):
            seen.append(kw['params'])
            return 200,{'retCode':0,'result':{'data':[{'t':150,'o':'.85','h':'.86','l':'.84','c':'.855','v':'1'}]}}
    p=ByrealWallet(transport=Http())
    monkeypatch.setattr(p,'_cli_command',lambda:None)
    rows=p.get_token_klines(chain='solana',token='pool@mint',interval='1h',limit=200,start=100,end=200)
    assert rows[0]['price_currency']=='pool_pair' and rows[0]['close']==.855
    assert seen[0]=={'poolAddress':'pool','tokenAddress':'mint','klineType':'1h','startTime':100,'endTime':200}


def test_byreal_strategy_and_public_ticker_use_token_usd_candles(tmp_path,monkeypatch):
    import time
    from nerya.wallet.strategy_execution import _price
    from nerya.data.candles import fetch_public_ticker
    calls=[]
    def usd(market,**kw):
        calls.append(market)
        return [{'ts':int(time.time()),'close':110,'price_currency':'USD','_envelope':{'mode':'live'}}]
    monkeypatch.setattr('nerya.data.candles.fetch_candles',usd)
    market='BYREAL_ONCHAIN:solana:pool@SolMint'
    assert _price(_config(tmp_path),market)['price']==110
    assert fetch_public_ticker(market,config_like=_config(tmp_path),allow_mock=False)['price']==110
    assert calls==['ONCHAIN:solana:SolMint']*2


def test_http_wallet_reads_pin_binding_and_do_not_fallback_on_typo(tmp_path,monkeypatch):
    from nerya.api.routes_wallet import routes
    from nerya.wallet.protocol import WalletBalance
    import nerya.wallet
    cfg=_config(tmp_path)
    cfg.data['wallet']={'providers':{'a':{'provider':'byreal','config':{'signer_ref':'vault://a'}},
                                   'b':{'provider':'byreal','config':{'signer_ref':'vault://b'}}}}
    chosen=[]
    def build(name,config,**kw):
        chosen.append(config['signer_ref'])
        return SimpleNamespace(get_balance=lambda **kw:WalletBalance(provider=name,chain='solana',address='owner',token='SOL',balance=1))
    monkeypatch.setattr(nerya.wallet,'build_provider',build)
    handlers={(method,path):fn for method,path,fn in routes()};client=SimpleNamespace(config=cfg)
    result=handlers['POST','/wallet/balance'](client,{'wallet_id':'b','chain':'solana','token':'SOL'})
    assert result['ok'] and chosen==['vault://b'] and result['wallet_id']=='b'
    bad=handlers['POST','/wallet/klines'](client,{'wallet_id':'missing','chain':'solana','token':'mint'})
    assert not bad['ok'] and bad['error']=='policy_denied'
