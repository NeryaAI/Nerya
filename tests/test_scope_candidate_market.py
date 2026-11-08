"""Candidate identifiers are not durations; qualified markets keep exact spans."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.db.sqlite import connect_readonly
from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, admit, spans_output
import test_request_scope as request_scope_fixtures

env = request_scope_fixtures.env

pytestmark = pytest.mark.smoke
TEXT = ('候选 prp_f02d2714ecc7，BINANCE:BTCUSDT，1h，'
        '2026-10-23T12:00:00Z 至 2026-10-30T12:00:00Z，仅 paper 回测。')


def extraction(doc):
    result = spans_output(doc, venue='BINANCE', base='BTC', quote='USDT', timeframe='1h',
                          start_utc='2026-10-23T12:00:00Z', end_utc='2026-10-30T12:00:00Z')
    span = spans_output(doc, asset='仅 paper 回测')['fields']['asset']
    result['excluded_spans'] = [dict(span, role='execution_control')]
    return result


@pytest.mark.parametrize('excluded', [False, True])
def test_candidate_id_and_qualified_market_roundtrip_exact_paper_scope(env, excluded):
    admit(env, cid='candidate-scope', sid='candidate-scope', text=TEXT)

    def choose(docs):
        result = extraction(docs[0])
        if excluded:
            span = spans_output(docs[0], asset='prp_f02d2714ecc7')['fields']['asset']
            result['excluded_spans'].append(dict(span, role='authorization'))
        return result

    gateway = FakeGateway(choose)
    before = deepcopy(env.data)
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['constraints'] == {
        'venue': 'binance', 'base': 'BTC', 'quote': 'USDT', 'timeframe': 3600,
        'start_utc': scope._timestamp('2026-10-23T12:00:00Z'),
        'end_utc': scope._timestamp('2026-10-30T12:00:00Z'),
        'resources': [{'venue': 'binance', 'base': 'BTC', 'quote': 'USDT'}]}
    assert contract['evidence'] == contract['normalized_evidence'] == choose(contract['binding']['documents'])
    assert contract['revision'] == 1
    manifest = {'markets': ['BINANCE:BTCUSDT'], 'timeframe': '1h'}
    assert scope.compare(contract, manifest) == {
        'start_utc': '2026-10-23T12:00:00Z', 'end_utc': '2026-10-30T12:00:00Z'}
    for changed in ({'markets': ['BINANCE:BTCUSDC']}, {'timeframe': '4h'},
                    {'backtest': {'start_utc': '2026-10-24T12:00:00Z'}}):
        with pytest.raises(scope.ScopeError) as raised:
            scope.compare(contract, {**manifest, **changed})
        assert raised.value.reason == 'request_scope_mismatch'
    assert scope.current_contract(env) == contract
    assert scope.ensure_contract(env, gateway=gateway) == contract
    assert len(gateway.calls) == 1
    assert env.data == before
    assert not env.paths.proposals.exists()


def test_equivalent_slash_pair_is_covered_by_qualified_market(env):
    text = ('候选 prp_f02d2714ecc7，BTC/USDT EMA 策略，市场 BINANCE:BTCUSDT，1h，'
            '2026-10-23T12:00:00Z 至 2026-10-30T12:00:00Z，仅 paper 回测。')
    admit(env, cid='equivalent-pair', sid='equivalent-pair', text=text)
    def choose(documents):
        result = extraction(documents[0])
        qualified_start = documents[0]['text'].index('BINANCE:BTCUSDT')
        result['fields']['base'] = {
            'command_id': documents[0]['command_id'],
            'start': qualified_start + len('BINANCE:'),
            'end': qualified_start + len('BINANCE:BTC'),
            'text': 'BTC',
        }
        result['fields']['quote'] = {
            'command_id': documents[0]['command_id'],
            'start': qualified_start + len('BINANCE:BTC'),
            'end': qualified_start + len('BINANCE:BTCUSDT'),
            'text': 'USDT',
        }
        return result

    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints']['resources'] == [
        {'venue': 'binance', 'base': 'BTC', 'quote': 'USDT'}]


@pytest.mark.parametrize('suffix,missing_pair', [
    ('', None),
    (' 查看 artifacts/report.json、/TMP/REPORT、reports/BTC/USDC.json。', None),
    (' 操作标签 collect/verify、read/write。', None),
    (' 同一市场 btc/usdt。', None),
    (' 另含 ETH/USDT。', 'ETH'),
    (' 另含 BTC/USDC。', 'BTC'),
    (' 另含 eth/usdt。', 'eth'),
])
def test_operational_slashes_are_not_pairs_but_omitted_assets_fail_closed(env, suffix, missing_pair):
    text = ('使用当前有效的 bounded retrospective replay 授权，对候选 prp_65eb85296c74 运行同一范围回放：'
            'BINANCE:BTCUSDT，1h，2026-09-30T00:00:00Z 至 2026-10-30T00:00:00Z；'
            '仅 paper，不改候选、不启用交易或调度。请读取 producer receipt/report 并展开最终结果。')
    assert text[168:175] == 'receipt'
    admit(env, cid='operational-slash', sid='operational-slash', text=text + suffix)
    def choose(docs):
        result = spans_output(
            docs[0], venue='BINANCE', base='BTC', quote='USDT', timeframe='1h',
            start_utc='2026-09-30T00:00:00Z', end_utc='2026-10-30T00:00:00Z')
        control = spans_output(docs[0], asset='仅 paper，不改候选、不启用交易或调度')['fields']['asset']
        result['excluded_spans'] = [dict(control, role='execution_control')]
        return result

    gateway = FakeGateway(choose)
    if missing_pair:
        with pytest.raises(scope.ScopeError) as raised:
            scope.ensure_contract(env, gateway=gateway)
        assert raised.value.diagnostic_code == 'incomplete_scope_evidence'
        metadata = raised.value.diagnostic_metadata
        assert metadata['reason'] in {'uncovered_pair_asset', 'uncovered_native_name'}
        assert (text + suffix)[metadata['token_start']:metadata['token_end']] == missing_pair
    else:
        contract = scope.ensure_contract(env, gateway=gateway)
        assert contract['constraints']['resources'] == [
            {'venue': 'binance', 'base': 'BTC', 'quote': 'USDT'}]


@pytest.mark.parametrize('duration', ['1h', '30m', '2 days', '7天', '最近30天', 'last30days', 'past7days'])
@pytest.mark.parametrize('excluded', [False, True])
def test_real_omitted_durations_still_fail_closed(env, duration, excluded):
    text = TEXT + ' ' + duration
    admit(env, cid='missing-duration', sid='missing-duration', text=text)

    def choose(docs):
        result = extraction(docs[0])
        if excluded:
            start = len(TEXT) + 1
            result['excluded_spans'].append(dict(command_id=docs[0]['command_id'],
                start=start, end=len(text), text=duration, role='execution_control'))
        return result

    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert raised.value.diagnostic_code == 'incomplete_scope_evidence'
    assert raised.value.diagnostic_metadata['reason'] == 'uncovered_duration'
    with closing(connect_readonly(env.paths.db)) as con:
        context = json.loads(con.execute(
            "SELECT context_json FROM agent_commands WHERE command_id='missing-duration'").fetchone()[0])
    assert 'request_scope' not in context


@pytest.mark.parametrize('market,values', [
    ('BTCUSDT', {'venue': None}),
    ('BINANCE:BTC USDT', {}),
    ('BINANCE:BTCUSDT.extra', {}),
    ('BINANCE:BTCUSDT:USDT', {}),
    ('BINANCE:BTCUSDT', {'base': 'BT', 'quote': 'CUSDT'}),
    ('BINANCE:BTCUSDT', {'base': 'BTCUS', 'quote': 'USDT'}),
])
def test_compact_market_requires_complete_unambiguous_qualified_spans(env, market, values):
    admit(env, cid='invalid-market', sid='invalid-market', text=TEXT.replace('BINANCE:BTCUSDT', market))

    def choose(docs):
        result = extraction(docs[0])
        for key, value in values.items():
            result['fields'][key] = (spans_output(docs[0], **{key: value})['fields'][key]
                                     if value is not None else None)
        return result

    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(choose))
