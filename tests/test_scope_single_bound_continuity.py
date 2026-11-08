"""Selected single bounds cannot prove continuity of an omitted boundary.

These are fail-closed diagnostics, not acceptance tests for a single-bound
normalizer. An unselected constraint can change even when the selected bound
exactly matches the durable window. Only the existing paired control succeeds.
"""
from copy import deepcopy
import json

import pytest

from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, assistant_chain, spans_output
from test_request_scope import env  # noqa: F401
from test_scope_pinned_window_restatement import (
    ANCHOR, END, START, append, contexts, inspection, pinned, restated,
)

pytestmark = pytest.mark.smoke


def test_gateway_prompt_requires_evidence_preserving_window_continuity(env):
    gateway = FakeGateway()
    scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == 1
    system = gateway.calls[0]['system']
    for instruction in (
        'Prefer preserving the original authoritative relative-window and timeframe spans',
        'select both start_utc and end_utc from the same document',
        'only where admitted evidence supports the same pinned window',
        'the host must prove equality',
        'Do not mix window_days with explicit date bounds',
        'Never omit a conflicting opposite bound or an explicit scope amendment',
    ):
        assert instruction in system
    assert system == scope.PROMPT + '\nSchema: ' + scope._json(scope.SCHEMA)
    assert json.loads(gateway.calls[0]['messages'][0]['content']) == scope.current_contract(env)['binding']['documents']


@pytest.mark.parametrize('case', [
    'end_only', 'start_only', 'no_bounds',
    'unselected_changed_start', 'unselected_change_start_prose',
    'unselected_changed_end', 'changed_selected_counterpart',
    'window_conflict', 'timeframe_conflict', 'invalid_span',
    'shifted_timezone', 'date_only', 'naive_date',
])
def test_incomplete_window_proof_rejects_without_writing(pinned, case):
    config, prior = pinned
    original = deepcopy(prior)
    start, end = START, END
    if case in ('unselected_changed_start', 'changed_selected_counterpart'):
        start = '2026-09-29T00:00:00Z'
    elif case == 'unselected_changed_end':
        end = '2026-10-29T00:00:00Z'
    elif case == 'shifted_timezone':
        end = '2026-10-28T08:00:00+08:00'
    elif case == 'date_only':
        end = '2026-10-28'
    elif case == 'naive_date':
        end = '2026-10-28T00:00:00'
    text = inspection(start, end)
    if case == 'unselected_change_start_prose':
        text = f'change start; end={END}'
    elif case == 'window_conflict':
        text += ' 7 days'
    elif case == 'timeframe_conflict':
        text += ' 1h'
    append(config, prior, text, 9)
    before = contexts(config)
    attempted = []

    def choose(docs):
        select_start = case in ('start_only', 'unselected_changed_end',
                               'changed_selected_counterpart')
        select_end = case not in ('start_only', 'unselected_changed_end', 'no_bounds')
        result = restated(prior, docs[-1],
                          start if select_start else None, end if select_end else None)
        if case in ('window_conflict', 'timeframe_conflict'):
            key, value = ('window_days', '7 days') if case == 'window_conflict' else ('timeframe', '1h')
            result['fields'][key] = spans_output(docs[-1], **{key: value})['fields'][key]
        elif case == 'invalid_span':
            result['fields']['end_utc']['text'] = '2026-10-28T00:00:01Z'
        attempted.append(deepcopy(result))
        return result

    gateway = FakeGateway(choose)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config, gateway=gateway)
    assert raised.value.diagnostic_code == ('invalid_span' if case == 'invalid_span' else 'invalid_semantics')
    if case not in ('invalid_span', 'no_bounds'):
        assert raised.value.diagnostic_metadata == {
            'field': 'start_utc' if case in ('start_only', 'unselected_changed_end') else 'end_utc',
            'reason': 'unbound_followup_span', 'source_document_index': 9,
        }
    assert len(gateway.calls) == 1
    # In particular, the changed counterpart/prose is genuinely unselected.
    if case in ('end_only', 'unselected_changed_start', 'unselected_change_start_prose'):
        assert attempted[0]['fields']['start_utc'] is None
        assert attempted[0]['fields']['end_utc']['text'] == END
    if case == 'unselected_changed_end':
        assert attempted[0]['fields']['end_utc'] is None
        assert attempted[0]['fields']['start_utc']['text'] == START
    assert contexts(config) == before
    assert 'request_scope' not in json.loads(before['continue-9'])
    saved = scope.current_contract(config, command_id=prior['binding']['command_id'])
    assert saved == prior == original
    for key in ('evidence', 'extractions', 'normalized_evidence', 'revision', 'constraints'):
        assert saved[key] == original[key]
    assert saved['revision'] == 2
    assert saved['constraints']['window_anchor'] == ANCHOR
    assert saved['constraints']['base'] == 'WETH'
    assert saved['constraints']['start_utc'] == 1790553600
    assert saved['constraints']['end_utc'] == 1793145600


def test_omitting_timeframe_and_window_without_pair_cannot_drop_authority(env):
    assistant_chain(env, '继续原任务。')
    prior = scope.ensure_contract(env, gateway=FakeGateway())
    original = deepcopy(prior)
    assert prior['normalized_evidence']['fields']['timeframe'] is not None
    assert prior['normalized_evidence']['fields']['window_days'] is not None
    append(env, prior, '继续原任务。', 2)
    before = contexts(env)

    def choose(docs):
        result = deepcopy(prior['normalized_evidence'])
        for key in ('timeframe', 'window_days', 'start_utc', 'end_utc'):
            result['fields'][key] = None
        return result

    gateway = FakeGateway(choose)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=gateway)
    assert raised.value.diagnostic_code == 'incomplete_scope_evidence'
    assert raised.value.diagnostic_metadata['reason'] == 'uncovered_duration'
    assert len(gateway.calls) == 1
    assert contexts(env) == before
    assert scope.current_contract(env, command_id=prior['binding']['command_id']) == original == prior


def test_complete_pair_control_preserves_latest_weth_and_raw_extraction(pinned):
    config, prior = pinned
    original = deepcopy(prior)
    append(config, prior, inspection(), 9)
    before = contexts(config)
    reported = []

    def choose(docs):
        result = restated(prior, docs[-1])
        reported.append(deepcopy(result))
        return result

    gateway = FakeGateway(choose)
    contract = scope.ensure_contract(config, gateway=gateway)
    assert contract['evidence'] == contract['extractions'][-1] == reported[0]
    assert contract['extractions'][:-1] == prior['extractions']
    assert contract['normalized_evidence'] == prior['normalized_evidence']
    assert contract['constraints'] == prior['constraints']
    assert contract['constraints']['base'] == 'WETH'
    assert contract['constraints']['window_anchor'] == ANCHOR
    assert contract['revision'] == prior['revision'] == 2
    assert prior == original
    assert scope.current_contract(config) == contract
    after = contexts(config)
    assert {cid: value for cid, value in after.items() if cid != 'continue-9'} == {
        cid: value for cid, value in before.items() if cid != 'continue-9'}
    assert json.loads(after['continue-9'])['request_scope'] == contract
    assert scope.ensure_contract(config, gateway=gateway) == contract
    assert len(gateway.calls) == 1
