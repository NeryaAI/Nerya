"""Pair restatements retain role and chronological amendment authority."""
from copy import deepcopy

import pytest

from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, assistant_chain, spans_output
from test_request_scope import env as scope_env
from test_scope_asset_alias_amendment import INITIAL, FOLLOWUP, initial_output
from test_scope_pinned_window_restatement import append, contexts

pytestmark = pytest.mark.smoke
env = scope_env


def inspected(prior, doc):
    result = deepcopy(prior['normalized_evidence'])
    result['amendments'].append(dict(command_id=doc['command_id'], start=0,
                                    end=len(doc['text']), text=doc['text']))
    return result


def test_historical_pair_before_wrapped_asset_amendment_remains_readable(env):
    original = assistant_chain(env, 'Read references/history-data.md; inspect markets=[UNISWAP_V3:ETH/USDC].',
                               initial_text=INITIAL, initial_choose=initial_output)
    prior = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: inspected(original, docs[-1])))
    assert prior['constraints'] == original['constraints']
    append(env, prior, FOLLOWUP, 2)

    def amend(docs):
        result = inspected(prior, docs[-1])
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        return result

    changed = scope.ensure_contract(env, gateway=FakeGateway(amend))
    assert changed['constraints']['base'] == 'WETH'
    assert changed['constraints']['resources'][0]['base'] == 'WETH'
    assert scope.current_contract(env) == changed
    assert scope.ensure_contract(env, gateway=FakeGateway()) == changed
    append(env, changed, 'Continue.', 3)
    continued = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: deepcopy(changed['evidence'])))
    assert continued['constraints'] == changed['constraints']


@pytest.mark.parametrize('pair', ['ETH/DAI', 'USDC/ETH', 'WETH/USDC', 'BNB/USDC'])
def test_omitted_new_pair_or_role_is_not_restatement(env, pair):
    original = assistant_chain(env, f'Inspect markets=[UNISWAP_V3:{pair}].',
                               initial_text=INITIAL, initial_choose=initial_output)
    before = contexts(env)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(lambda docs: inspected(original, docs[-1])))
    assert contexts(env) == before


def test_later_old_pair_cannot_be_hidden_by_prior_asset_amendment(env):
    original = assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial_output)
    def amend(docs):
        result = inspected(original, docs[-1])
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        return result
    changed = scope.ensure_contract(env, gateway=FakeGateway(amend))
    append(env, changed, 'Use UNISWAP_V3:ETH/USDC.', 2)
    before = contexts(env)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(lambda docs: inspected(changed, docs[-1])))
    assert contexts(env) == before
