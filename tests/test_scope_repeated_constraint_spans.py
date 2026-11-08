"""Repeated quotations inherit only the latest verified same-role evidence."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.db.sqlite import connect, connect_readonly
from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, admit, assistant_chain, spans_output
import test_request_scope as request_scope_fixtures

env = request_scope_fixtures.env
from test_scope_asset_alias_amendment import INITIAL, FOLLOWUP, initial_output, amended_output

pytestmark = pytest.mark.smoke


def context(config, cid='child-follow'):
    with closing(connect_readonly(config.paths.db)) as con:
        row = con.execute('SELECT context_json FROM agent_commands WHERE command_id=?', (cid,)).fetchone()
    return json.loads(row[0])


def repeat(prior, doc, **values):
    result = deepcopy(prior['evidence'])
    for key, span in spans_output(doc, **values)['fields'].items():
        if span is not None:
            result['fields'][key] = span
    return result


def assert_saved(config, contract, prior, gateway):
    assert contract['constraints'] == prior['constraints']
    assert contract['revision'] == prior['revision']
    assert contract['binding']['documents'][:-1] == prior['binding']['documents']
    assert contract['input_hash'] == scope._hash(contract['binding'])
    assert contract['request_scope_hash'] == scope._hash(
        {k: v for k, v in contract.items() if k != 'request_scope_hash'})
    assert context(config, contract['binding']['command_id'])['request_scope'] == contract
    assert scope.current_contract(config) == contract
    assert scope.ensure_contract(config, gateway=gateway) == contract
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('field,text', [
    ('venue', 'Binance'), ('base', 'ETH'), ('quote', 'USDT'),
    ('timeframe', '1h'), ('window_days', '30 days'), ('all', None),
])
def test_requoted_original_fields_keep_raw_evidence_and_original_anchor(env, field, text):
    followup = 'Please finish. Context: Binance ETH/USDT, 1h candles, last 30 days.'
    prior = assistant_chain(env, followup)
    values = ({k: v['text'] for k, v in prior['normalized_evidence']['fields'].items() if v}
              if field == 'all' else {field: text})
    reported = []
    def choose(docs):
        result = repeat(prior, docs[-1], **values)
        # Reuse the existing unique exact-quote offset repair first.
        for key in values:
            result['fields'][key].update(start=0, end=1)
        reported.append(deepcopy(result))
        return result
    gateway = FakeGateway(choose)
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['evidence'] == reported[0]
    assert contract['extractions'][-1] == reported[0]
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert contract['constraints']['window_anchor'] == prior['constraints']['window_anchor']
    assert_saved(env, contract, prior, gateway)


def test_protocol_requotation_uses_verified_role_not_venue_alias(env):
    prior = assistant_chain(env, 'Please finish: Uniswap v3 ETH/USDC, 30天.',
                            initial_text=INITIAL, initial_choose=initial_output)
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], protocol='Uniswap v3'))
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert_saved(env, contract, prior, gateway)


def test_unbound_protocol_change_is_not_continuity(env):
    prior = assistant_chain(env, 'Please finish with Aave.',
                            initial_text=INITIAL, initial_choose=initial_output)
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], protocol='Aave'))
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == 1
    assert 'request_scope' not in context(env)
    assert scope.current_contract(env, command_id='child-create') == prior


def test_operational_exclusion_in_followup_does_not_require_scope_amendment(env):
    followup = 'Keep the original candidate and window. Host diagnosis: use the configured model route.'
    prior = assistant_chain(env, followup)

    def choose(docs):
        result = deepcopy(prior['evidence'])
        doc = docs[-1]
        start = doc['text'].index('Host diagnosis:')
        result['excluded_spans'] = [{'command_id': doc['command_id'], 'start': start,
            'end': len(doc['text']), 'text': doc['text'][start:], 'role': 'provider_route'}]
        return result

    gateway = FakeGateway(choose)
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['constraints'] == prior['constraints']
    assert contract['revision'] == prior['revision']
    assert contract['evidence']['amendments'] == []
    assert_saved(env, contract, prior, gateway)
    snapshot = scope._snapshot(env)
    invalid = deepcopy(contract['evidence'])
    invalid['excluded_spans'][0]['text'] = 'not admitted'
    with pytest.raises(scope.ScopeError):
        scope._canonical(invalid, snapshot, previous=prior)
    invalid = deepcopy(contract['evidence'])
    invalid['fields']['quote'] = dict(invalid['excluded_spans'][0])
    invalid['fields']['quote'].pop('role')
    with pytest.raises(scope.ScopeError):
        scope._canonical(invalid, snapshot, previous=prior)


def test_repeated_quotes_survive_multiple_saved_prefix_comparisons(env):
    prior = assistant_chain(env, 'Please finish: Binance ETH/USDT, last 30 days.')
    for index, cid in enumerate(('child-follow', 'child-next', 'child-last')):
        if index:
            with closing(connect(env.paths.db)) as con, con:
                con.execute('UPDATE agent_commands SET state=? WHERE command_id=?',
                            ('succeeded', prior['binding']['command_id']))
            admit(env, cid=cid, sid='child', text='Please finish: Binance ETH/USDT, last 30 days.',
                  created=1793000000 + index, position=index + 2, source='user_chat')
        gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], venue='Binance', base='ETH',
                                                  quote='USDT', window_days='30 days'))
        contract = scope.ensure_contract(env, gateway=gateway)
        assert contract['normalized_evidence'] == prior['normalized_evidence']
        assert_saved(env, contract, prior, gateway)
        prior = contract


def test_requotation_reaches_value_check_when_original_name_is_still_covered(env):
    def initial(docs):
        return spans_output(docs[0], venue='Aave', protocol='Aave', asset='USDC', window_days='30 days')
    prior = assistant_chain(env, 'Please finish: Aave.',
                            initial_text='Aave USDC strategy, last 30 days.', initial_choose=initial)
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], venue='Aave'))
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert_saved(env, contract, prior, gateway)


def test_repeated_original_mentions_use_authoritative_field_span(env):
    text = 'Notes: USDT. Binance ETH/USDT strategy, 1h candles, last 30 days.'
    def initial(docs):
        result = spans_output(docs[0], venue='Binance', base='ETH', quote='USDT',
                              timeframe='1h', window_days='30 days')
        start = docs[0]['text'].index('ETH/USDT') + len('ETH/')
        result['fields']['quote'].update(start=start, end=start + 4)
        return result
    prior = assistant_chain(env, 'Please finish: USDT.', initial_text=text, initial_choose=initial)
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], quote='USDT'))
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert_saved(env, contract, prior, gateway)


@pytest.mark.parametrize('base', ['BTC', 'ETH'])
def test_additional_resource_cannot_borrow_first_resource_field(env, base):
    def initial(docs):
        result = spans_output(docs[0], venue='Binance', base='ETH', quote='USDT',
                              timeframe='1h', window_days='30 days')
        other = spans_output(docs[0], base='BTC', quote='USDC')['fields']
        result['additional_resources'] = [{key: other[key] for key in scope.RESOURCE_FIELDS}]
        return result
    prior = assistant_chain(env, 'Please finish: ' + base + '/USDC.',
                            initial_text='Binance ETH/USDT and BTC/USDC, 1h candles, last 30 days.',
                            initial_choose=initial)
    def choose(docs):
        result = deepcopy(prior['evidence'])
        fields = spans_output(docs[-1], base=base, quote='USDC')['fields']
        for key in ('base', 'quote'):
            result['additional_resources'][0][key] = fields[key]
        return result
    gateway = FakeGateway(choose)
    if base == 'BTC':
        contract = scope.ensure_contract(env, gateway=gateway)
        assert contract['normalized_evidence'] == prior['normalized_evidence']
        assert_saved(env, contract, prior, gateway)
    else:
        with pytest.raises(scope.ScopeError):
            scope.ensure_contract(env, gateway=gateway)
        assert 'request_scope' not in context(env)
        assert len(gateway.calls) == 1


@pytest.mark.parametrize('field,text', [
    ('venue', 'OKX'), ('base', 'WETH'), ('quote', 'USDC'),
    ('window_days', '7 days'), ('timeframe', '60m'), ('venue', 'binance'),
    ('base', 'USDT'), ('asset', 'ETH'), ('protocol', 'Binance'),
])
def test_new_values_aliases_and_borrowed_field_roles_fail_closed(env, field, text):
    prior = assistant_chain(env, 'Please finish. Context: ' + text)
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], **{field: text}))
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    # Duration/timeframe are reported as uncovered evidence on the first
    # structured response; request_scope performs one bounded extraction
    # repair before rejecting the unbound follow-up value.
    assert len(gateway.calls) == (2 if field in {'window_days', 'timeframe'} else 1)
    assert 'request_scope' not in context(env)
    assert scope.current_contract(env, command_id='child-create') == prior


@pytest.mark.parametrize('quoted', ['WETH', 'ETH', 'WBTC'])
def test_continuation_keeps_latest_explicit_asset_amendment(env, quoted):
    original = assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial_output)
    prior = scope.ensure_contract(env, gateway=FakeGateway(amended_output))
    with closing(connect(env.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='child-follow'")
    admit(env, cid='child-next', sid='child', text='Please finish. Context: ' + quoted + '/USDC.',
          created=1793000000, position=3, source='user_chat')
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], base=quoted))
    if quoted != 'WETH':
        with pytest.raises(scope.ScopeError):
            scope.ensure_contract(env, gateway=gateway)
        assert 'request_scope' not in context(env, 'child-next')
        assert len(gateway.calls) == 1
    else:
        contract = scope.ensure_contract(env, gateway=gateway)
        assert contract['normalized_evidence'] == prior['normalized_evidence']
        assert contract['constraints']['base'] == 'WETH'
        assert contract['revision'] == 2
        assert_saved(env, contract, prior, gateway)
    assert scope.current_contract(env, command_id='child-follow') == prior
    assert scope.current_contract(env, command_id='child-create') == original


def test_explicit_bound_change_still_works_with_other_repeated_fields(env):
    text = 'Change quote asset to USDC. Keep Binance and the original interval.'
    prior = assistant_chain(env, text)
    def choose(docs):
        result = repeat(prior, docs[-1], venue='Binance', quote='USDC')
        result['amendments'] = [spans_output(docs[-1], asset='Change quote asset to USDC')['fields']['asset']]
        return result
    gateway = FakeGateway(choose)
    # A same-document field outside its explicit amendment is still rejected.
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    assert 'request_scope' not in context(env)
    def bound(docs):
        result = choose(docs)
        result['amendments'] = [spans_output(docs[-1], asset=text)['fields']['asset']]
        return result
    contract = scope.ensure_contract(env, gateway=FakeGateway(bound))
    assert contract['constraints']['quote'] == 'USDC'
    assert contract['revision'] == 2
    assert contract['constraints']['window_anchor'] == prior['constraints']['window_anchor']
    assert scope.current_contract(env) == contract


@pytest.mark.parametrize('damage', ['request', 'prior_digest'])
def test_repeated_span_cannot_bypass_cas_or_prior_digest(env, damage):
    prior = assistant_chain(env, 'Please finish: Binance ETH/USDT, last 30 days.')
    def mutate():
        with closing(connect(env.paths.db)) as con, con:
            if damage == 'request':
                con.execute("UPDATE agent_commands SET request_json='{}',revision=revision+1 "
                            "WHERE command_id='child-follow'")
            else:
                saved = context(env, 'child-create')
                saved['request_scope']['request_scope_hash'] = 'tampered'
                con.execute('UPDATE agent_commands SET context_json=?,revision=revision+1 WHERE command_id=?',
                            (json.dumps(saved), 'child-create'))
    gateway = FakeGateway(lambda docs: repeat(prior, docs[-1], window_days='30 days'), effect=mutate)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == 1
    assert 'request_scope' not in context(env)
