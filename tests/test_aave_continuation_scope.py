"""Aave rate-scope continuation keeps the selected pool identity."""

from copy import deepcopy
import json

from nerya.strategies import request_scope as scope
from nerya.tools.native.financial import _lending_pool_identity
import test_request_scope as request_scope_fixtures
from test_request_scope import FakeGateway, spans_output

env = request_scope_fixtures.env

import pytest

pytestmark = pytest.mark.smoke


def test_aave_question_answer_binds_pool_without_breaking_lending_resource():
    original = {
        'command_id': 'aave-original',
        'text': '创建一个 Aave V3 Ethereum USDC 收益率策略，回测最近30天，不交易。',
        'created_at': 1793838257,
        'request_hash': 'original',
    }
    answer = {
        'command_id': 'aave-answer',
        'text': '主 USDC reserve（pool_id=aa70268e-4b52-42bf-a116-608b370f9501）\\n最近 30 天',
        'created_at': 1793838447,
        'request_hash': 'answer',
    }
    snapshot = {
        'binding': {
            'command_id': answer['command_id'],
            'original_command_id': original['command_id'],
            'session_id': 'aave-session',
            'actor_id': 'owner',
            'documents': [original, answer],
        },
        'scope_answers': [answer['command_id']],
    }
    snapshot['input_hash'] = scope._hash(snapshot['binding'])

    initial = spans_output(
        original, protocol='Aave V3', chain='Ethereum', asset='USDC', window_days='最近30天')
    selected = spans_output(
        original, protocol='Aave V3', chain='Ethereum', asset='USDC', window_days='最近30天')
    selected['fields']['pool'] = spans_output(
        answer, pool='aa70268e-4b52-42bf-a116-608b370f9501')['fields']['pool']

    contract = scope._chain([initial, selected], snapshot)

    assert contract['constraints']['resources'] == [{
        'protocol': scope._protocol('aave_v3'),
        'chain': 'ethereum',
        'asset': 'USDC',
        'pool': 'aa70268e-4b52-42bf-a116-608b370f9501',
    }]
    assert contract['constraints']['window_days'] == 30


@pytest.mark.parametrize('correction', ['identifier', 'still_invalid', 'invented'])
def test_delegated_aave_answer_repairs_only_rejected_pool_span_once(env, correction):
    # Actual admitted request/selected answer from taskchat_692af861. The
    # rejected model span was not retained: recreate its exact host rejection
    # with the full selected label, rather than claim a captured extraction.
    pool_id = 'aa70268e-4b52-42bf-a116-608b370f9501'
    selected = ('普通 Aave V3 USDC 供应池（无特殊标签，原生 USDC 0xA0b8...eB48，'
                '当前 APY ≈3.82%，TVL ≈$139M）— pool_id: ' + pool_id)
    documents = [
        dict(command_id='aave-original', created_at=1793966225.2190259,
             request_hash='original',
             text='创建一个 Ethereum Aave V3 USDC 收益率策略，回测最近30天并生成报告，不交易。'),
        dict(command_id='aave-answer', created_at=1793966777.887949,
             request_hash='answer', text=selected),
    ]
    snapshot = {'binding': {'command_id': 'aave-answer',
        'original_command_id': 'aave-original', 'session_id': 'aave-session',
        'actor_id': 'owner', 'documents': documents},
        'scope_answers': ['aave-answer'],
        'request': {'model_provider': 'openai', 'model_id': 'fixture-model'}}
    snapshot['input_hash'] = scope._hash(snapshot['binding'])
    initial = spans_output(documents[0], protocol='Aave V3', chain='Ethereum',
                           asset='USDC', window_days='最近30天')
    prior = scope._chain([initial], scope._prefix(snapshot, 1))
    before = deepcopy((snapshot, prior))
    malformed = deepcopy(initial)
    malformed['fields']['pool'] = spans_output(documents[1], pool=selected)['fields']['pool']
    with pytest.raises(scope.ScopeError) as rejected:
        scope._canonical(malformed, snapshot, prior, previous=prior)
    assert rejected.value.diagnostic_metadata == {
        'field': 'pool', 'reason': 'invalid_pool_identity', 'source_document_index': 1}

    def choose(docs):
        evidence = deepcopy(malformed)
        if len(gateway.calls) == 2 and correction != 'still_invalid':
            evidence['fields']['pool'] = spans_output(docs[1], pool=pool_id)['fields']['pool']
            if correction == 'invented':
                evidence['fields']['pool']['text'] = '11111111-2222-4333-8444-555555555555'
        return evidence

    gateway = FakeGateway(choose)
    def extract():
        # Delegated interaction_continue uses the single-evidence path, not
        # the direct user_chat task_tail envelope used by sampled LP tasks.
        return scope._extract(env, snapshot, gateway=gateway, original=prior, previous=prior)

    if correction == 'identifier':
        extracted = extract()
        contract = scope._chain([initial, extracted['evidence']], snapshot)
        expected = deepcopy(prior['constraints'])
        expected['pool'] = expected['resources'][0]['pool'] = pool_id
        assert contract['constraints'] == expected
        identity = _lending_pool_identity(dict(project='aave-v3', chain='Ethereum',
                                              symbol='USDC', pool_id=pool_id))
        manifest = {'markets': [identity['market']], 'params': {'rate_source': {'pool_id': pool_id}}}
        assert scope.compare(contract, manifest) == {
            'start_utc': '2026-10-07T00:00:00Z', 'end_utc': '2026-11-06T00:00:00Z'}
        for market in ('AAVE_V3:arbitrum-USDC', 'AAVE_V3:ethereum-DAI', 'COMPOUND:ethereum-USDC'):
            with pytest.raises(scope.ScopeError):
                scope.compare(contract, {**manifest, 'markets': [market]})
        with pytest.raises(scope.ScopeError):
            scope.compare(contract, {**manifest, 'backtest': {'window_days': 7}})
        with pytest.raises(scope.ScopeError):
            scope.compare(contract, {**manifest, 'params': {'rate_source': {'pool_id': 'other-pool'}}})
    else:
        with pytest.raises(scope.ScopeError) as failed:
            extract()
        assert failed.value.diagnostic_code == ('invalid_span' if correction == 'invented' else 'unverified_scope')
    assert len(gateway.calls) == 2
    first, second = gateway.calls
    assert first['messages'] == second['messages']
    assert first['tools'] == second['tools'] and first['model_id'] == second['model_id']
    assert json.loads(second['messages'][0]['content']) == documents
    assert 'invalid_pool_identity' in second['system']
    assert (snapshot, prior) == before
    assert not env.paths.proposals.exists()
