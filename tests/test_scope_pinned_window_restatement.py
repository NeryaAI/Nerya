"""Host-proven paired UTC restatements retain the durable window authority."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.db.sqlite import connect, connect_readonly
from nerya.strategies import request_scope as scope
import test_request_scope as helpers
from test_request_scope import FakeGateway, admit, assistant_chain, spans_output
from test_request_scope import env  # noqa: F401
from test_scope_asset_alias_amendment import FOLLOWUP

pytestmark = pytest.mark.smoke
ANCHOR = 1793183119.480184
START = '2026-09-28T00:00:00Z'
END = '2026-10-28T00:00:00Z'


def contexts(config):
    with closing(connect_readonly(config.paths.db)) as con:
        return {row['command_id']: row['context_json'] for row in
                con.execute('SELECT command_id,context_json FROM agent_commands')}


def append(config, prior, text, index):
    with closing(connect(config.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?",
                    (prior['binding']['command_id'],))
    admit(config, cid=f'continue-{index}', sid='child', text=text, created=ANCHOR + index * 60,
          position=index + 1, source='user_chat')


@pytest.fixture
def pinned(env, monkeypatch):
    original_admit = helpers.admit

    def anchored(config, **kwargs):
        if kwargs.get('cid') == 'parent-create':
            kwargs['created'] = ANCHOR
        return original_admit(config, **kwargs)

    monkeypatch.setattr(helpers, 'admit', anchored)
    original = assistant_chain(env, FOLLOWUP,
        initial_text='Uniswap v3 ETH/USDC LP; last 30 days; BTC/USDT; no trading.',
        initial_choose=initial)

    def amend(docs):
        result = initial(docs)
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        result['amendments'] = [dict(command_id=docs[-1]['command_id'], start=0,
                                     end=len(FOLLOWUP), text=FOLLOWUP)]
        return result

    prior = scope.ensure_contract(env, gateway=FakeGateway(amend))
    for index in range(2, 9):
        append(env, prior, inspection() if index == 8 else '继续原任务。', index)
        prior = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: deepcopy(prior['evidence'])))
    assert len(prior['binding']['documents']) == 9
    assert prior['constraints']['start_utc'] == 1790553600
    assert prior['constraints']['end_utc'] == 1793145600
    assert prior['constraints']['window_days'] == 30
    assert prior['constraints']['window_anchor'] == ANCHOR
    assert prior['constraints']['base'] == 'WETH' and prior['revision'] == 2
    assert scope.current_contract(env, command_id='child-create') == original
    return env, prior


def initial(docs):
    result = spans_output(docs[0], protocol='Uniswap v3', base='ETH', quote='USDC', window_days='30 days')
    other = spans_output(docs[0], base='BTC', quote='USDT')['fields']
    result['additional_resources'] = [{key: other[key] for key in scope.RESOURCE_FIELDS}]
    return result


def restated(prior, doc, start=START, end=END):
    result = deepcopy(prior['normalized_evidence'])
    result['fields']['window_days'] = None
    dates = spans_output(doc, **{k: v for k, v in [('start_utc', start), ('end_utc', end)] if v})['fields']
    # Actual diagnostic selected end_utc first; object order grants no authority.
    result['fields'] = {'end_utc': dates['end_utc'],
                        **{k: v for k, v in result['fields'].items() if k != 'end_utc'}}
    result['fields']['start_utc'] = dates['start_utc']
    return result


def inspection(start=START, end=END):
    return f'只读检查历史数据 start={start},end={end}（首次任务保存的30天区间），不改变任务范围。'


def test_ensure_paired_restatement_at_document_9_keeps_authority(pinned):
    config, prior = pinned
    append(config, prior, inspection(), 9)
    reported = []

    def choose(docs):
        result = restated(prior, docs[-1])
        reported.append(deepcopy(result))
        return result

    gateway = FakeGateway(choose)
    contract = scope.ensure_contract(config, gateway=gateway)
    assert contract['constraints'] == prior['constraints']
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert contract['revision'] == 2
    assert contract['evidence'] == contract['extractions'][-1] == reported[0]
    assert contract['evidence']['fields']['end_utc']['command_id'] == 'continue-9'
    assert contract['normalized_evidence']['fields']['end_utc'] is None
    assert contract['constraints']['resources'] == prior['constraints']['resources']
    assert contract['binding']['documents'][:-1] == prior['binding']['documents']
    assert contract['input_hash'] == scope._hash(contract['binding'])
    assert contract['request_scope_hash'] == scope._hash(
        {k: v for k, v in contract.items() if k != 'request_scope_hash'})
    assert json.loads(contexts(config)['continue-9'])['request_scope'] == contract
    assert scope.current_contract(config) == contract
    assert scope.ensure_contract(config, gateway=gateway) == contract
    assert len(gateway.calls) == 1
    # Revalidate the normalized prefix again on another admitted continuation.
    append(config, contract, inspection(), 10)
    later = scope.ensure_contract(config, gateway=FakeGateway(lambda docs: restated(contract, docs[-1])))
    assert later['normalized_evidence'] == prior['normalized_evidence']
    assert later['constraints'] == prior['constraints']
    assert scope.current_contract(config) == later
    append(config, later, '继续原任务。', 11)
    inherited = scope.ensure_contract(config, gateway=FakeGateway(lambda docs: deepcopy(later['evidence'])))
    assert inherited['constraints'] == prior['constraints']
    assert inherited['normalized_evidence'] == prior['normalized_evidence']
    assert scope.current_contract(config) == inherited


@pytest.mark.parametrize('start,end', [
    ('2026-09-29T00:00:00Z', END), (START, '2026-10-29T00:00:00Z'),
    (None, END), (START, None),
    ('2026-09-28', '2026-10-28'),
    ('2026-09-28T00:00:00', '2026-10-28T00:00:00'),
    ('2026-09-28T08:00:00+08:00', '2026-10-28T08:00:00+08:00'),
    ('not-a-date', END),
    ('2026-09-28T00:00:00.0000001Z', END),
    # Same duration anchored to the later inspection is still a new window.
    ('2026-09-29T00:00:00Z', '2026-10-29T00:00:00Z'),
])
def test_changed_partial_or_non_utc_bounds_still_reject(pinned, start, end):
    config, prior = pinned
    append(config, prior, inspection(start, end), 9)
    before = contexts(config)
    gateway = FakeGateway(lambda docs: restated(prior, docs[-1], start, end))
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config, gateway=gateway)
    assert raised.value.diagnostic_code == 'invalid_semantics'
    assert raised.value.diagnostic_metadata == {
        'field': 'end_utc' if end else 'start_utc',
        'reason': 'unbound_followup_span', 'source_document_index': 9}
    assert contexts(config) == before and len(gateway.calls) == 1
    assert scope.current_contract(config, command_id=prior['binding']['command_id']) == prior


@pytest.mark.parametrize('damage,code', [
    ('ambiguous', 'ambiguous_scope'), ('schema', 'invalid_schema'),
    ('span', 'invalid_span'), ('split_documents', 'invalid_semantics'),
    ('timeframe', 'invalid_semantics'), ('window_days', 'invalid_semantics'),
    ('new_base', 'incomplete_scope_evidence'), ('new_resource', 'invalid_semantics'),
    ('amendment_outside_bounds', 'invalid_semantics'),
])
def test_paired_match_does_not_hide_other_invalid_scope(pinned, damage, code):
    config, prior = pinned
    append(config, prior, inspection() + ' 1h 7 days WBTC DAI confirm', 9)
    before = contexts(config)

    def choose(docs):
        result = restated(prior, docs[-1])
        fields = result['fields']
        if damage == 'ambiguous':
            result['status'] = 'ambiguous'
        elif damage == 'schema':
            fields['end_utc']['value'] = 1793145600
        elif damage == 'span':
            fields['end_utc']['text'] = '2026-10-28T00:00:01Z'
        elif damage == 'split_documents':
            fields['start_utc'] = spans_output(docs[-2], start_utc=START)['fields']['start_utc']
        elif damage in ('timeframe', 'window_days', 'new_base', 'new_resource'):
            key, text = {'timeframe': ('timeframe', '1h'), 'window_days': ('window_days', '7 days'),
                         'new_base': ('base', 'WBTC'), 'new_resource': ('quote', 'DAI')}[damage]
            target = result['additional_resources'][0] if damage == 'new_resource' else fields
            target[key] = spans_output(docs[-1], **{key: text})['fields'][key]
        else:
            result['amendments'].append(spans_output(docs[-1], asset='confirm')['fields']['asset'])
        return result

    gateway = FakeGateway(choose)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config, gateway=gateway)
    assert raised.value.diagnostic_code == code
    # Only an exact out-of-amendment diagnostic gets one structured-output
    # correction; repeated invalid evidence still cannot persist anything.
    attempts = 2 if damage == 'amendment_outside_bounds' else 1
    assert contexts(config) == before and len(gateway.calls) == attempts


@pytest.mark.parametrize('damage', ['anchor', 'bounds', 'prior_digest', 'request_cas'])
def test_paired_match_keeps_saved_chain_and_cas_verification(pinned, damage):
    config, prior = pinned
    append(config, prior, inspection(), 9)

    def mutate():
        with closing(connect(config.paths.db)) as con, con:
            if damage == 'request_cas':
                con.execute("UPDATE agent_commands SET request_json='{}',revision=revision+1 "
                            "WHERE command_id='continue-9'")
                return
            context = json.loads(contexts(config)['continue-8'])
            saved = context['request_scope']
            if damage == 'prior_digest':
                saved['request_scope_hash'] = 'tampered'
            else:
                saved['constraints']['window_anchor' if damage == 'anchor' else 'end_utc'] += 86400
                saved['request_scope_hash'] = scope._hash(
                    {k: v for k, v in saved.items() if k != 'request_scope_hash'})
            con.execute("UPDATE agent_commands SET context_json=?,revision=revision+1 "
                        "WHERE command_id='continue-8'", (json.dumps(context),))

    gateway = FakeGateway(lambda docs: restated(prior, docs[-1]), effect=mutate)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=gateway)
    assert len(gateway.calls) == 1
    assert 'request_scope' not in json.loads(contexts(config)['continue-9'])


def test_paired_match_restores_original_timeframe_as_well_as_window(env):
    prior = assistant_chain(env, '继续原任务。')
    prior = scope.ensure_contract(env, gateway=FakeGateway())
    start, end = (scope._iso(prior['constraints'][key]) for key in ('start_utc', 'end_utc'))
    append(env, prior, inspection(start, end), 2)

    def choose(docs):
        result = restated(prior, docs[-1], start, end)
        result['fields']['timeframe'] = None
        return result

    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints'] == prior['constraints']
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert contract['evidence']['fields']['timeframe'] is None
    assert scope.current_contract(env) == contract
