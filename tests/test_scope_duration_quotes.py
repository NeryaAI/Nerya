"""Offline duration quotations through the producer contract, with tmp-only state."""
from contextlib import closing
from copy import deepcopy
import json
import socket

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect, connect_readonly
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesResponse
from nerya.strategies import request_scope as scope

pytestmark = pytest.mark.smoke
TEXT = '帮我创建一个 Aave USDC 收益策略，回测最近30天，不启用交易。'
COMMAND = 'duration-quote-offline'
ANCHOR = 1792900000


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')

    def denied(*args, **kwargs):
        pytest.fail('NETWORK_OR_REAL_MODEL_FORBIDDEN')

    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(LLMGateway, 'call_messages', denied)
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    config.data['llm']['tiers'] = {'medium': {
        'provider': 'openai', 'model': 'fixture-model', 'allowed_tasks': ['agent.loop']}}
    config.data['agent']['native']['tier'] = 'medium'
    config.data.setdefault('runtime', {})['command_id'] = COMMAND
    return config


def admit(config, text):
    request = {'session_id': 'duration-session', '_auth_actor_id': 'owner',
               '_auth_scopes': ['execute:strategy'], 'model_provider': 'openai',
               'model_id': 'fixture-model', 'payload': {'text': text}}
    with closing(connect(config.paths.db)) as con, con:
        con.execute('INSERT INTO agent_commands(command_id,session_id,actor_id,kind,fingerprint,'
                    'request_json,context_json,state,turn_id,position,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (COMMAND, 'duration-session', 'owner', 'send', COMMAND, json.dumps(request),
                     '{}', 'running', 'duration-turn', 1, ANCHOR, ANCHOR))


class QuoteGateway:
    def __init__(self, quote):
        self.quote, self.outputs = quote, []

    def call_messages(self, **kwargs):
        doc, = json.loads(kwargs['messages'][0]['content'])
        fields = dict.fromkeys(scope.FIELDS)
        for key, text in [('protocol', 'Aave'), ('asset', 'USDC'), ('window_days', self.quote)]:
            start = doc['text'].index(text)
            fields[key] = {'command_id': doc['command_id'], 'start': start,
                           'end': start + len(text), 'text': text}
        output = {'status': 'resolved', 'fields': fields, 'additional_resources': [], 'amendments': []}
        self.outputs.append(deepcopy(output))
        return MessagesResponse(provider=kwargs['model_provider'], model=kwargs['model_id'],
                                stop_reason='tool_use', content=[{
                                    'type': 'tool_use', 'name': 'record_request_scope', 'input': output}])


@pytest.mark.parametrize('quote', ['30天', '最近30天'])
def test_actual_prompt_window_normalizes_and_preserves_exact_evidence(config, quote):
    admit(config, TEXT)
    gateway = QuoteGateway(quote)
    contract = scope.ensure_contract(config, gateway=gateway)
    constraints = contract['constraints']
    end = ANCHOR // 86400 * 86400
    assert constraints['window_days'] == 30
    assert constraints['window_anchor'] == ANCHOR
    assert constraints['start_utc'] == end - 30 * 86400
    assert constraints['end_utc'] == end
    assert constraints['protocol']['component_id'] == 'builtin:aave'
    assert constraints['asset'] == 'USDC'
    assert contract['evidence'] == gateway.outputs[0]
    assert contract['normalized_evidence'] == gateway.outputs[0]
    span = contract['evidence']['fields']['window_days']
    assert TEXT[span['start']:span['end']] == span['text'] == quote
    with closing(connect_readonly(config.paths.db)) as con:
        saved = json.loads(con.execute('SELECT context_json FROM agent_commands').fetchone()[0])
    assert saved['request_scope'] == contract
    assert scope.current_contract(config) == contract
    assert scope.ensure_contract(config, gateway=gateway) == contract
    assert len(gateway.outputs) == 1


@pytest.mark.parametrize('quote,days', [
    ('近30天', 30), ('过去 30 天', 30), ('最近 2 周', 14),
    ('last30days', 30), ('past 30 days', 30), ('LAST 30 DAYS', 30),
    ('last 2 weeks', 14), ('past 48 hours', 2), ('30 days', 30),
    ('1.5d', 1.5), (' 30天 ', 30),
])
def test_bounded_historical_quotes_derive_real_contract_constraints(config, quote, days):
    admit(config, TEXT.replace('最近30天', quote))
    gateway = QuoteGateway(quote)
    contract = scope.ensure_contract(config, gateway=gateway)
    assert contract['constraints']['window_days'] == days
    assert contract['constraints']['end_utc'] - contract['constraints']['start_utc'] == days * 86400
    assert contract['evidence'] == contract['normalized_evidence'] == gateway.outputs[0]


@pytest.mark.parametrize('quote', [
    '未来30天', '接下来30天', 'next 30 days', 'in 30 days', '30 days from now',
    '30天后', '30天前', 'last 30 days tomorrow', 'last past 30 days',
    '回测最近30天', 'about 30 days', '任意30天', 'blast30days',
    '最近30到60天', 'last 30-60 days', 'last 30 days or 60 days',
    '最近30天和7天', 'last 1 week 2 days', '30', 'days',
    '最近30个月', 'last 30 months', 'last 30M', 'last 30fortnights',
    'last 0 days', 'last -30 days', 'last 4000 days',
])
def test_invalid_duration_quotes_fail_closed_without_persisted_scope(config, quote):
    admit(config, TEXT.replace('最近30天', quote))
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config, gateway=QuoteGateway(quote))
    assert raised.value.reason == 'request_scope_unverified'
    assert raised.value.diagnostic_code == 'invalid_semantics'
    with closing(connect_readonly(config.paths.db)) as con:
        context = json.loads(con.execute('SELECT context_json FROM agent_commands').fetchone()[0])
    assert 'request_scope' not in context


@pytest.mark.parametrize('quote', ['最近30天', '近30天', '过去30天', 'last30days', 'past 30 days'])
def test_historical_prefix_is_not_a_candle_timeframe(quote):
    with pytest.raises(scope.ScopeError):
        scope._duration(quote)


@pytest.mark.parametrize('quote,seconds', [('30m', 1800), ('1h', 3600), ('1d', 86400), ('1w', 604800)])
def test_existing_bare_timeframe_units_remain_supported(quote, seconds):
    assert scope._duration(quote) == seconds


@pytest.mark.parametrize('quote', ['1M', '1month', '1fortnight', '0h', '-1d', '1h 30m'])
def test_invalid_bare_timeframes_remain_rejected(quote):
    with pytest.raises(scope.ScopeError):
        scope._duration(quote)
