"""Prediction-market wiring acceptance, isolated from the network."""
import socket
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
        BalanceAllowanceParams=lambda **kw:SimpleNamespace(**kw),AssetType=SimpleNamespace(COLLATERAL='COLLATERAL',CONDITIONAL='CONDITIONAL'),
        TradeParams=lambda **kw:SimpleNamespace(**kw),OrderPayload=lambda **kw:SimpleNamespace(**kw),OpenOrderParams=lambda **kw:SimpleNamespace(**kw))
    monkeypatch.setattr('nerya.connectors.polymarket._sdk_module',lambda:module,raising=False)
    p=PolymarketConnector(live=True,credentials=CEXCredentials(api_key='key',api_secret='secret',api_passphrase='pass',extras={'privateKey':'11'*32}),transport=FakePolymarketHttp())
    monkeypatch.setattr(p,'_sdk',lambda:Client(),raising=False)
    monkeypatch.setattr(p,'_signed_order_hash',lambda signed,neg_risk:'hash',raising=False)
    p.workspace=tmp_path
    return p,calls


def test_limit_signs_current_parameters_and_tif(monkeypatch,tmp_path):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    ack=p.place_order(market='POLYMARKET:'+TOKEN_ID,side='buy',order_type='limit',size=10,price=.5,time_in_force='post_only',client_order_id='client')
    assert ack.order_id=='hash' and calls[0][0]=='sign'
    args=calls[0][1]
    assert (args.token_id,args.price,args.size,args.side)==(TOKEN_ID,.5,10,'BUY')
    assert calls[1][2:]==('GTC',True)


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


def test_market_buy_uses_bounded_share_order_not_usd_amount(monkeypatch,tmp_path):
    p,calls=sdk_connector(monkeypatch,tmp_path)
    ack=p.place_order(market=TOKEN_ID,side='buy',order_type='market',size=2,reference_price=.35,client_order_id='market')
    assert calls[0][1].size==2 and calls[0][1].price==.35
    assert calls[1][2]=='FOK' and ack.filled==0
    with pytest.raises(TradingError,match='liquidity|slippage'):
        p.place_order(market=TOKEN_ID,side='buy',order_type='market',size=7,reference_price=.35,client_order_id='bad')
    assert len([x for x in calls if x[0]=='post'])==1


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


def test_unified_sdk_signs_and_authenticates_without_provisioning_or_network(monkeypatch,tmp_path):
    pytest.importorskip('polymarket')
    import json
    import httpx
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from polymarket._internal.actions.orders.types import OrderDraft
    from polymarket._internal.environment import PRODUCTION_CONFIG
    key='11'*32;owner=Account.from_key(key).address
    creds=CEXCredentials(api_key='fixed-test-api',api_secret='dGVzdA==',api_passphrase='fixed-test-pass',extras={'privateKey':key})
    p=PolymarketConnector(live=True,credentials=creds,workspace=tmp_path,transport=FakePolymarketHttp())
    bridge=p._sdk();client=bridge.client;typed=[];captured=[]
    original_sign=client._ctx.signer.sign_typed_data
    def sign(**kwargs):
        typed.append(kwargs['full_message']);return original_sign(**kwargs)
    monkeypatch.setattr(client._ctx.signer,'sign_typed_data',sign)
    def draft(**kwargs):
        return client._sign_order(OrderDraft(chain_id=137,exchange_address=PRODUCTION_CONFIG.standard_exchange,
            expiration=0,funder_address=owner,offered_amount=5000000,requested_amount=10000000,
            order_type='GTC',side=kwargs['side'],signer=owner,asset_id=TOKEN_ID),post_only=kwargs.get('post_only',False))
    monkeypatch.setattr(client,'_prepare_and_sign_limit_order',draft)
    def request(method,path,**kwargs):
        body=json.loads(kwargs['content']);captured.append((method,path,body,kwargs['headers']))
        row=p._load_send('real-sdk');assert row and row['order_id']
        assert method=='POST' and path=='/order'
        return httpx.Response(200,json={'success':True,'errorMsg':'','orderID':row['order_id'],'status':'live',
            'makingAmount':'0','takingAmount':'0','tradeIDs':[],'transactionsHashes':[]},request=httpx.Request(method,'https://clob.example/order'))
    monkeypatch.setattr(client._ctx.secure_clob._client,'request',request)
    ack=p.place_order(market=TOKEN_ID,side='buy',order_type='limit',size=10,price=.5,client_order_id='real-sdk')
    body,headers=captured[0][2:]
    assert body['orderType']=='GTC' and str(body['order']['tokenId'])==TOKEN_ID
    assert int(body['order']['makerAmount'])==5000000 and int(body['order']['takerAmount'])==10000000
    assert headers['POLY_ADDRESS']==owner and headers['POLY_API_KEY']=='fixed-test-api' and headers['POLY_SIGNATURE']
    assert Account.recover_message(encode_typed_data(full_message=typed[0]),signature=body['order']['signature'])==owner
    assert ack.order_id==p._load_send('real-sdk')['order_id']
    assert len(captured)==1
    bridge.close()


def test_unsettled_matched_trade_is_not_booked(monkeypatch,tmp_path):
    p,_=sdk_connector(monkeypatch,tmp_path);client=p._sdk()
    client.get_trades=lambda params:[{'id':'t1','taker_order_id':'hash','asset_id':TOKEN_ID,'size':'4','price':'.48','status':'MATCHED'}]
    monkeypatch.setattr(p,'_sdk',lambda:client)
    ack=p.get_order(market=TOKEN_ID,order_id='hash')
    assert ack.filled==0 and ack.avg_price is None and ack.status=='new'


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
    result=submit_trade_plan(cfg,TradePlan(strategy_id='s1',account_id='paper_main',market=market,action='open_position',side='long',
        sizing=SizingPolicy(method='fixed_usd',fixed_usd=5),confidence=1,source='strategy_runtime'),market_snapshot={'price':.5,'age_s':0})
    assert result['status']=='filled',result
