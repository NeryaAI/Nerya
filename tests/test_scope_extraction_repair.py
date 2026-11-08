"""Bounded extraction repair never relaxes the host's span authority."""
from copy import deepcopy
from contextlib import closing
import json

import pytest

from nerya.strategies import request_scope as scope
from nerya.db.sqlite import connect
from test_request_scope import FakeGateway, assistant_chain, spans_output
from test_request_scope import env as scope_env
from test_scope_pinned_window_restatement import contexts

env = scope_env
pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('repair', [True, False])
def test_outside_amendment_has_one_diagnostic_correction_and_same_authority(env, repair):
    text = 'Change quote to USDC; confirm.'
    prior = assistant_chain(env, text)
    before = contexts(env)
    calls = []

    def choose(docs):
        result = deepcopy(prior['normalized_evidence'])
        result['fields']['quote'] = spans_output(docs[-1], quote='USDC')['fields']['quote']
        quote = text if repair and calls else 'confirm'
        result['amendments'] = [spans_output(docs[-1], asset=quote)['fields']['asset']]
        calls.append(result)
        return result

    gateway = FakeGateway(choose)
    if repair:
        result = scope.ensure_contract(env, gateway=gateway)
        assert result['constraints']['quote'] == 'USDC'
        assert result['constraints']['base'] == prior['constraints']['base']
    else:
        with pytest.raises(scope.ScopeError) as raised:
            scope.ensure_contract(env, gateway=gateway)
        assert raised.value.diagnostic_metadata['reason'] == 'outside_amendment_span'
        assert contexts(env) == before
    assert len(gateway.calls) == 2
    first, second = gateway.calls
    assert first['messages'] == second['messages']
    assert first['tools'] == second['tools']
    assert first['model_id'] == second['model_id']
    assert 'extraction_repair' not in first['system']
    assert 'extraction_repair' in second['system']
    assert 'outside_amendment_span' in second['system']
    # No provider body or rejected model output is used as user authority.
    assert json.loads(second['messages'][0]['content']) == scope._snapshot(env)['binding']['documents']


def test_corrected_selection_cannot_persist_after_request_tampering(env):
    text = 'Change quote to USDC; confirm.'
    prior = assistant_chain(env, text)
    attempts = []
    def choose(docs):
        result = deepcopy(prior['normalized_evidence'])
        result['fields']['quote'] = spans_output(docs[-1], quote='USDC')['fields']['quote']
        result['amendments'] = [spans_output(docs[-1], asset=text if attempts else 'confirm')['fields']['asset']]
        if attempts:
            with closing(connect(env.paths.db)) as con, con:
                con.execute("UPDATE agent_commands SET request_json='{}',revision=revision+1 WHERE command_id='child-follow'")
        attempts.append(True)
        return result
    gateway = FakeGateway(choose)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == 2
    assert 'request_scope' not in json.loads(contexts(env)['child-follow'])


@pytest.mark.parametrize('repair', [True, False])
def test_mixed_window_evidence_is_diagnosed_not_silently_accepted(env, repair):
    from test_scope_pinned_window_restatement import START, END
    prior = assistant_chain(env, f'Inspect start={START}, end={END}.')
    attempts = []
    def choose(docs):
        result = deepcopy(prior['normalized_evidence'])
        if not attempts or not repair:
            result['fields'].update({key: value for key, value in
                spans_output(docs[-1], start_utc=START, end_utc=END)['fields'].items()
                if value is not None})
            result['amendments'] = [spans_output(docs[-1], asset=docs[-1]['text'])['fields']['asset']]
        attempts.append(True)
        return result
    gateway = FakeGateway(choose)
    if repair:
        result = scope.ensure_contract(env, gateway=gateway)
        assert result['constraints'] == prior['constraints']
    else:
        with pytest.raises(scope.ScopeError) as raised:
            scope.ensure_contract(env, gateway=gateway)
        assert raised.value.diagnostic_metadata['reason'] == 'mixed_window_evidence'
        assert 'request_scope' not in json.loads(contexts(env)['child-follow'])
    assert len(gateway.calls) == 2
    assert 'mixed_window_evidence' in gateway.calls[-1]['system']
