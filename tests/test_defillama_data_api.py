from copy import deepcopy
from io import BytesIO

import pytest

from nerya.connectors.http import UrllibHttp
from nerya.data_api.defillama import DefiLlamaYieldProvider, defillama_specs, ORIGIN
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataApiContext, DataApiError
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
PID = 'aa70268e-4b52-42bf-a116-608b370f9501'
POOL = {'pool': PID, 'project': 'aave-v3', 'chain': 'Ethereum', 'symbol': 'USDC',
        'apy': 3.2, 'apyBase': 3.1, 'apyReward': None, 'apyBaseBorrow': 9.0,
        'tvlUsd': 1000, 'underlyingTokens': ['token'], 'poolMeta': None}
ROWS = [{'timestamp': '2026-10-20T00:00:00Z', 'apyBase': 3.1, 'apyReward': None, 'tvlUsd': 1000},
        {'timestamp': '2026-10-22T00:00:00.000Z', 'apyBase': 3.2, 'apyReward': None, 'tvlUsd': 1001}]

class Fake:
    def __init__(self, pools=None, rows=None, status=200):
        self.pools = deepcopy(pools if pools is not None else [POOL])
        self.rows = deepcopy(rows if rows is not None else ROWS)
        self.status = status
        self.calls = []
    def request(self, method, url, **kwargs):
        assert method == 'GET' and url in (ORIGIN + '/pools', ORIGIN + '/chart/' + PID)
        assert kwargs['timeout'] <= 15
        self.calls.append(url)
        return self.status, {'status': 'success', 'data': self.pools if url.endswith('/pools') else self.rows}

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('network forbidden')
    monkeypatch.setattr('socket.socket.connect', denied)
    monkeypatch.setattr('socket.create_connection', denied)

def history(fake, **args):
    return DefiLlamaYieldProvider(fake).yield_history(DataApiContext(),
        {'pool_id': PID, 'start': '2026-10-20', 'end': '2026-10-22', **args})

def test_history_dates_units_nulls_identity_and_gaps():
    fake = Fake(rows=list(reversed(ROWS)) + [ROWS[0]])
    out = history(fake)
    assert out['rows'][0]['deposit_base_apy_percent'] == 3.1
    assert out['rows'][0]['deposit_reward_apy_percent'] is None
    assert out['_envelope']['pool']['chain'] == 'Ethereum'
    assert out['_envelope']['apy_unit'] == 'percent_per_year'
    assert out['_envelope']['duplicate_timestamps_removed'] == 1
    assert out['_envelope']['coverage']['missing_utc_days'] == ['2026-10-21']
    assert out['_envelope']['coverage']['completeness'] == 'not_asserted'
    assert len(out['_envelope']['sources'][1]['sha256']) == 64
    assert 'borrow' not in str(out['rows']).lower()


def test_history_retains_same_source_brackets_without_changing_raw_coverage(tmp_path):
    import hashlib
    import json
    from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs
    rows = [{'timestamp': f'2026-10-{day}T00:00:00Z', 'apy': 5.0}
            for day in (19, 20, 21, 22, 23)]
    out = history(Fake(rows=rows), start='2026-10-20T12:00:00Z',
                  end='2026-10-22T12:00:00Z')
    envelope = out['_envelope']
    assert [r['timestamp'] for r in out['rows']] == [rows[2]['timestamp'], rows[3]['timestamp']]
    assert envelope['boundary_observations']['before']['timestamp'] == rows[1]['timestamp']
    assert envelope['boundary_observations']['after']['timestamp'] == rows[4]['timestamp']
    assert envelope['boundary_observations']['before']['pool_id'] == PID
    assert envelope['coverage']['completeness'] == 'not_asserted'
    assert envelope['coverage']['missing_utc_days'] == ['2026-10-20']
    assert envelope['coverage']['leading_gap_seconds'] == 43200
    assert envelope['coverage']['trailing_gap_seconds'] == 43200
    (tmp_path / 'replay_inputs.json').write_text(json.dumps({'version': 1, 'datasets': [
        {'id': 'rates', 'provider': 'defillama', 'action': 'yield_history', 'args': {}}]}))
    class Registry:
        def call_replay_input(self, *args, **kwargs):
            return out
    prepared = prepare_inputs(tmp_path, registry=Registry())
    blob = prepared['files']['rates.json']
    assert json.loads(blob)['_envelope']['boundary_observations'] == envelope['boundary_observations']
    assert prepared['receipts'][0]['sha256'] == hashlib.sha256(blob).hexdigest()
    assert prepared['receipts'][0]['requested_window_complete'] is False
    envelope['boundary_observations']['before']['deposit_apy_percent'] = 6.
    changed = prepare_inputs(tmp_path, registry=Registry())
    assert changed['receipts'][0]['sha256'] != prepared['receipts'][0]['sha256']

def test_native_handler_preserves_provenance_under_both_limits():
    registry = DataApiRegistry()
    for spec in defillama_specs(Fake()): registry.register_action(spec)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'defillama', 'action': 'yield_history',
        'args': {'pool_id': PID, 'start': '2026-10-20T00:00:00+00:00', 'end': '2026-10-22', 'limit': 2},
        'limit': 1}), registry=registry)
    assert not result.is_error, result.text()
    out = result.content[0].data
    assert out['row_count'] == 2 and len(out['rows']) == 1 and out['truncated']
    assert out['_envelope']['pool']['pool_id'] == PID
    assert out['_envelope']['coverage']['observations'] == 2
    assert out['_envelope']['requested_window']['start'].startswith('2026-10-20')
    assert out['source_url'] == ORIGIN + '/chart/' + PID

def test_pool_filters_and_borrow_not_substituted():
    wrong = {**POOL, 'pool': 'ba70268e-4b52-42bf-a116-608b370f9501', 'chain': 'Base', 'poolMeta': ''}
    out = DefiLlamaYieldProvider(Fake(pools=[POOL, wrong])).yield_pools(DataApiContext(),
        {'project': 'aave-v3', 'chain': 'Ethereum', 'symbol': 'USDC', 'underlying_token': 'token'})
    assert len(out['rows']) == 1
    assert out['rows'][0]['deposit_base_apy_percent'] == 3.1

@pytest.mark.parametrize('args', [
    {'pool_id': '../secret'}, {'pool_id': 'https://evil.invalid'}, {'limit': True},
    {'limit': 501}, {'start': '2026-10-23'}, {'start': '2020-01-01'},
    {'end': '2026-10-22T00:00:00+08:00'}, {'start': 'invalid'}, {'url': 'https://evil.invalid'},
])
def test_invalid_request_rejected_before_http(args):
    fake = Fake()
    with pytest.raises(DataApiError): history(fake, **args)
    assert not fake.calls

@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, '3.2'])
def test_invalid_numbers_fail_closed(value):
    with pytest.raises(DataApiError): history(Fake(rows=[{**ROWS[0], 'apyBase': value}]))

def test_conflicting_duplicate_timestamp_and_pool_fail_closed():
    with pytest.raises(DataApiError): history(Fake(rows=[ROWS[0], {**ROWS[0], 'apyBase': 4}]))
    with pytest.raises(DataApiError): history(Fake(pools=[POOL, {**POOL, 'chain': 'Other'}]))

@pytest.mark.parametrize('status,retryable', [(404, False), (429, True), (503, True)])
def test_http_error_has_no_mock_fallback(status, retryable):
    with pytest.raises(DataApiError) as exc: history(Fake(status=status))
    assert exc.value.retryable is retryable

@pytest.mark.parametrize('raw', [b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}', b'not json'])
def test_strict_transport_rejects_invalid_json(raw):
    with pytest.raises(ValueError): UrllibHttp(strict_json=True)._read_json(BytesIO(raw))

def test_transport_size_and_origin_bounds():
    http = UrllibHttp(max_response_bytes=2, allowed_origins=(ORIGIN,), strict_json=True)
    with pytest.raises(ValueError): http._read_json(BytesIO(b'{"x":1}'))
    for url in ('http://yields.llama.fi/pools', 'https://evil.invalid/pools', 'https://user@yields.llama.fi/pools'):
        with pytest.raises(ValueError): http._check_origin(url)
    http._check_origin(ORIGIN + '/pools')


def test_registry_discovery_and_schema_do_not_fetch(monkeypatch):
    from nerya.data_api import build_data_api_registry
    monkeypatch.setattr(UrllibHttp, 'request', lambda *a, **kw: pytest.fail('discovery performed IO'))
    reg = build_data_api_registry()
    assert {r['action'] for r in reg.list(provider='defillama')['actions']} == {'yield_pools','yield_history'}
    assert 'pool_id' in reg.schema('defillama', 'yield_history')['input_schema']['required']


def test_allowlisted_transport_rejects_cross_origin_redirect_before_request(monkeypatch):
    import urllib.request
    http = UrllibHttp(allowed_origins=(ORIGIN,), strict_json=True)
    calls = []
    class Opener:
        def open(self, req, timeout):
            calls.append(req.full_url)
            raise AssertionError('unexpected real opener')
    def build(handler):
        with pytest.raises(ValueError, match='origin'):
            handler.redirect_request(urllib.request.Request(ORIGIN + '/pools'), None, 302, '', {},
                                     'http://127.0.0.1/private')
        raise ValueError('redirect rejected')
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    with pytest.raises(ValueError, match='redirect rejected'):
        http.request('GET', ORIGIN + '/pools')
    assert calls == []
