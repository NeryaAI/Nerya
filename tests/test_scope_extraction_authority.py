"""Offline gateway-argument and authority-chain checks; no live-model claim."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.db.sqlite import connect, connect_readonly
from nerya.llm.messages import MessagesResponse
from nerya.strategies import request_scope as scope
import test_request_scope as helpers
from test_request_scope import admit, assistant_chain, spans_output
import test_request_scope as request_scope_fixtures

env = request_scope_fixtures.env
from test_scope_asset_alias_amendment import FOLLOWUP

pytestmark = pytest.mark.smoke
INITIAL = '帮我创建一个 Uniswap V3 ETH/USDC LP 策略，回测最近30天，不启用交易。'
ANCHOR = 1793183119.480184
CONTINUE = ('继续完成原任务。只读检查 start=2026-09-29T00:00:00Z '
            'end=2026-10-29T00:00:00Z，不改变原区间，不启用交易。')


def initial(docs):
    return spans_output(docs[0], protocol='Uniswap V3', base='ETH', quote='USDC',
                        window_days='最近30天')


def authority(kwargs):
    return json.loads(kwargs['system'].rsplit('\n', 1)[1])['previous_verified_scope']


class AuthorityGateway:
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def call_messages(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        docs = json.loads(kwargs['messages'][0]['content'])
        # Select prior authority from system, not a raw-document guess.
        result = (deepcopy(authority(kwargs)['normalized_evidence'])
                  if 'previous_verified_scope' in kwargs['system'] else initial(docs))
        if self.change:
            self.change(result, docs)
        return MessagesResponse(provider=kwargs['model_provider'], model=kwargs['model_id'],
            stop_reason='tool_use', content=[{'type': 'tool_use', 'name': 'record_request_scope',
                                            'input': result}])


@pytest.fixture
def original(env, monkeypatch):
    original_admit = helpers.admit

    def anchored(config, **kwargs):
        if kwargs.get('cid') == 'parent-create':
            kwargs['created'] = ANCHOR
        return original_admit(config, **kwargs)

    monkeypatch.setattr(helpers, 'admit', anchored)
    prior = assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial)
    assert prior['normalized_evidence']['fields']['window_days'] == {
        'command_id': 'child-create', 'start': 35, 'end': 40, 'text': '最近30天'}
    assert INITIAL[37:40] == '30天'
    assert prior['constraints']['start_utc'] == 1790553600
    assert prior['constraints']['end_utc'] == 1793145600
    return prior


def amend(result, docs):
    if docs[-1]['text'] == FOLLOWUP:
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        result['amendments'].append(dict(command_id=docs[-1]['command_id'], start=0,
                                         end=len(FOLLOWUP), text=FOLLOWUP))


def append(config, previous_id, index, text=CONTINUE):
    with closing(connect(config.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?",
                    (previous_id,))
    admit(config, cid=f'continue-{index}', sid='child', text=text, created=ANCHOR + index * 60,
          position=index, source='user_chat')
    return f'continue-{index}'


def assert_kwargs(call, docs, prior=None):
    assert call['messages'] == [{'role': 'user', 'content': scope._json(docs)}]
    assert call['task'] == 'agent.loop' and call['caller'] == 'strategy.request_scope'
    assert call['tier'] == 'medium'
    assert call['model_provider'] == 'openai' and call['model_id'] == 'fixture-model'
    assert call['cancel_token'] is None and call['max_tokens'] == 16384
    assert call['tool_choice'] == {'type': 'tool', 'name': 'record_request_scope'}
    assert call['tools'] == [{'name': 'record_request_scope',
        'description': 'Return extracted scope as structured data; no action is executed.',
        'input_schema': scope.SCHEMA}]
    baseline = scope.PROMPT + '\nSchema: ' + scope._json(scope.SCHEMA)
    if prior is None:
        assert call['system'] == baseline
    else:
        assert call['system'].startswith(baseline + '\n')
        assert authority(call) == {key: prior[key] for key in ('normalized_evidence', 'constraints')}
        assert 'Changes require an explicit admitted user amendment with exact source spans.' in call['system']
        assert 'Prior authority is not permission to act or to change scope.' in call['system']


def test_first_extraction_has_no_prior_context(env):
    admit(env, cid='fresh', sid='fresh', text=INITIAL, created=ANCHOR)
    snapshot = scope._snapshot(env, active=True)
    gateway = AuthorityGateway()
    scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == 1
    assert_kwargs(gateway.calls[0], snapshot['binding']['documents'])


def test_scope_validation_does_not_hold_writer_and_rejects_changed_context(env, monkeypatch):
    import sqlite3
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    prior = scope.ensure_contract(env, gateway=helpers.FakeGateway())
    snapshot = scope._snapshot(env, active=True)
    entered, release = Event(), Event()
    real = scope._saved_contract
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real(*args, **kwargs)
    monkeypatch.setattr(scope, '_saved_contract', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(scope._persist_prefix, env, snapshot, snapshot, prior, [prior], None)
        assert entered.wait(3)
        started = time.monotonic()
        try:
            with sqlite3.connect(env.paths.db, timeout=0.1) as con:
                con.execute("UPDATE agent_commands SET context_json=?,revision=revision+1 WHERE command_id=?",
                            (json.dumps({**snapshot['context'], 'stop_requested': True}),
                             prior['binding']['command_id']))
        finally:
            release.set()
        print('concurrent writer seconds:', time.monotonic() - started)
        with pytest.raises(scope.ScopeError):
            pending.result()


def test_verified_eth_then_weth_authority_roundtrips_25_raw_documents(env, original):
    gateway = AuthorityGateway(amend)
    changed = scope.ensure_contract(env, gateway=gateway)
    expected = deepcopy(original['constraints'])
    expected['base'] = expected['resources'][0]['base'] = 'WETH'
    assert changed['constraints'] == expected and changed['revision'] == 2
    assert_kwargs(gateway.calls[0], changed['binding']['documents'], original)
    previous_id = 'child-follow'
    for index in range(3, 26):
        previous_id = append(env, previous_id, index)
    snapshot = scope._snapshot(env, active=True)
    continued = scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == 24
    for index, call in enumerate(gateway.calls, 2):
        docs = snapshot['binding']['documents'][:index]
        prior = scope.current_contract(env, command_id=docs[-2]['command_id'])
        assert_kwargs(call, docs, prior)
        assert authority(call)['constraints']['base'] == ('ETH' if index == 2 else 'WETH')
    assert continued['constraints'] == expected
    assert continued['normalized_evidence'] == changed['normalized_evidence']
    assert continued['revision'] == 2 and len(continued['extractions']) == 25
    assert scope.current_contract(env) == continued
    assert scope.ensure_contract(env, gateway=gateway) == continued
    assert len(gateway.calls) == 24


def test_context_whitelist_uses_normalized_not_raw_evidence(env, original):
    snapshot = scope._snapshot(env, active=True)
    prior = deepcopy(original)
    prior['evidence'] = {'untrusted_raw_evidence': object()}
    prior['credentials'] = object()
    prior['context'] = {'private_context': object()}
    prior['binding'] = {'private_binding': object()}
    snapshot['context']['private_context'] = object()
    snapshot['request']['private_provider_body'] = object()
    gateway = AuthorityGateway(amend)
    scope._extract(env, snapshot, gateway=gateway, original=original, previous=prior)
    assert len(gateway.calls) == 1
    assert_kwargs(gateway.calls[0], snapshot['binding']['documents'], original)
    serialized = json.dumps(gateway.calls[0])
    for extra in ('untrusted_raw_evidence', 'credentials', 'private_context',
                  'private_binding', 'private_provider_body', 'request_scope_hash', 'extractions'):
        assert extra not in serialized


@pytest.mark.parametrize('damage', ['omission', 'unbound_change', 'invalid_quote', 'request_cas'])
def test_prior_context_does_not_fill_omissions_or_authorize_changes(env, original, damage):
    changed = scope.ensure_contract(env, gateway=AuthorityGateway(amend))
    current_id = append(env, 'child-follow', 3, CONTINUE + ' WBTC')

    def corrupt(result, docs):
        if damage == 'omission':
            result['fields']['window_days'] = None
        elif damage == 'unbound_change':
            result['fields']['base'] = spans_output(docs[-1], base='WBTC')['fields']['base']
        elif damage == 'invalid_quote':
            result['fields']['window_days']['text'] = '伪造30天'
        else:
            with closing(connect(env.paths.db)) as con, con:
                con.execute("UPDATE agent_commands SET request_json='{}',revision=revision+1 "
                            'WHERE command_id=?', (current_id,))

    gateway = AuthorityGateway(corrupt)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=gateway)
    if damage == 'omission':
        # Existing amendment-chain validation rejects the missing prior window.
        assert raised.value.diagnostic_code == 'invalid_semantics'
    assert len(gateway.calls) == 1
    assert authority(gateway.calls[0])['normalized_evidence'] == changed['normalized_evidence']
    assert scope.current_contract(env, command_id='child-follow') == changed
    assert scope.current_contract(env, command_id='child-create') == original
    with closing(connect_readonly(env.paths.db)) as con:
        context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                                         (current_id,)).fetchone()[0])
    assert 'request_scope' not in context
