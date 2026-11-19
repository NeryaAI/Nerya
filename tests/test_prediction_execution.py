"""Prediction-market wiring acceptance, isolated from the network."""
import socket
from decimal import Decimal
from dataclasses import dataclass
from types import SimpleNamespace
import pytest
from nerya.connectors.polymarket import PolymarketConnector
from nerya.connectors.cex_base import CEXCredentials
from nerya.core.errors import TradingError
from test_polymarket_connector import TOKEN_ID,FakePolymarketHttp

pytestmark=pytest.mark.smoke

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a,**kw):raise RuntimeError('NETWORK_DISABLED')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket,'create_connection',denied)


def test_slug_requires_explicit_outcome_and_no_resolves_correctly():
    http=FakePolymarketHttp();p=PolymarketConnector(transport=http)
    with pytest.raises(TradingError,match='outcome'):
        p.get_ticker('POLYMARKET:event-slug')
    p.get_ticker('POLYMARKET:event-slug#No')
    assert http.calls[-1][2]['token_id']=='2222222222222222222222222'


def test_condition_id_cannot_be_submitted_as_an_outcome_token():
    p=PolymarketConnector(transport=FakePolymarketHttp())
    with pytest.raises(TradingError,match='condition'):
        p.get_ticker('POLYMARKET:0x'+'a'*64)


def test_history_window_and_fidelity_are_not_the_requested_count():
    http=FakePolymarketHttp();p=PolymarketConnector(transport=http,clob_url='https://clob.test')
    rows=p.get_klines(TOKEN_ID,interval='1h',limit=2,since=1_700_000_000_000,end=1_700_003_600_000)
    params=http.calls[-1][2]
    assert params['fidelity']==60 and params['startTs']==1700000000 and params['endTs']==1700003600
    assert rows[1][1:5]==[.25]*4  # sampled prices are not observed OHLC bars


def test_public_ticker_preserves_prediction_slug_case(monkeypatch):
    from nerya.data.candles import fetch_public_ticker
    seen=[]
    def ticker(self,market):
        seen.append(market)
        from nerya.connectors.base import Ticker
        return Ticker(market=market,bid=.4,ask=.5,mid=.45,last=.45,spread_bps=1,ts_ms=1,venue='POLYMARKET')
    monkeypatch.setattr(PolymarketConnector,'get_ticker',ticker)
    assert fetch_public_ticker('pm:event-slug#No',allow_mock=False)['price']==.45
    assert seen==['POLYMARKET:event-slug#No']


@dataclass
class Args:
    token_id:str;price:float;size:float;side:str;expiration:int=0


def sdk_connector(monkeypatch,tmp_path):
    calls=[]
    class Client:
        def create_order(self,args,options=None):
            calls.append(('sign',args,options));return SimpleNamespace(tokenId=args.token_id,side=args.side,price=args.price,size=args.size)
        def create_market_order(self,args,*,order_type):
            calls.append(('sign_market',args,order_type));return SimpleNamespace(tokenId=args.token_id,side=args.side,price=args.price,size=args.size)
        def post_order(self,signed,order_type='GTC',post_only=False):
            calls.append(('post',signed,order_type,post_only))
            return {'success':True,'orderID':'hash','status':'live'}
        def get_order(self,oid):
            return {'id':oid,'asset_id':TOKEN_ID,'side':'BUY','status':'LIVE','original_size':'10','size_matched':'4','price':'.5','associate_trades':['t1']}
        def get_trades(self,params=None):
            return [{'id':'t1','taker_order_id':'hash','asset_id':TOKEN_ID,'side':'BUY','size':'4','price':'.48','status':'CONFIRMED','transaction_hash':'tx'}]
        def cancel_order(self,payload):return {'canceled':[],'not_canceled':{payload.orderID:'already matched'}}
        def get_balance_allowance(self,params):
            calls.append(('balance',params));return {'balance':'10000000','allowances':{}}
        def get_open_orders(self,params=None):return []
    module=SimpleNamespace(OrderArgs=Args,PartialCreateOrderOptions=lambda **kw:SimpleNamespace(**kw),
        BalanceAllowanceParams=lambda **kw:SimpleNamespace(**kw),AssetType=SimpleNamespace(COLLATERAL='COLLATERAL',CONDITIONAL='CONDITIONAL',CONDITIONAL_V2='CONDITIONAL-V2'),
        TradeParams=lambda **kw:SimpleNamespace(**kw),OrderPayload=lambda **kw:SimpleNamespace(**kw),OpenOrderParams=lambda **kw:SimpleNamespace(**kw),
        asset_protocol=lambda token:'v2' if token.startswith('651150819117105875331414918119') else 'v1',share_step=lambda tick:Decimal('.01'))
    monkeypatch.setattr('nerya.connectors.polymarket._sdk_module',lambda:module,raising=False)
    p=PolymarketConnector(live=True,credentials=CEXCredentials(api_key='key',api_secret='secret',api_passphrase='pass',extras={'privateKey':'11'*32}),transport=FakePolymarketHttp())
    monkeypatch.setattr(p,'_sdk',lambda:Client(),raising=False)
    monkeypatch.setattr(p,'_signed_order_hash',lambda signed,neg_risk:'hash',raising=False)
    p.workspace=tmp_path
    return p,calls


def test_limit_signs_current_parameters_and_tif(monkeypatch,tmp_path):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    ack=p.place_order(market='POLYMARKET:'+TOKEN_ID,side='buy',order_type='limit',size=10,price=.5,time_in_force='post_only',client_order_id='client')
    assert ack.order_id=='hash'
    args=next(call[1] for call in calls if call[0]=='sign')
    assert (args.token_id,args.price,args.size,args.side)==(TOKEN_ID,.5,10,'BUY')
    assert next(call for call in calls if call[0]=='post')[2:]==('GTC',True)


def test_order_query_accounts_only_confirmed_execution_price(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path)
    ack=p.get_order(market='POLYMARKET:'+TOKEN_ID,order_id='hash')
    assert ack.status=='partial' and ack.filled==4 and ack.avg_price==.48


def test_cancel_refusal_is_not_reported_canceled(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path)
    with pytest.raises(TradingError,match='cancel'):
        p.cancel_order(market=TOKEN_ID,order_id='hash')


def test_private_balance_uses_authenticated_sdk(monkeypatch,tmp_path):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    balance=p.get_balances()[0]
    assert balance.free==10 and balance.asset=='PUSD' and calls[0][0]=='balance'


@pytest.mark.parametrize('order_type,tif',[('market','GTC'),('market','FOK'),('limit','IOC'),('limit','FOK'),('limit','FAK')])
def test_share_sized_immediate_buy_is_rejected_before_signing(monkeypatch,tmp_path,order_type,tif):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    with pytest.raises(TradingError,match='collateral budgets'):
        p.place_order(market=TOKEN_ID,side='buy',order_type=order_type,time_in_force=tif,size=2,price=.35,client_order_id='market')
    assert calls==[] and not p._load_send('market')


@pytest.mark.parametrize('tif,expected',[('GTC','FOK'),('FOK','FOK'),('IOC','FAK'),('FAK','FAK')])
def test_market_sell_keeps_shares_and_requested_execution_type(monkeypatch,tmp_path,tif,expected):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    ack=p.place_order(market=TOKEN_ID,side='sell',order_type='market',size=1,reference_price=.3,
                      client_order_id='market',time_in_force=tif)
    signed=next(call for call in calls if call[0]=='sign_market')
    assert signed[1].size==1 and signed[1].price==Decimal('.30') and signed[2]==expected
    assert next(call for call in calls if call[0]=='post')[2]==expected and ack.filled==0


def test_submission_timeout_persists_hash_and_never_reposts(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path)
    client=p._sdk();sent=[]
    def timeout(*a,**kw):sent.append(True);raise TimeoutError('lost response')
    client.post_order=timeout;monkeypatch.setattr(p,'_sdk',lambda:client)
    args=dict(market=TOKEN_ID,side='buy',order_type='limit',size=10,price=.5,client_order_id='retry')
    with pytest.raises(TradingError) as error:p.place_order(**args)
    assert error.value.ambiguous and p._load_send('retry')['order_id']=='hash'
    assert p.place_order(**args).order_id=='hash' and len(sent)==1
    assert 'signature' not in p._state_path('retry').read_text()


@pytest.mark.parametrize('entry',['agent','script','sdk'])
@pytest.mark.parametrize('subscriber_registered', [False, True])
def test_agent_and_script_prediction_orders_reach_approval_and_fill(tmp_path,monkeypatch,entry,subscriber_registered):
    from copy import deepcopy
    from contextlib import closing
    from nerya.core.config import Config,DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.core import yaml_io
    from nerya.tools.native.trading import trade_intent_submit_handler
    from nerya.tools.types import ToolCall
    from nerya.trading.account_snapshots import AccountSnapshot
    from nerya.trading.position_book import PositionBook
    from nerya.trading.order_tracker import OrderTracker
    from nerya.trading.order_polling import poll_active_live_orders
    from nerya.trading import approval_resume
    from test_wallet_swap_approval import _approve
    import time
    # A subscriber in another workspace (or one stopped earlier) must not make
    # this callback silently skip the durable execution dispatch.
    monkeypatch.setattr(approval_resume, '_resume_subscriber_registered', subscriber_registered)
    data=deepcopy(DEFAULT_CONFIG);data['runtime']['live_trading_enabled']=True;data['runtime']['mock_mode']=False
    cfg=Config(paths=WorkspacePaths(tmp_path),data=data);yaml_io.dump(tmp_path/'nerya.yml',data)
    market='POLYMARKET:'+TOKEN_ID
    yaml_io.dump(cfg.paths.accounts_file,{'accounts':[{'id':'pm','exchange':'polymarket','venue':'polymarket','kind':'prediction_market',
        'mode':'live','status':'active','live_trading_enabled':True,'base_currency':'PUSD','permissions':{'read_balances':True,'place_order':True,'cancel_order':True}}]})
    yaml_io.dump(cfg.paths.strategy('s')/'strategy.yml',{'id':'s','status':'live','account_id':'pm','markets':[market],'live_trading_enabled':True})
    yaml_io.dump(cfg.paths.strategy('s')/'limits.yml',{'allowed_markets':[market],'min_confidence':0,'max_stale_seconds':120,'approval_threshold_usd':1})
    p,calls=sdk_connector(monkeypatch,tmp_path);client=p._sdk()
    client.get_order=lambda oid:{'id':oid,'asset_id':TOKEN_ID,'side':'BUY','status':'MATCHED','original_size':'10','size_matched':'10','price':'.5','associate_trades':['t1']}
    client.get_trades=lambda params:[{'id':'t1','taker_order_id':'hash','asset_id':TOKEN_ID,'side':'BUY','size':'10','price':'.48','status':'CONFIRMED','transaction_hash':'tx'}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    monkeypatch.setattr('nerya.connectors.ConnectorRegistry.get',lambda *a,**kw:p)
    snap=AccountSnapshot(snapshot_id='s',account_id='pm',ts=time.time(),source='live',nav_usd=1000,cash_by_asset={'PUSD':1000},free_by_asset={'PUSD':1000},health='ok')
    monkeypatch.setattr('nerya.trading.risk.fresh_snapshot',lambda *a,**kw:snap)
    monkeypatch.setattr('nerya.trading.submit.fresh_snapshot',lambda *a,**kw:snap)
    snapshot={'price':.5,'age_s':0,'_envelope':{'mode':'live'}}
    if entry=='agent':
        out=trade_intent_submit_handler(ToolCall(id='pm-agent',name='trade_intent_submit',arguments={
            'strategy_id':'s','account_id':'pm','market':market,'side':'buy','size':5,'size_unit':'usd','order_type':'limit',
            'limit_price':.5,'confidence':1,'source':'strategy_agent','market_snapshot':snapshot}),config=cfg)
        assert not out.is_error,out
        response=out.content[0].data
    elif entry=='script':
        from nerya.strategies.context import StrategyTrading
        from test_strategy_execution_modes import _policy
        trading=StrategyTrading(config=cfg,strategy_id='s',policy=_policy(),accounts=('pm',),execution_mode='live',session_id='pm-script')
        response=trading.submit_intent(market=market,side='buy',size=5,size_unit='usd',order_type='limit',limit_price=.5,confidence=1,market_snapshot=snapshot)
    else:
        from nerya.sdk.trading_api import TradingAPI
        response=TradingAPI(config=cfg,skills=None).open_position(strategy_id='s',account_id='pm',market=market,
            side='long',sizing={'method':'fixed_usd','fixed_usd':5},entry={'order_type':'limit','limit_price':.5},confidence=1,market_snapshot=snapshot)
    assert response['status']=='pending_approval',response
    approved = _approve(cfg,response['approval_id'])
    assert approved.get('resume', {}).get('ok'), approved.get('resume', approved)
    poll_active_live_orders(cfg)
    with closing(OrderTracker(cfg.paths)) as tracker:
        orders=tracker.active_orders(account_id='pm')
        assert not orders
    with closing(PositionBook(cfg.paths)) as book:
        share=book.get_share(account_id='pm',strategy_id='s',market=market)
        assert share and share.size_share_base==10 and share.avg_entry_share_price==.48
    repeated = approval_resume.resume_approved(cfg, response['approval_id'])
    assert repeated['ok'] and repeated['already_resumed']
    assert len([c for c in calls if c[0]=='post'])==1


@pytest.mark.parametrize('version,negative_risk',[('v1',False),('v1',True),('v2',False)])
@pytest.mark.parametrize('side,tif,order_type',[(side,tif,'limit') for side in ('buy','sell') for tif in ('GTC','GTD','POST_ONLY')]+[('sell','FOK','market'),('sell','IOC','market')])
def test_unified_sdk_signs_and_authenticates_without_provisioning_or_network(monkeypatch,tmp_path,version,negative_risk,side,tif,order_type):
    pytest.importorskip('polymarket')
    import json
    import httpx
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from polymarket._internal.environment import PRODUCTION_CONFIG
    from test_prediction_metadata import V2_ID, gamma
    import time
    key='11'*32;owner=Account.from_key(key).address
    creds=CEXCredentials(api_key='fixed-test-api',api_secret='dGVzdA==',api_passphrase='fixed-test-pass',extras={'privateKey':key})
    class MarketHttp(FakePolymarketHttp):
        def request(self,method,url,**kwargs):
            if url.endswith('/markets'):return 200,[gamma(version)]
            status,body=super().request(method,url,**kwargs)
            if url.endswith('/book'):
                body.update(neg_risk=negative_risk,bids=[{'price':'.5','size':'20'}],asks=[{'price':'.5','size':'20'}])
            return status,body
    token=V2_ID if version=='v2' else TOKEN_ID
    p=PolymarketConnector(live=True,credentials=creds,workspace=tmp_path,transport=MarketHttp())
    bridge=p._sdk();client=bridge.client;typed=[];captured=[];reads=[];canceled=[]
    original_sign=client._ctx.signer.sign_typed_data
    def sign(**kwargs):
        typed.append(kwargs['full_message']);return original_sign(**kwargs)
    monkeypatch.setattr(client._ctx.signer,'sign_typed_data',sign)
    def request(method,path,**kwargs):
        if method=='GET':
            reads.append(path)
            if str(path).startswith('/markets-by-token/'):
                data={'condition_id':'0x'+'a'*64}
            elif str(path).startswith('/clob-markets/'):
                data={'nr':negative_risk,'mts':'.01','t':[{'t':token}],'fd':{'r':0,'e':0}}
            elif path=='/balance-allowance':data={'balance':'10000000','allowances':{}}
            elif path=='/data/orders':data={'data':[],'next_cursor':'LTE='}
            elif str(path).startswith('/data/order/'):
                data={'id':p._load_send('real-sdk')['order_id'],'market':'0x'+'a'*64,'asset_id':token,
                      'owner':'fixed-test-api','maker_address':owner,'side':side.upper(),'price':'.5',
                      'original_size':'10','size_matched':'4','outcome':'Yes','order_type':'GTC',
                      'status':'CANCELED' if canceled else 'LIVE','associate_trades':['trade-fixture'],'created_at':int(time.time())}
            elif path=='/data/trades':
                data={'data':[{'id':'trade-fixture','market':'0x'+'a'*64,'asset_id':token,
                    'owner':'fixed-test-api','maker_address':owner,'taker_order_id':p._load_send('real-sdk')['order_id'],
                    'side':side.upper(),'trader_side':'TAKER','price':'.48' if side=='buy' else '.52','size':'4','outcome':'Yes',
                    'status':'TRADE_STATUS_CONFIRMED','fee_rate_bps':'0','bucket_index':0,'transaction_hash':'0x'+'b'*64,
                    'maker_orders':[],'match_time':int(time.time()),'last_update':int(time.time())}], 'next_cursor':'LTE='}
            elif path=='/book':data={'market':'0x'+'a'*64,'asset_id':token,'timestamp':str(int(time.time()*1000)),
                'hash':'fixture','min_order_size':'1','tick_size':'.01','neg_risk':negative_risk,'last_trade_price':'.5',
                'bids':[{'price':'.5','size':'20'}],'asks':[{'price':'.5','size':'20'}]}
            else:pytest.fail(f'unexpected SDK read {path}')
            return httpx.Response(200,json=data,request=httpx.Request(method,'https://clob.example'+path))
        if method=='DELETE':
            assert path=='/order'
            canceled.append(True)
            return httpx.Response(200,json={'canceled':[p._load_send('real-sdk')['order_id']],'not_canceled':{}},
                                  request=httpx.Request(method,'https://clob.example/order'))
        body=json.loads(kwargs['content']);captured.append((method,path,body,kwargs['headers']))
        row=p._load_send('real-sdk');assert row and row['order_id']
        assert method=='POST' and path=='/order'
        return httpx.Response(200,json={'success':True,'errorMsg':'','orderID':row['order_id'],'status':'live',
            'makingAmount':'0','takingAmount':'0','tradeIDs':[],'transactionsHashes':[]},request=httpx.Request(method,'https://clob.example/order'))
    monkeypatch.setattr(client._ctx.secure_clob._client,'request',request)
    monkeypatch.setattr(client._ctx.clob._client,'request',request)
    ack=p.place_order(market=token,side=side,order_type=order_type,size=10,price=.5,reference_price=.5,client_order_id='real-sdk',
                      time_in_force=tif,extra_params={'expiration':int(time.time())+600} if tif=='GTD' else {})
    body,headers=captured[0][2:]
    assert body['orderType']==('GTC' if tif=='POST_ONLY' else 'FAK' if tif=='IOC' else tif) and str(body['order']['tokenId'])==token
    assert int(body['order']['makerAmount'])==(5000000 if side=='buy' else 10000000)
    assert int(body['order']['takerAmount'])==(10000000 if side=='buy' else 5000000)
    assert typed[0]['domain']['version']==('3' if version=='v2' else '2')
    expected_exchange=PRODUCTION_CONFIG.exchange_v3 if version=='v2' else PRODUCTION_CONFIG.neg_risk_exchange if negative_risk else PRODUCTION_CONFIG.standard_exchange
    assert typed[0]['domain']['verifyingContract'].lower()==expected_exchange.lower()
    assert any(str(path).startswith('/markets-by-token/') for path in reads)
    assert any(str(path).startswith('/clob-markets/') for path in reads)
    assert headers['POLY_ADDRESS']==owner and headers['POLY_API_KEY']=='fixed-test-api' and headers['POLY_SIGNATURE']
    assert Account.recover_message(encode_typed_data(full_message=typed[0]),signature=body['order']['signature'])==owner
    assert ack.order_id==p._load_send('real-sdk')['order_id']
    assert len(captured)==1
    observed=p.get_order(market=token,order_id=ack.order_id)
    assert observed.status=='partial' and observed.filled==4 and observed.avg_price==(.48 if side=='buy' else .52)
    assert observed.fee_usd is None and observed.raw['fee_status']=='unverified'
    canceled_order=p.cancel_order(market=token,order_id=ack.order_id)
    assert canceled==[True] and canceled_order.status=='canceled' and canceled_order.filled==4
    assert len(captured)==1  # Reading and canceling never posts a replacement order.
    bridge.close()


def test_unsettled_matched_trade_is_not_booked(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path);client=p._sdk()
    client.get_trades=lambda params:[{'id':'t1','taker_order_id':'hash','asset_id':TOKEN_ID,'size':'4','price':'.48','status':'MATCHED'}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    ack=p.get_order(market=TOKEN_ID,order_id='hash')
    assert ack.filled==0 and ack.avg_price is None and ack.status=='new'


def test_real_sdk_does_not_retry_a_post_timeout(monkeypatch):
    pytest.importorskip('polymarket')
    import httpx
    from eth_account import Account
    from polymarket.models.clob.orders import SignedOrder
    from nerya.connectors.polymarket_client import ClobClient, ApiCreds
    key='11'*32; owner=Account.from_key(key).address
    bridge=ClobClient(host='https://fixture.invalid',chain_id=137,key=key,
                      creds=ApiCreds(api_key='fixture',api_secret='dGVzdA==',api_passphrase='fixture'))
    signed=SignedOrder(builder='0x'+'0'*64,expiration=0,maker=owner,maker_amount=5_000_000,
        metadata='0x'+'0'*64,order_type='GTC',salt=1,side='BUY',signature='0x'+'0'*130,
        signature_type=0,signer=owner,taker_amount=10_000_000,timestamp=1,token_id=TOKEN_ID)
    calls=[]
    def timeout(method,path,**kwargs):
        calls.append((method,path))
        raise httpx.ReadTimeout('fixture response lost',request=httpx.Request(method,'https://fixture.invalid/order'))
    monkeypatch.setattr(bridge.client._ctx.secure_clob._client,'request',timeout)
    try:
        with pytest.raises(Exception):bridge.post_order(signed)
        assert calls==[('POST','/order')]
    finally:bridge.close()


def test_sell_requires_owned_outcome_and_gtd_expiry(monkeypatch,tmp_path):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    with pytest.raises(TradingError,match='owned'):
        p.place_order(market=TOKEN_ID,side='sell',order_type='limit',size=11,price=.5,client_order_id='sell')
    with pytest.raises(TradingError,match='expiration'):
        p.place_order(market=TOKEN_ID,side='buy',order_type='limit',size=1,price=.5,client_order_id='gtd',time_in_force='gtd')
    assert not any(c[0]=='post' for c in calls)


def test_partial_then_final_fill_uses_incremental_not_cumulative_price(tmp_path,monkeypatch):
    from nerya.trading.order_tracker import OrderTracker
    from nerya.trading.order_polling import poll_active_live_orders
    from nerya.core import yaml_io
    from test_wallet_swap_approval import _config
    from contextlib import closing
    p,_=sdk_connector(monkeypatch,tmp_path);client=p._sdk();stage=[1]
    client.get_order=lambda oid:{'id':oid,'asset_id':TOKEN_ID,'side':'BUY','status':'MATCHED' if stage[0]==2 else 'LIVE',
        'original_size':'10','size_matched':str(5*stage[0]),'price':'.5','associate_trades':['t1'] if stage[0]==1 else ['t1','t2']}
    client.get_trades=lambda params:[{'id':params.id,'taker_order_id':'hash','asset_id':TOKEN_ID,'size':'5',
        'price':'.4' if params.id=='t1' else '.5','status':'CONFIRMED','transaction_hash':'tx'}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    monkeypatch.setattr('nerya.connectors.ConnectorRegistry.get',lambda *a,**kw:p)
    cfg=_config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file,{'accounts':[{'id':'pm','venue':'polymarket','exchange':'polymarket','kind':'prediction_market',
        'mode':'live','status':'active','live_trading_enabled':True,'permissions':{'read_balances':True,'place_order':True,'cancel_order':True}}]})
    with closing(OrderTracker(cfg.paths)) as tracker:
        order=tracker.register(client_order_id='cid',account_id='pm',strategy_id='s',market='POLYMARKET:'+TOKEN_ID,side='buy',order_type='limit',size_base=10,price=.5)
        tracker.mark_submitted(order.order_id,exchange_order_id='hash')
    factory=lambda *a:p
    first=poll_active_live_orders(cfg,connector_factory=factory)
    assert first.errors==0,first
    stage[0]=2
    second=poll_active_live_orders(cfg,connector_factory=factory)
    assert second.errors==0,second
    poll_active_live_orders(cfg,connector_factory=factory)
    with closing(OrderTracker(cfg.paths)) as tracker:
        updated=tracker.get(order.order_id);fills=tracker.fills_for_order(order.order_id)
        assert updated.filled_size==10 and updated.avg_price==.45
        assert [f.price for f in fills]==[.4,.5]


def test_discovery_exposes_both_outcome_tokens():
    p=PolymarketConnector(transport=FakePolymarketHttp())
    rows=p.list_markets(limit=1)
    assert [r['outcome'] for r in rows]==['Yes','No']
    assert rows[0]['token_id']==TOKEN_ID and rows[1]['token_id']=='2222222222222222222222222'


def test_provider_factory_keeps_api_signer_and_funder_separate(tmp_path,monkeypatch):
    from nerya.connectors.registry import build_connector
    values={'vault://api':'fixed-api','vault://secret':'fixed-secret','vault://pass':'fixed-pass','vault://key':'11'*32}
    monkeypatch.setattr('nerya.connectors.registry._resolve_ref',lambda ref,*a,**kw:values[ref])
    p=build_connector({'id':'pm','venue':'polymarket','live':True,'credentials':{
        'api_key':'vault://api','api_secret':'vault://secret','api_passphrase':'vault://pass','private_key':'vault://key'},
        'provider_config':{'funder':'0x'+'2'*40,'signature_type':2}},workspace=tmp_path)
    assert p.credentials.api_key=='fixed-api' and p.credentials.api_passphrase=='fixed-pass'
    assert p.credentials.extras['privateKey']=='11'*32 and p.signature_type==2 and p.funder=='0x'+'2'*40


def test_prediction_snapshot_counts_pusd_and_outcome_value(tmp_path,monkeypatch):
    from test_wallet_swap_approval import _config
    from nerya.trading.account_snapshots import capture_snapshot
    from nerya.core import yaml_io
    from nerya.connectors.base import Balance
    cfg=_config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file,{'accounts':[{'id':'pm','venue':'polymarket','kind':'prediction_market','mode':'live',
        'status':'active','live_trading_enabled':True,'permissions':{'read_balances':True,'place_order':True}}]})
    conn=SimpleNamespace(kind='prediction_market',get_balances=lambda:[Balance('PUSD',90,10,100)],
        get_positions_value=lambda:25,fetch_positions=lambda:[],fetch_open_orders=lambda:[])
    monkeypatch.setattr('nerya.connectors.ConnectorRegistry.get',lambda *a,**kw:conn)
    snapshot=capture_snapshot(cfg,'pm',persist=False)
    assert snapshot.nav_usd==125 and snapshot.free_usd==90


def test_cancel_racing_fill_returns_fresh_execution(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path);client=p._sdk();canceled=[False]
    client.cancel_order=lambda payload:canceled.__setitem__(0,True) or {'canceled':[payload.orderID]}
    client.get_order=lambda oid:{'id':oid,'asset_id':TOKEN_ID,'side':'BUY','original_size':'10','size_matched':'10' if canceled[0] else '0',
        'status':'CANCELED' if canceled[0] else 'LIVE','price':'.5','associate_trades':['t1'] if canceled[0] else []}
    client.get_trades=lambda params:[{'id':'t1','taker_order_id':'hash','asset_id':TOKEN_ID,'size':'10','price':'.48','status':'CONFIRMED','transaction_hash':'tx'}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    ack=p.cancel_order(market=TOKEN_ID,order_id='hash')
    assert ack.status=='filled' and ack.filled==10 and ack.avg_price==.48


def test_maker_trade_uses_own_leg_only(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path);client=p._sdk()
    client.get_trades=lambda params:[{'id':'t1','taker_order_id':'other','asset_id':'different','size':'100','price':'.9','status':'CONFIRMED','transaction_hash':'tx',
        'maker_orders':[{'order_id':'hash','asset_id':TOKEN_ID,'matched_amount':'4','price':'.47'},
                        {'order_id':'other-maker','asset_id':TOKEN_ID,'matched_amount':'96','price':'.5'}]}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    ack=p.get_order(market=TOKEN_ID,order_id='hash')
    assert ack.filled==4 and ack.avg_price==.47


def test_paper_prediction_plan_never_constructs_private_sdk(tmp_path,monkeypatch):
    from test_strategy_order_auto_approval import _config
    from nerya.core import yaml_io
    from nerya.trading.submit import submit_trade_plan
    from nerya.trading.order_intents import TradePlan,SizingPolicy
    cfg=_config(tmp_path);market='POLYMARKET:'+TOKEN_ID
    accounts=yaml_io.load(cfg.paths.accounts_file);accounts['accounts'][0].update(venue='polymarket',exchange='polymarket',kind='prediction_market')
    yaml_io.dump(cfg.paths.accounts_file,accounts)
    path=cfg.paths.strategy('s1')/'strategy.yml';doc=yaml_io.load(path);doc['markets']=[market];yaml_io.dump(path,doc)
    path=cfg.paths.strategy('s1')/'limits.yml';doc=yaml_io.load(path);doc.update(allowed_markets=[market],approval_threshold_usd=0);yaml_io.dump(path,doc)
    monkeypatch.setattr(PolymarketConnector,'_sdk',lambda self:pytest.fail('paper touched private SDK'))
    plan=TradePlan(strategy_id='s1',account_id='paper_main',market=market,action='open_position',side='long',
        sizing=SizingPolicy(method='fixed_usd',fixed_usd=5),confidence=1,source='strategy_runtime')
    result=submit_trade_plan(cfg,plan,market_snapshot={'price':.5,'age_s':0})
    assert result['status']=='filled',result
    yaml_io.dump(cfg.paths.config, {'modules': {'prediction_market': 'disabled'}})
    with pytest.raises(TradingError, match='module_disabled'):
        submit_trade_plan(cfg,plan,market_snapshot={'price':.5,'age_s':0})
