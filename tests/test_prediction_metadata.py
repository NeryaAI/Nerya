"""Public identity and skill routing contracts, with no network access."""
import pytest

from nerya.connectors.polymarket import PolymarketConnector
from nerya.connectors.prediction_markets import market_outcomes
from nerya.core.errors import TradingError
from test_polymarket_connector import FakePolymarketHttp, TOKEN_ID

pytestmark = pytest.mark.smoke
V2_ID = '651150819117105875331414918119047680898421632356043490229292782433651916800'


def gamma(version='v2'):
    return {'version': version, 'slug': 'MixedCase', 'question': 'Will the event happen?',
            'outcomes': '["Yes","No"]', 'clobTokenIds': [TOKEN_ID, '2222222222222222222222222'],
            'positionIds': [V2_ID, str(int(V2_ID) + 1)], 'acceptingOrders': True}


@pytest.mark.parametrize('version,expected', [('v1', TOKEN_ID), ('v2', V2_ID)])
def test_protocol_selects_identity_even_when_both_fields_exist(version, expected):
    row = gamma(version)
    conn = PolymarketConnector()
    conn._get = lambda *a, **kw: [row]
    assert conn.list_markets()[0]['asset_id'] == expected
    assert conn._resolve_token('POLYMARKET:MixedCase#yes')[0] == expected
    meta = conn.get_instrument(expected)
    assert meta['protocol_version'] == version and meta['outcome'] == 'Yes'
    assert meta['label'] == row['question'] and meta['settlement_status'] == 'open'


def test_v2_discovery_without_legacy_tokens_and_numeric_lookup():
    row = gamma(); row.pop('clobTokenIds')
    calls = []
    conn = PolymarketConnector()
    def get(*args, params):
        calls.append(params)
        return [] if 'clob_token_ids' in params else [row]
    conn._get = get
    assert conn.get_market_metadata(V2_ID)['asset_id'] == V2_ID
    assert calls == [{'clob_token_ids': V2_ID}, {'position_ids': V2_ID}]
    assert conn.list_markets()[1]['outcome'] == 'No'


@pytest.mark.parametrize('patch', [{'version': None}, {'version': 'v9'}, {'positionIds': None},
    {'positionIds': ['1']}, {'positionIds': [True, '2']}, {'outcomes': '["Yes","YES"]'}])
def test_unknown_or_incomplete_identity_never_selects_another_protocol(patch):
    row = gamma(); row.update(patch)
    with pytest.raises(TradingError):
        market_outcomes(row, 'POLYMARKET')


def test_retired_asset_is_not_substituted_with_the_same_outcomes_new_id():
    conn = PolymarketConnector()
    conn._get = lambda *a, **kw: [gamma()]
    with pytest.raises(TradingError, match='retired'):
        conn.get_market_metadata(TOKEN_ID)


def test_prediction_skill_uses_registry_aliases_and_selected_connector(monkeypatch):
    from nerya.skills.builtin.adapter.scripts import inspect_prediction
    conn = PolymarketConnector(transport=FakePolymarketHttp(), clob_url='https://clob.test')
    created = []
    def build(config, **kwargs):
        created.append(config)
        return conn
    monkeypatch.setattr(inspect_prediction, 'build_connector', build)
    result = inspect_prediction.run(provider='pm', market='POLYMARKET:event-slug#No')
    assert result['ok'] and result['metadata']['outcome'] == 'No'
    assert result['book']['size_step']=='0.01' and result['book']['min_order_size']==1
    assert 'bid=0.3' in result['summary'] and len(result['book']['bids'])<=3
    assert created[0]['venue'] == 'polymarket' and created[0]['live'] is False
    assert inspect_prediction.run(provider='unregistered')['ok'] is False
    assert inspect_prediction.run(provider='pm', market='BINANCE:BTCUSDT')['ok'] is False


def test_sample_history_preserves_zero_and_missing_data_is_not_a_sample():
    conn=PolymarketConnector()
    conn._get_price_history=lambda params:{'history':[{'t':1700000000,'p':0}, {'t':1700000001,'p':None}]}
    history=conn.get_price_history(TOKEN_ID,start=1700000000,end=1700000010)
    assert history['data_kind']=='price_samples' and history['volume_available'] is False
    assert history['samples']==[{'ts':1700000000,'price':0}]
    conn._get_price_history=lambda params:{}
    with pytest.raises(TradingError,match='history unavailable'):
        conn.get_price_history(TOKEN_ID)


@pytest.mark.parametrize('venue',['polymarket','pm','polymarket_v2'])
def test_agent_market_data_reads_prediction_quotes_without_trading_keys(tmp_path,monkeypatch,venue):
    from test_phase_m_native_first import _bare_config
    from nerya.tools.native.connectors import market_data_handler, _venue_has_public_market_feed
    from nerya.tools.types import ToolCall
    cfg=_bare_config(tmp_path)
    http=FakePolymarketHttp()
    monkeypatch.setattr(PolymarketConnector,'_get',lambda self,base,path,**kwargs:http.request('GET',base+'/'+path,**kwargs)[1])
    monkeypatch.setattr(PolymarketConnector,'_sdk',lambda self:pytest.fail('public quotes must not access signing credentials'))
    result=market_data_handler(ToolCall(name='market_data',arguments={'action':'get_ticker','market':f'{venue}:{TOKEN_ID}'}),config_like=cfg)
    data=result.content[0].data
    assert not data.get('error') and data['bid']==.3 and data['ask']==.35
    assert data['_envelope']['mode']=='live'
    assert _venue_has_public_market_feed(venue,capability='klines',config_like=cfg)


def test_script_public_prediction_book_is_not_replaced_by_mock_account_prices(tmp_path,monkeypatch):
    from test_phase_m_native_first import _bare_config
    from nerya.core import yaml_io
    from nerya.connectors import ConnectorRegistry
    from nerya.strategies.context import StrategyMarket
    cfg=_bare_config(tmp_path)
    yaml_io.dump(cfg.paths.accounts_file,{'accounts':[{'id':'practice','venue':'mock','mode':'paper','initial_balance_usd':1000}]})
    http=FakePolymarketHttp()
    monkeypatch.setattr(PolymarketConnector,'_get',lambda self,base,path,**kw:http.request('GET',base+'/'+path,**kw)[1])
    market=StrategyMarket(paths=cfg.paths,accounts=('practice',),config=cfg,
                          _registry_factory=lambda:ConnectorRegistry(workspace=cfg.paths.root))
    book=market.orderbook('POLYMARKET:'+TOKEN_ID)
    assert book['bid']==.3 and book['ask']==.35
    assert book['min_order_size']==1 and book['size_step']=='0.01'
    assert market.mark_price('POLYMARKET:'+TOKEN_ID)==pytest.approx(.325)


@pytest.mark.parametrize('wrapped', [False, True])
def test_public_get_retries_one_transient_read_without_signing(monkeypatch, wrapped):
    from ssl import SSLEOFError
    from urllib.error import URLError
    conn = PolymarketConnector(transport=FakePolymarketHttp())
    original = conn.transport.request
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, kwargs['timeout']))
        if len(calls) == 1:
            error = SSLEOFError('read interrupted')
            if wrapped:
                raise TradingError('network error') from URLError(error)
            raise error
        return original(method, url, **kwargs)
    monkeypatch.setattr(conn.transport, 'request', request)
    monkeypatch.setattr(conn, '_sdk', lambda: pytest.fail('public read accessed signer'))
    assert conn.get_order_book(TOKEN_ID)['bid'] == .3
    assert [method for method, _ in calls] == ['GET', 'GET']
    assert 0 < calls[1][1] <= calls[0][1]


@pytest.mark.parametrize('failure,attempts', [
    (ConnectionResetError('connection reset'), 2),
    (TradingError('invalid response'), 1),
])
def test_public_get_propagates_exhausted_or_non_network_failure(monkeypatch, failure, attempts):
    conn = PolymarketConnector()
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        raise failure
    monkeypatch.setattr(conn.transport, 'request', request)
    with pytest.raises(type(failure), match=str(failure)):
        conn.get_order_book(TOKEN_ID)
    assert len(calls) == attempts


@pytest.mark.parametrize('status', [400, 429, 500])
def test_public_get_does_not_retry_http_errors(monkeypatch, status):
    conn = PolymarketConnector()
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        return status, {'error': 'request rejected'}
    monkeypatch.setattr(conn.transport, 'request', request)
    with pytest.raises(TradingError, match=str(status)):
        conn._get(conn.clob_url, 'book')
    assert len(calls) == 1


def test_public_get_does_not_retry_certificate_failure(monkeypatch):
    from ssl import SSLCertVerificationError
    from urllib.error import URLError
    conn = PolymarketConnector()
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        raise TradingError('network error') from URLError(SSLCertVerificationError('invalid cert'))
    monkeypatch.setattr(conn.transport, 'request', request)
    with pytest.raises(TradingError):
        conn._get(conn.clob_url, 'book')
    assert len(calls) == 1


def test_public_get_does_not_retry_after_original_deadline(monkeypatch):
    conn = PolymarketConnector()
    calls = []
    clock = iter([100.0, 111.0])
    monkeypatch.setattr('nerya.connectors.polymarket.time.monotonic', lambda: next(clock))
    def request(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('read timed out')
    monkeypatch.setattr(conn.transport, 'request', request)
    with pytest.raises(TimeoutError, match='read timed out'):
        conn._get(conn.clob_url, 'book', timeout=10)
    assert len(calls) == 1
