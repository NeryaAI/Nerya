"""Offline fixture checks, not live replay evidence."""
from copy import deepcopy
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from nerya.data_api import build_data_api_registry
from nerya.data_api.geckoterminal import GeckoTerminalProvider, geckoterminal_specs
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataApiContext, DataApiError
from nerya.harness.cancellation import CancelToken, CancelledError
from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs

pytestmark = pytest.mark.smoke
POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE'
BASE = 'So11111111111111111111111111111111111111112'
QUOTE = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
ARGS = dict(network='solana', pool_address=POOL, base_token=BASE, quote_token=QUOTE,
            start='2026-10-26T00:00:00Z', end='2026-11-02T00:00:00Z',
            timeframe='1h', warmup_bars=50)


class Fake:
    def __init__(self):
        self.calls = []
        self.item = {'type': 'pool', 'id': 'solana_' + POOL, 'attributes': {'address': POOL},
            'relationships': {**{side + '_token': {'data': {'id': 'solana_' + mint, 'type': 'token'}}
                for side, mint in [('base', BASE), ('quote', QUOTE)]},
                'dex': {'data': {'id': 'orca', 'type': 'dex'}}}}
        self.included = [{'id': 'solana_' + mint, 'type': 'token',
            'attributes': {'address': mint, 'symbol': symbol, 'decimals': decimals}}
            for mint, symbol, decimals in [(BASE, 'SOL', 9), (QUOTE, 'USDC', 6)]]
        self.candles = {'data': {'type': 'ohlcv_request_response', 'attributes': {'ohlcv_list':
            [[ts, 94., 96., 93., 95., 100.] for ts in reversed(range(1792792800, 1793577600, 3600))]}},
            'meta': {'base': {'address': BASE}, 'quote': {'address': QUOTE}}}
        self.catalog = [{'id': BASE, 'symbol': 'SOL', 'decimals': 9, 'isVerified': True},
                        {'id': QUOTE, 'symbol': 'USDC', 'decimals': 6, 'tags': ['verified']}]

    def request(self, method, url, **kwargs):
        assert method == 'GET' and urlsplit(url).netloc in ('api.geckoterminal.com', 'lite-api.jup.ag')
        assert kwargs['timeout'] <= 15
        self.calls.append(url)
        if urlsplit(url).netloc == 'lite-api.jup.ag':
            assert urlsplit(url).path == '/tokens/v2/search'
            assert parse_qs(urlsplit(url).query)['query'] in ([BASE + ',' + QUOTE], ['SOL,USDC'])
            return 200, deepcopy(self.catalog)
        if '/ohlcv/' in url:
            query = parse_qs(urlsplit(url).query)
            assert query['currency'] == ['token'] and query['token'] == ['base']
            assert query['before_timestamp'] == ['1793577599']
            return 200, deepcopy(self.candles)
        return 200, {'data': [deepcopy(self.item)] if '/tokens/' in url else deepcopy(self.item),
                     'included': deepcopy(self.included)}


def test_pool_identity_prices_coverage_and_prepared_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr('socket.socket.connect', lambda *a, **kw: pytest.fail('network forbidden'))
    fake = Fake()
    provider = GeckoTerminalProvider(fake)
    discovery = provider.pool_discover(DataApiContext(), {k: ARGS[k] for k in ('network', 'base_token', 'quote_token')})
    assert discovery['rows'][0]['address'] == POOL
    assert discovery['_envelope']['catalog_complete'] is False
    reg = DataApiRegistry()
    for spec in geckoterminal_specs(fake):
        reg.register_action(spec)
    assert build_data_api_registry().schema('geckoterminal', 'pool_ohlcv')['replay_input']
    declaration = {'version': 1, 'datasets': [{'id': 'pool_prices', 'provider': 'geckoterminal',
        'action': 'pool_ohlcv', 'args': ARGS}]}
    (tmp_path / 'replay_inputs.json').write_text(json.dumps(declaration))
    prepared = prepare_inputs(tmp_path, registry=reg)
    blob = prepared['files']['pool_prices.json']
    out = json.loads(blob)
    env = out['_envelope']
    receipt = prepared['receipts'][0]
    assert len(out['rows']) == 218 and out['rows'][0]['ts'] == 1792792800
    assert set(out['rows'][0]) == {'ts', 'open', 'high', 'low', 'close', 'volume'}
    assert env['pool']['base'] == {'address': BASE, 'symbol': 'SOL', 'decimals': 9}
    assert env['pool']['quote']['address'] == QUOTE and env['pool']['dex'] == 'orca'
    assert env['quote_units'] == env['price_units'] == 'quote_token_per_base_token'
    assert env['volume_units'] == 'upstream_reported_usd' and env['volume_available'] is False
    assert env['coverage']['warmup_complete'] and receipt['requested_window_complete']
    assert receipt['sha256'] == hashlib.sha256(blob).hexdigest()
    assert receipt['sources'] == env['sources'] and len(env['sources']) == 3
    assert all(len(source['sha256']) == 64 for source in env['sources'])
    assert len(fake.calls) == 4  # discovery + metadata + candles + official token identities
    assert env['request_count'] == 3
    assert env['token_identities'] == [
        {**env['pool'][side], 'identity_scope': 'current_official_catalog',
         'catalog_source_sha256': env['sources'][2]['sha256']} for side in ('base', 'quote')]
    assert env['sources'][2]['sha256'] == hashlib.sha256(json.dumps(
        fake.catalog, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()
    catalog_http = GeckoTerminalProvider().http
    for origin in ('https://api.geckoterminal.com', 'https://lite-api.jup.ag'):
        catalog_http._check_origin(origin + '/public')
    with pytest.raises(ValueError):
        catalog_http._check_origin('https://other.invalid/public')
    for damage in ('isVerified', 'symbol', 'decimals', 'id'):
        original = deepcopy(fake.catalog)
        fake.catalog[0][damage] = {'isVerified': False, 'symbol': 'IMPOSTOR',
                                   'decimals': 6, 'id': QUOTE}[damage]
        with pytest.raises(DataApiError, match='official token|Official token'):
            provider.pool_ohlcv(DataApiContext(), ARGS)
        fake.catalog = original

    # One missing warmup bar cannot silently turn into full-window admission.
    fake.candles['data']['attributes']['ohlcv_list'].pop()
    incomplete = prepare_inputs(tmp_path, registry=reg)
    assert incomplete['receipts'][0]['requested_window_complete'] is False
    assert not json.loads(incomplete['files']['pool_prices.json'])['_envelope']['coverage']['warmup_complete']

    # Exact quote metadata is mandatory; token/USD or a different mint fails closed.
    fake.candles['meta']['quote']['address'] = BASE
    with pytest.raises(DataApiError, match='identity mismatch'):
        provider.pool_ohlcv(DataApiContext(), ARGS)
    fake.candles['meta']['quote']['address'] = QUOTE
    fake.item['attributes']['address'] = BASE
    with pytest.raises(DataApiError, match='identity mismatch'):
        provider.pool_ohlcv(DataApiContext(), ARGS)
    fake.item['attributes']['address'] = POOL
    for change in ({'url': 'https://evil.invalid'}, {'warmup_bars': True},
                   {'end': '2026-11-02T00:01:00Z'}, {'start': '2026-01-01'},
                   {'timeframe': 'minute'}, {'quote_token': '../secret'}):
        count = len(fake.calls)
        with pytest.raises(DataApiError):
            provider.pool_ohlcv(DataApiContext(), {**ARGS, **change})
        assert len(fake.calls) == count
    token = CancelToken()
    token.cancel('stop')
    count = len(fake.calls)
    with pytest.raises(CancelledError):
        provider.pool_ohlcv(DataApiContext(cancel_token=token), ARGS)
    assert len(fake.calls) == count
def test_pair_discovery_resolves_symbols_and_marks_pool_handoff() -> None:
    fake = Fake()
    provider = GeckoTerminalProvider(fake)
    out = provider.pair_discover(DataApiContext(), {
        'network': 'solana', 'base_symbol': 'SOL', 'quote_symbol': 'USDC',
    })
    env = out['_envelope']
    assert [row['symbol'] for row in out['token_identities']] == ['SOL', 'USDC']
    assert out['rows'][0]['address'] == POOL
    assert env['source_role'] == 'alternative_pool_price'
    assert env['historical_data_verified'] is False
    assert 'historical_jupiter_routes' in env['incompatible_observations']


