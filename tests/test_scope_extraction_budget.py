"""Offline scope extraction through the real gateway and GLM wire adapter.

The token costs below are a deterministic fake, not a measurement of the
incident: its provider finish reason was not retained.
"""
from contextlib import closing
from copy import deepcopy
import json
import socket

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect, connect_readonly
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import OpenAIMessagesBackend
from nerya.strategies import request_scope as scope
from test_request_scope import admit, assistant_chain, spans_output

pytestmark = pytest.mark.smoke
INITIAL = '新建 Uniswap v3 ETH/USDC LP策略，回测最近30天，不启用交易。'
AMENDMENT = '本次仅在 LP 池资产绑定中使用 WETH 作为 ETH 的包装资产，保留原协议、原区间，不执行交易。'


def extracted(docs):
    result = spans_output(docs[0], protocol='Uniswap v3', base='ETH',
                          quote='USDC', window_days='30天')
    if docs[-1]['text'] == AMENDMENT:
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        result['amendments'] = [dict(command_id=docs[-1]['command_id'], start=0,
                                     end=len(AMENDMENT), text=AMENDMENT)]
    return result


class BudgetTransport:
    def __init__(self, *, finish=None, wrong_tool=False, effect=None):
        self.calls = []
        self.finish, self.wrong_tool, self.effect = finish, wrong_tool, effect

    def post_json(self, url, **kwargs):
        body = deepcopy(kwargs['body'])
        self.calls.append(body)
        docs = json.loads(body['messages'][-1]['content'])
        if self.effect:
            self.effect()
        # Simulate reasoning plus structured output exceeding the gateway's
        # 4096 default. No assertion about the incident's actual token split.
        required = 2048 + (0 if body.get('thinking') == {'type': 'disabled'} else 4096)
        finish = self.finish or ('length' if body['max_tokens'] < required else 'tool_calls')
        return 200, {
            'choices': [{'finish_reason': finish, 'message': {'content': None, 'tool_calls': [{
                'id': 'scope-output', 'type': 'function', 'function': {
                    'name': 'other_tool' if self.wrong_tool else 'record_request_scope',
                    'arguments': json.dumps(extracted(docs), ensure_ascii=False),
                },
            }]}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': min(required, body['max_tokens'])},
        }


@pytest.fixture
def offline(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')
    monkeypatch.setenv('NERYA_CONTEXT_FULL_LOG', '0')
    monkeypatch.setenv('NERYA_LLM_CONTEXT_LOG', '0')
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    config.data['llm']['tiers'] = {'medium': {
        'provider': 'zhipu', 'model': 'glm-5.2', 'reasoning_effort': 'high',
        'allowed_tasks': ['agent.loop'],
    }}
    config.data['agent']['native']['tier'] = 'medium'
    # Bypass credential lookup only. Routing, request defaults, model limits,
    # reasoning inheritance, wire rendering and response parsing stay real.
    monkeypatch.setattr(LLMGateway, '_resolve_provider_keys', lambda *args: ['fixture'])
    def install(transport):
        def backend(self, tier, **kwargs):
            route = kwargs['route_cfg']
            return OpenAIMessagesBackend(
                api_key='fixture', provider_name=route['provider'], model=route['model'],
                base_url='https://fixture.invalid/v1', transport=transport, max_attempts=1,
            )
        monkeypatch.setattr(LLMGateway, '_resolve_messages_backend', backend)
    return config, install


def chain(config):
    original = assistant_chain(config, '继续完成原任务。', initial_text=INITIAL,
                               initial_choose=extracted)
    # The original is already committed; all remaining prefixes use the
    # reported model, with a final explicit asset amendment at command 17.
    with closing(connect(config.paths.db)) as con, con:
        row = con.execute(
            "SELECT request_json FROM agent_commands WHERE command_id='child-follow'").fetchone()
        request = json.loads(row[0])
        request.update(model_provider='zhipu', model_id='glm-5.2')
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='child-follow'",
                    (json.dumps(request),))
    previous = 'child-follow'
    for index in range(3, 18):
        with closing(connect(config.paths.db)) as con, con:
            con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (previous,))
        previous = f'child-{index}'
        admit(config, cid=previous, sid='child', position=index,
              created=1792900600 + index, source='user_chat',
              text=AMENDMENT if index == 17 else '继续完成原任务。',
              extra={'model_provider': 'zhipu', 'model_id': 'glm-5.2'})
    return original


def test_structured_budget_completes_17_command_chain_without_clipping(offline):
    config, install = offline
    original = chain(config)
    before = deepcopy(config.data)
    snapshot = scope._snapshot(config, active=True)
    transport = BudgetTransport()
    install(transport)
    result = scope.ensure_contract(config)
    assert len(transport.calls) == 16  # cached original is never re-extracted
    for index, body in enumerate(transport.calls, 2):
        assert body['max_tokens'] == 16384
        assert body['thinking'] == {'type': 'enabled'}  # retain route policy
        assert body['model'] == 'glm-5.2' and body['tool_choice'] == 'required'
        assert len(body['tools']) == 1
        assert body['tools'][0]['function']['name'] == 'record_request_scope'
        assert body['tools'][0]['function']['parameters'] == scope.SCHEMA
        assert json.loads(body['messages'][-1]['content']) == snapshot['binding']['documents'][:index]
    expected = deepcopy(original['constraints'])
    expected['base'] = expected['resources'][0]['base'] = 'WETH'
    assert result['constraints'] == expected and result['revision'] == 2
    assert result['binding'] == snapshot['binding']
    assert result['input_hash'] == snapshot['input_hash']
    assert len(result['extractions']) == 17
    assert result['evidence'] == extracted(snapshot['binding']['documents'])
    assert result['normalized_evidence'] == result['evidence']
    assert scope.current_contract(config, command_id='child-create') == original
    assert scope.current_contract(config) == result
    assert scope.ensure_contract(config) == result and len(transport.calls) == 16
    assert config.data == before


def test_gateway_default_budget_reproduces_incomplete_response(offline, monkeypatch):
    config, install = offline
    original = chain(config)
    transport = BudgetTransport()
    install(transport)
    call = LLMGateway.call_messages
    def legacy(self, **kwargs):
        kwargs.pop('max_tokens', None)
        return call(self, **kwargs)
    monkeypatch.setattr(LLMGateway, 'call_messages', legacy)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config)
    assert raised.value.diagnostic_code == 'incomplete_response'
    assert len(transport.calls) == 1 and transport.calls[0]['max_tokens'] == 4096
    assert scope.current_contract(config, command_id='child-create') == original
    with closing(connect_readonly(config.paths.db)) as con:
        contexts = [json.loads(row[0]) for row in con.execute(
            "SELECT context_json FROM agent_commands WHERE session_id='child' AND command_id!='child-create'")]
    assert all('request_scope' not in context for context in contexts)


@pytest.mark.parametrize('finish,wrong_tool,diagnostic', [
    ('length', False, 'incomplete_response'),
    ('content_filter', False, 'incomplete_response'),
    ('tool_calls', True, 'invalid_tool_response'),
])
def test_larger_budget_still_rejects_invalid_response(offline, finish, wrong_tool, diagnostic):
    config, install = offline
    original = chain(config)
    transport = BudgetTransport(finish=finish, wrong_tool=wrong_tool)
    install(transport)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config)
    assert raised.value.diagnostic_code == diagnostic
    assert len(transport.calls) == 1
    assert scope.current_contract(config, command_id='child-create') == original


def test_budget_does_not_bypass_request_cas(offline):
    config, install = offline
    original = chain(config)
    def mutate():
        with closing(connect(config.paths.db)) as con, con:
            con.execute("UPDATE agent_commands SET request_json='{}',revision=revision+1 WHERE command_id='child-17'")
    transport = BudgetTransport(effect=mutate)
    install(transport)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config)
    assert len(transport.calls) == 1
    assert scope.current_contract(config, command_id='child-create') == original
