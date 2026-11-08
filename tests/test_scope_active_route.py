"""Current command routing with durable historical authority; entirely offline."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.db.sqlite import connect, connect_readonly
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesResponse
from nerya.strategies import request_scope as scope
from test_request_scope import admit, assistant_chain, output
from test_request_scope import env  # noqa: F401

pytestmark = pytest.mark.smoke
AGNES = {'model_provider': 'agnes', 'model_id': 'agnes-flash', 'model_tier': 'high'}
REAL_CALL_MESSAGES = LLMGateway.call_messages


def rows(config):
    with closing(connect_readonly(config.paths.db)) as con:
        return {row['command_id']: dict(row) for row in con.execute('SELECT * FROM agent_commands')}


@pytest.fixture
def chain(env):  # noqa: F811 - imported pytest fixture
    original = assistant_chain(env, 'Continue the original task; no trading.')
    with closing(connect(env.paths.db)) as con, con:
        request = json.loads(rows(env)['child-follow']['request_json'])
        request.update(model_provider='zhipu', model_id='glm-5.2', model_tier='medium')
        con.execute(
            "UPDATE agent_commands SET request_json=?,state='failed' WHERE command_id='child-follow'",
            (json.dumps(request),))
    env.data['llm']['tiers']['high'] = {
        'provider': 'agnes', 'model': 'agnes-flash', 'allowed_tasks': ['agent.loop'],
        'provider_key_env': 'FAKE_AGNES_KEY', 'base_url': 'https://agnes.invalid/v1'}
    env.data['llm']['tiers']['medium'].update(
        provider_key_env='FAKE_OLD_KEY', base_url='https://old.invalid/v1')
    admit(env, cid='active', sid='child', text='Finish the original task; no trading.',
          created=1792901200, position=3, source='user_chat', extra=AGNES)
    return env, original


def fake_gateway(monkeypatch, effect=None, wrong=None):
    calls = []

    def call(self, **kwargs):
        calls.append((deepcopy(kwargs), deepcopy(self.config.data)))
        if effect:
            effect()
        docs = json.loads(kwargs['messages'][0]['content'])
        return MessagesResponse(
            provider='zhipu' if wrong == 'provider' else kwargs['model_provider'],
            model='glm-5.2' if wrong == 'model' else kwargs['model_id'],
            stop_reason='tool_use', content=[{'type': 'tool_use',
                'name': 'record_request_scope', 'input': output(docs[0])}])

    monkeypatch.setattr(LLMGateway, 'call_messages', call)
    return calls


def test_active_route_recovers_old_prefix_without_rewriting_authority(chain, monkeypatch):
    config, original = chain
    before, data = rows(config), deepcopy(config.data)
    snapshot = scope._snapshot(config, active=True)
    calls = fake_gateway(monkeypatch)
    result = scope.ensure_contract(config)
    assert len(calls) == 2  # original is cached; failed prefix and active are not
    for index, (kwargs, owned_data) in enumerate(calls, 2):
        assert (kwargs['model_provider'], kwargs['model_id'], kwargs['tier']) == (
            'agnes', 'agnes-flash', 'high')
        assert kwargs['messages'] == [{'role': 'user',
            'content': scope._json(snapshot['binding']['documents'][:index])}]
        route = owned_data['llm']['tiers']['high']
        assert route['provider_key_env'] == 'FAKE_AGNES_KEY'
        assert route['base_url'] == 'https://agnes.invalid/v1'
        assert route['models'] == ['agnes-flash'] and 'routes' not in route
        assert route['provider_native_web_search'] is False
        assert kwargs['max_tokens'] == 16384
        assert kwargs['tool_choice'] == {'type': 'tool', 'name': 'record_request_scope'}
    assert result['binding'] == snapshot['binding']
    assert result['input_hash'] == snapshot['input_hash']
    assert result['constraints'] == original['constraints']
    assert result['normalized_evidence'] == original['normalized_evidence']
    after = rows(config)
    assert {cid: row['request_json'] for cid, row in after.items()} == {
        cid: row['request_json'] for cid, row in before.items()}
    assert after['child-create'] == before['child-create']
    assert after['child-follow']['state'] == 'failed'
    assert config.data == data


def test_scope_accepts_explanatory_text_with_exact_structured_tool_call(chain, monkeypatch):
    config, original = chain

    def call(self, **kwargs):
        docs = json.loads(kwargs['messages'][0]['content'])
        return MessagesResponse(
            provider=kwargs['model_provider'], model=kwargs['model_id'], stop_reason='tool_use',
            content=[
                {'type': 'thinking', 'thinking': 'Preparing the structured scope.'},
                {'type': 'text', 'text': 'I will record the admitted scope.'},
                {'type': 'tool_use', 'name': 'record_request_scope', 'input': output(docs[0])},
            ],
        )

    monkeypatch.setattr(LLMGateway, 'call_messages', call)
    assert scope.ensure_contract(config)['constraints'] == original['constraints']


def test_scope_keeps_current_command_reasoning_policy_after_tier_admission(chain, monkeypatch):
    config, original = chain
    config.data['llm']['tiers']['high']['reasoning_effort'] = 'none'
    with closing(connect(config.paths.db)) as con, con:
        request = json.loads(rows(config)['active']['request_json'])
        request.update(reasoning_effort='low', reasoning_summary='auto')
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='active'", (json.dumps(request),))
    calls = fake_gateway(monkeypatch)
    assert scope.ensure_contract(config)['constraints'] == original['constraints']
    assert calls
    for kwargs, _ in calls:
        assert kwargs['reasoning_effort'] == 'low'
        assert kwargs['reasoning_summary'] == 'auto'


def test_reasoning_override_reaches_real_gateway_request_without_relaxing_tier(chain, monkeypatch):
    config, original = chain
    config.data['llm']['tiers']['high']['reasoning_effort'] = 'none'
    with closing(connect(config.paths.db)) as con, con:
        request = json.loads(rows(config)['active']['request_json'])
        request['reasoning_effort'] = 'low'
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='active'", (json.dumps(request),))
    seen = []
    def backend(self, tier, **kwargs):
        def respond(request):
            seen.append((tier, request.reasoning_effort))
            docs = json.loads(request.messages[0]['content'])
            return MessagesResponse(provider=kwargs['route_cfg']['provider'],
                model=kwargs['route_cfg']['model'], stop_reason='tool_use',
                content=[{'type':'tool_use','name':'record_request_scope','input':output(docs[0])}])
        return respond
    monkeypatch.setattr(LLMGateway, '_resolve_provider_keys', lambda *args: ['fixture'])
    monkeypatch.setattr(LLMGateway, '_resolve_messages_backend', backend)
    monkeypatch.setattr(LLMGateway, 'call_messages', REAL_CALL_MESSAGES)
    assert scope.ensure_contract(config)['constraints'] == original['constraints']
    assert seen == [('high', 'low'), ('high', 'low')]


@pytest.mark.parametrize('selection,expected', [
    ({'model_tier': 'high'}, ('agnes', 'agnes-flash', 'high')),
    ({}, ('openai', 'fixture-model', 'medium')),
])
def test_partial_or_absent_current_selection_uses_config(chain, monkeypatch, selection, expected):
    config, _ = chain
    with closing(connect(config.paths.db)) as con, con:
        request = json.loads(rows(config)['active']['request_json'])
        for key in AGNES:
            request.pop(key, None)
        request.update(selection)
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='active'", (json.dumps(request),))
    calls = fake_gateway(monkeypatch)
    scope.ensure_contract(config)
    kwargs = calls[0][0]  # the uncached historical GLM prefix
    assert (kwargs['model_provider'], kwargs['model_id'], kwargs['tier']) == expected


@pytest.mark.parametrize('failure', ['provider', 'model', 'transport'])
def test_selected_route_mismatch_or_failure_never_falls_back(chain, monkeypatch, failure):
    config, original = chain

    def fail():
        if failure == 'transport':
            raise RuntimeError('offline failure')

    calls = fake_gateway(monkeypatch, effect=fail, wrong=failure)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config)
    assert raised.value.diagnostic_code == ('transport_error' if failure == 'transport' else 'model_mismatch')
    assert len(calls) == 1
    assert calls[0][0]['model_provider'] == 'agnes'
    assert scope.current_contract(config, command_id='child-create') == original
    assert 'request_scope' not in json.loads(rows(config)['child-follow']['context_json'])


def test_pinned_cross_tier_connection_retains_current_task_policy(chain, monkeypatch):
    config, original = chain
    config.data['llm']['tiers']['light'] = {
        'provider': 'agnes', 'model': 'agnes-flash',
        'allowed_tasks': ['extract_json'], 'allowed_classes': ['structured_extraction'],
        'base_url': 'https://agnes.invalid/v1', 'provider_key_env': 'FAKE_AGNES_KEY'}
    config.data['llm']['tiers']['high'] = {
        'provider': 'openai', 'model': 'fixture-model', 'allowed_tasks': ['agent.loop']}
    seen = []

    def call(self, **kwargs):
        # Exercise the actual second gateway policy gate, not a permissive fake.
        selected = self.tier_policy.resolve(kwargs['task'], requested_tier=kwargs['tier'],
                                            caller_allowed_tiers=None)
        seen.append(selected)
        docs = json.loads(kwargs['messages'][0]['content'])
        return MessagesResponse(provider=kwargs['model_provider'], model=kwargs['model_id'],
            stop_reason='tool_use', content=[{'type': 'tool_use', 'name': 'record_request_scope',
                                            'input': output(docs[0])}])

    monkeypatch.setattr(LLMGateway, 'call_messages', call)
    assert scope.ensure_contract(config)['constraints'] == original['constraints']
    assert seen == ['high', 'high']


@pytest.mark.parametrize('policy', [
    {'allowed_tasks': ['agent.loop']},
    {'allowed_classes': ['agent_loop']},
])
def test_cross_tier_scope_uses_real_gateway_with_original_task_authority(chain, monkeypatch, policy):
    config, original = chain
    config.data['llm']['tiers']['light'] = {
        'provider': 'agnes', 'model': 'agnes-flash',
        'allowed_tasks': ['extract_json'], 'allowed_classes': ['structured_extraction'],
        'base_url': 'https://agnes.invalid/v1', 'provider_key_env': 'FAKE_AGNES_KEY'}
    config.data['llm']['tiers']['high'] = {
        'provider': 'openai', 'model': 'fixture-model', **policy}
    before = deepcopy(config.data)
    seen = []

    def backend(self, tier, **kwargs):
        seen.append((tier, deepcopy(kwargs['route_cfg'])))

        def respond(request):
            docs = json.loads(request.messages[0]['content'])
            return MessagesResponse(provider=kwargs['route_cfg']['provider'],
                model=kwargs['route_cfg']['model'], stop_reason='tool_use',
                content=[{'type': 'tool_use', 'id': 'scope-result',
                          'name': 'record_request_scope', 'input': output(docs[0])}])

        return respond

    monkeypatch.setattr(LLMGateway, 'call_messages', REAL_CALL_MESSAGES)
    monkeypatch.setattr(LLMGateway, '_resolve_provider_keys', lambda *args: ['fixture-key'])
    monkeypatch.setattr(LLMGateway, '_resolve_messages_backend', backend)
    result = scope.ensure_contract(config)
    assert result['constraints'] == original['constraints']
    assert len(seen) == 2
    for tier, route in seen:
        assert tier == 'high'
        assert route['provider'] == 'agnes' and route['model'] == 'agnes-flash'
        assert route.get('allowed_tasks', []) == policy.get('allowed_tasks', [])
        assert route.get('allowed_classes', []) == policy.get('allowed_classes', [])
    assert config.data == before


def test_cross_tier_connection_cannot_admit_task_denied_by_all_tiers(chain, monkeypatch):
    from nerya.core.errors import LLMTaskNotAllowed

    config, _ = chain
    for route in config.data['llm']['tiers'].values():
        route['allowed_tasks'] = ['extract_json']
        route['allowed_classes'] = ['structured_extraction']
    before = rows(config)
    monkeypatch.setattr(LLMGateway, 'call_messages', REAL_CALL_MESSAGES)
    monkeypatch.setattr(LLMGateway, '_resolve_messages_backend',
                        lambda *args, **kwargs: pytest.fail('denied task reached backend'))
    with pytest.raises(LLMTaskNotAllowed):
        scope._extract(config, scope._snapshot(config, active=True))
    assert rows(config) == before


@pytest.mark.parametrize('target', ['active', 'child-follow', 'context'])
def test_route_override_preserves_request_cas_and_concurrent_context(chain, monkeypatch, target):
    config, original = chain

    def mutate():
        cid = 'child-follow' if target == 'context' else target
        row = rows(config)[cid]
        key = 'context_json' if target == 'context' else 'request_json'
        value = json.loads(row[key])
        if target == 'context':
            value['concurrent_note'] = 'preserve'
        else:
            value['model_id'] = 'concurrent-selection'
        with closing(connect(config.paths.db)) as con, con:
            con.execute(f'UPDATE agent_commands SET {key}=?,revision=revision+1 WHERE command_id=?',
                        (json.dumps(value), cid))

    calls = fake_gateway(monkeypatch, effect=mutate)
    if target == 'context':
        scope.ensure_contract(config)
        assert json.loads(rows(config)['child-follow']['context_json'])['concurrent_note'] == 'preserve'
    else:
        with pytest.raises(scope.ScopeError) as raised:
            scope.ensure_contract(config)
        assert raised.value.diagnostic_code == 'persistence_conflict'
        assert len(calls) == 1
        assert 'request_scope' not in json.loads(rows(config)['child-follow']['context_json'])
    assert scope.current_contract(config, command_id='child-create') == original
