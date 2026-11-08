"""Protocol identity coverage never grants asset or window authority."""
from copy import deepcopy

import pytest

from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, assistant_chain, spans_output
import test_request_scope as request_scope_fixtures

env = request_scope_fixtures.env
import test_scope_pinned_window_restatement as scope_pinned_window_restatement_fixtures
from test_scope_pinned_window_restatement import START, END, append, contexts

pinned = scope_pinned_window_restatement_fixtures.pinned

pytestmark = pytest.mark.smoke


def inspection():
    prefix = f'Inspect start={START},end={END}; retain original scope. '
    return prefix.ljust(149, '.') + 'markets=[UNISWAP_V3:ETH/USDC]'


def restated(prior, doc):
    # Retain the authoritative relative window; exercise amendment coverage
    # without claiming a new window or paired-bound normalization.
    result = deepcopy(prior['normalized_evidence'])
    result['amendments'].append(dict(command_id=doc['command_id'], start=0,
                                    end=len(doc['text']), text=doc['text']))
    return result


def test_exact_protocol_identity_coverage_only(env):
    text = 'Uniswap V3 ETH/USDC last 30 days; protocol=UNISWAP_V3'

    def initial(docs):
        return spans_output(docs[0], protocol='Uniswap V3', base='ETH',
                            quote='USDC', window_days='30 days')

    original = assistant_chain(env, 'Continue.', initial_text=text, initial_choose=initial)
    contract = scope.ensure_contract(env, gateway=FakeGateway(initial))
    assert contract['constraints'] == original['constraints']
    assert contract['revision'] == original['revision']
    assert scope.current_contract(env) == contract
    snapshot = scope._snapshot(env)
    inventory = scope._ScopeInventory()
    assert scope._protocol('UNISWAP_V3', inventory)['component_id'] == scope._protocol('UNISWAP_V4', inventory)['component_id']
    for name in ('UNISWAP_V4', 'Aave', 'Pancakeswap', 'Binance'):
        changed = deepcopy(snapshot)
        changed['binding']['documents'][0]['text'] = text.replace('protocol=UNISWAP_V3', 'protocol=' + name)
        with pytest.raises(scope.ScopeError) as raised:
            scope._canonical(initial(changed['binding']['documents']), changed, inventory=inventory)
        assert raised.value.diagnostic_code == 'incomplete_scope_evidence'
    missing = initial(snapshot['binding']['documents'])
    missing['fields']['protocol'] = None
    with pytest.raises(scope.ScopeError):
        scope._canonical(missing, snapshot, inventory=inventory)


def test_document_9_advances_to_existing_eth_rejection(pinned):
    config, prior = pinned
    text = inspection()
    assert text[158:168] == 'UNISWAP_V3'
    append(config, prior, text, 9)
    before = contexts(config)
    gateway = FakeGateway(lambda docs: restated(prior, docs[-1]))
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config, gateway=gateway)
    assert raised.value.diagnostic_code == 'incomplete_scope_evidence'
    assert raised.value.diagnostic_metadata == {
        'reason': 'uncovered_native_name', 'source_document_index': 9,
        'token_start': 169, 'token_end': 172, 'field': 'base'}
    assert len(gateway.calls) == 1 and contexts(config) == before
    assert scope.current_contract(config, command_id='continue-8') == prior


def test_old_eth_pair_cannot_revert_latest_weth(pinned):
    config, prior = pinned
    assert prior['constraints']['base'] == 'WETH'
    append(config, prior, inspection(), 9)
    before = contexts(config)

    def choose(docs):
        result = restated(prior, docs[-1])
        # Attempt to resurrect superseded original authority, not a newly
        # admitted asset change. The existing chain guard must reject it.
        result['fields']['base'] = spans_output(docs[0], base='ETH')['fields']['base']
        return result

    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=FakeGateway(choose))
    assert contexts(config) == before
    saved = scope.current_contract(config, command_id='continue-8')
    assert saved == prior and saved['constraints']['resources'][0]['base'] == 'WETH'
