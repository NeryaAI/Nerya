"""Scope producers with fake classification and tmp_path storage only."""
from contextlib import closing
from copy import deepcopy
import json
import socket

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect, connect_readonly
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesResponse
from nerya.strategies import request_scope as scope
from nerya.tools.native import strategy_runtime
from nerya.tools.types import ToolCall
from nerya.tools.types import ToolResult
from nerya.tools.result_contracts import tool_json_data
from nerya.strategies.tool_verification import attach_strategy_checks, source_binding, delivery_source_versions
import test_assistant_receipts as assistant_receipts_fixtures
from test_assistant_receipts import request as parent_request, create as create_task, followup as followup_task

assistant_env = assistant_receipts_fixtures.env

pytestmark = pytest.mark.smoke
TEXT = 'Binance ETH/USDT strategy, 1h candles, last 30 days; no trading.'


def output(doc, *, venue='Binance', base='ETH', quote='USDT', timeframe='1h', window='30 days'):
    fields = dict.fromkeys(scope.FIELDS)
    for key, value in [('venue', venue), ('base', base), ('quote', quote), ('timeframe', timeframe), ('window_days', window)]:
        if value is not None:
            start = doc['text'].index(value)
            fields[key] = {'command_id': doc['command_id'], 'start': start, 'end': start + len(value), 'text': value}
    return {'status': 'resolved', 'fields': fields, 'additional_resources': [], 'amendments': []}


class FakeGateway:
    def __init__(self, choose=None, effect=None):
        self.calls, self.choose, self.effect = [], choose, effect

    def call_messages(self, **kwargs):
        self.calls.append(kwargs)
        docs = json.loads(kwargs['messages'][0]['content'])
        if self.effect:
            self.effect()
        value = self.choose(docs) if self.choose else output(docs[0])
        return MessagesResponse(provider=kwargs['model_provider'], model=kwargs['model_id'],
                                content=[{'type': 'text', 'text': json.dumps(value)}])


def admit(config, *, cid='scope-1', sid='scope-session', text=TEXT, created=1792900000,
          state='running', position=1, source=None, extra=None):
    request = {'session_id': sid, '_auth_actor_id': 'owner', '_auth_scopes': ['execute:strategy'],
               'model_provider': 'openai', 'model_id': 'fixture-model', 'payload': {'text': text}}
    if source:
        request['source'] = source
    request.update(extra or {})
    with closing(connect(config.paths.db)) as con, con:
        con.execute('INSERT INTO agent_commands(command_id,session_id,actor_id,kind,fingerprint,request_json,context_json,'
                    'state,turn_id,position,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (cid, sid, 'owner', 'send', cid, json.dumps(request), '{}', state, 'turn-' + cid, position, created, created))
    config.data.setdefault('runtime', {}).update(command_id=cid, command_session_id=sid,
        command_turn_id='turn-' + cid, financial_actor_id='owner')
    return request


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')
    def denied(*args, **kwargs):
        pytest.fail('NETWORK_OR_REAL_MODEL_FORBIDDEN')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(LLMGateway, 'call_messages', denied)
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model', 'allowed_tasks': ['agent.loop']}}
    config.data['agent']['native']['tier'] = 'medium'
    admit(config)
    return config


@pytest.fixture
def fake_execution(monkeypatch):
    # Isolate only the unrelated OS loader. Real package schema/AST still run.
    monkeypatch.setattr('nerya.strategies.candidate_validation.run_candidate_validation',
        lambda *args, **kwargs: {'smoke_issues': [], 'behavioral': {'status': 'passed', 'exit_code': 0,
            'summary': {'passed': 1}, 'failures': [], 'runner': 'offline-execution-fixture'}})


def manifest(market='BINANCE:ETHUSDT', days=30):
    return {'markets': [market], 'timeframe': '1h', 'backtest': {'window_days': days}}


def install_fake(monkeypatch, fake):
    monkeypatch.setattr(LLMGateway, 'call_messages', lambda self, **kwargs: fake.call_messages(**kwargs))


def draft(config, market='BINANCE:ETHUSDT'):
    return strategy_runtime.strategy_draft_proposal_handler(ToolCall(name='strategy_draft_proposal', arguments={
        'strategy_id': 'scope_fixture', 'markets': [market], 'accounts': ['paper_main'], 'create_tuning': False,
        'files': {'strategy.yml': yaml_io.dumps(manifest(market)),
                  'main.py': 'def run(ctx): return ctx.result.hold(reason="fixture")\n'},
    }), config=config)


def test_exact_scope_derives_spans_persists_reuses_and_pins_window(env):
    gateway = FakeGateway()
    original = deepcopy(env.data)
    contract = scope.ensure_contract(env, gateway=gateway)
    assert contract['constraints']['venue'] == 'binance'
    assert contract['constraints']['base'] == 'ETH' and contract['constraints']['quote'] == 'USDT'
    cutoff = 1792900000 // 3600 * 3600
    assert contract['constraints']['end_utc'] == cutoff
    assert contract['constraints']['start_utc'] == cutoff - 30 * 86400
    assert scope.ensure_contract(env, gateway=gateway) == contract and len(gateway.calls) == 1
    assert scope.compare(contract, manifest()) == {'start_utc': scope._iso(cutoff - 30 * 86400), 'end_utc': scope._iso(cutoff)}
    assert env.data == original
    assert gateway.calls[0]['tools'][0]['name'] == 'record_request_scope'
    assert gateway.calls[0]['tools'][0]['input_schema'] == scope.SCHEMA
    assert gateway.calls[0]['tool_choice'] == {'type': 'tool', 'name': 'record_request_scope'}
    assert gateway.calls[0]['caller'] == 'strategy.request_scope'


def test_plain_binance_ema_prompt_keeps_explicit_resource_without_inventing_time(env):
    text = '创建一个 Binance BTC/USDT EMA 趋势策略，做 paper 回测并生成报告，不交易。'
    admit(env, cid='plain-binance-ema', sid='plain-binance-ema', text=text)
    fake = FakeGateway(lambda docs: spans_output(docs[0], venue='Binance', base='BTC', quote='USDT'))
    result = scope.ensure_contract(env, gateway=fake)
    assert result['constraints']['resources'] == [{'venue': 'binance', 'base': 'BTC', 'quote': 'USDT'}]
    assert 'timeframe' not in result['constraints']
    assert 'window_days' not in result['constraints']


@pytest.mark.parametrize('market,days,field', [('OKX:ETHUSDT', 30, 'venue'), ('BINANCE:ETHUSDC', 30, 'quote'),
    ('BINANCE:BTCUSDT', 30, 'base'), ('BINANCE:ETHUSDT', 7, 'window_days')])
def test_structured_scope_mismatch_has_exact_field_diagnostic(env, market, days, field):
    contract = scope.ensure_contract(env, gateway=FakeGateway())
    with pytest.raises(scope.ScopeError) as raised:
        scope.compare(contract, manifest(market, days))
    assert raised.value.reason == 'request_scope_mismatch'
    assert field in {d['field'] for d in raised.value.differences}


@pytest.mark.parametrize('attack', ['span', 'invented_value', 'unknown', 'ambiguous', 'omitted_quote', 'foreign_document'])
def test_model_cannot_self_declare_unanchored_scope(env, attack):
    def choose(docs):
        value = output(docs[0])
        if attack == 'span': value['fields']['venue']['text'] = 'not an exact source quote'
        if attack == 'invented_value': value['fields']['quote']['text'] = 'USDC'
        if attack == 'unknown': value['fields']['venue']['value'] = 'binance'
        if attack == 'ambiguous': value['status'] = 'ambiguous'
        if attack == 'omitted_quote': value['fields']['quote'] = None
        if attack == 'foreign_document': value['fields']['venue']['command_id'] = 'foreign'
        return value
    with pytest.raises(scope.ScopeError, match='request_scope_unverified'):
        scope.ensure_contract(env, gateway=FakeGateway(choose))
    with closing(connect_readonly(env.paths.db)) as con:
        assert 'request_scope' not in json.loads(con.execute('SELECT context_json FROM agent_commands').fetchone()[0])


def test_request_change_during_model_call_rejects_cas(env):
    def mutate():
        with closing(connect(env.paths.db)) as con, con:
            con.execute("UPDATE agent_commands SET request_json='{}',revision=revision+1")
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(effect=mutate))


def test_standalone_no_command_does_not_require_scope_or_model(env):
    env.data['runtime'].pop('command_id')
    assert scope.ensure_contract(env) is None
    assert scope.compare(None, {}) == {}


def test_explicit_perpetual_uses_declared_family_without_spot_or_legacy_aliasing(env):
    from nerya.connectors.provider_spec import get_registry

    text = '创建一个 Hyperliquid BTC/USDC 永续趋势策略，回测最近7天，不启用交易。'
    admit(env, cid='perpetual-request', sid='perpetual-session', text=text)
    def choose(docs):
        return spans_output(docs[0], venue='Hyperliquid', base='BTC', quote='USDC',
                            instrument='永续', window_days='7天')
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['evidence']['fields']['instrument']['text'] == '永续'
    assert contract['constraints']['resources'][0]['instrument'] == 'perpetual'
    assert scope.current_contract(env) == contract
    candidate = {'markets': ['HYPERLIQUID_PERPETUAL:BTC/USDC:USDC'],
                 'timeframe': '1h', 'backtest': {'window_days': 7}}
    assert scope.compare(contract, candidate)
    assert scope.compare(contract, {**candidate, 'markets': ['HYPERLIQUID:BTC/USDC:USDC']})
    for market in ('HYPERLIQUID:BTC/USDC', 'BINANCE:BTC/USDC',
                   'BINANCE_PERPETUAL:BTC/USDC:USDC'):
        with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
            scope.compare(contract, {**candidate, 'markets': [market]})
    registry = get_registry()
    assert registry.find('hyperliquid_perpetual').to_info()['route_instrument'] == 'perpetual'
    assert registry.find('binance').route_instrument == 'spot'
    # Old persisted contracts retain exact provider IDs, even when their raw
    # text mentioned perpetual before the optional field existed.
    extraction = choose(scope._snapshot(env)['binding']['documents'])
    extraction['fields'].pop('instrument')
    legacy = scope._chain([extraction], scope._snapshot(env))
    assert 'instrument' not in legacy['constraints']
    assert scope.compare(legacy, {**candidate, 'markets': ['HYPERLIQUID:BTC/USDC']})
    with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
        scope.compare(legacy, candidate)
    binance = deepcopy(contract)
    binance['constraints']['resources'][0]['venue'] = 'binance'
    assert scope.compare(binance, {**candidate, 'markets': ['BINANCE_PERPETUAL:BTC/USDC:USDC']})
    with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
        scope.compare(binance, {**candidate, 'markets': ['BINANCE:BTC/USDC']})
    binance['constraints']['resources'][0]['instrument'] = 'spot'
    assert scope.compare(binance, {**candidate, 'markets': ['BINANCE:BTC/USDC']})
    with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
        scope.compare(binance, {**candidate, 'markets': ['BINANCE_PERPETUAL:BTC/USDC:USDC']})


def test_wrong_draft_blocked_before_candidate_creation(env, monkeypatch):
    fake = FakeGateway()
    install_fake(monkeypatch, fake)
    result = draft(env, 'BINANCE:ETHUSDC')
    assert result.is_error and result.content[0].data['reason'] == 'request_scope_mismatch'
    assert not env.paths.proposals.exists()


def test_exact_submit_passes_and_replay_override_rejected_before_runner(env, monkeypatch, fake_execution):
    fake = FakeGateway()
    install_fake(monkeypatch, fake)
    result = draft(env)
    assert not result.is_error, result.text()
    pid = result.content[0].data['proposal_id']
    submitted = strategy_runtime.strategy_submit_proposal_handler(ToolCall(name='strategy_submit_proposal', arguments={'proposal_id': pid}), config=env)
    assert not submitted.is_error, submitted.text()
    data = submitted.content[0].data
    assert data['state'] == 'pending_review' and data['request_scope']['status'] == 'passed'
    assert data['request_scope']['request_scope_hash'] == scope.current_contract(env)['request_scope_hash']
    def denied(*args, **kwargs):
        pytest.fail('runner must not start on scope mismatch')
    monkeypatch.setattr('nerya.skills.builtin.backtest.scripts.backtest_run.run_strategy_backtest', denied)
    result = strategy_runtime.strategy_backtest_handler(ToolCall(name='strategy_backtest', arguments={
        'proposal_id': pid, 'settings': {'window_days': 7}, 'override_candidate_backtest_defaults': True,
    }), config=env)
    assert result.is_error and result.content[0].data['reason'] == 'request_scope_mismatch'
    assert len(fake.calls) == 1


def assistant_chain(config, followup, *, initial_text=TEXT, initial_choose=None):
    from nerya.agent.assistant_work import AssistantWork
    from nerya.agent.assistant_intent import assistant_request_digest
    AssistantWork(config.paths)
    def parent(cid, text, disposition):
        req = admit(config, cid=cid, sid='parent', text=text, state='succeeded')
        binding = {'command_id': cid, 'session_id': 'parent', 'turn_id': 'turn-' + cid,
                   'actor_id': 'owner', 'input_digest': assistant_request_digest(req)}
        contract = {'status': 'ready', 'binding': binding, 'disposition': disposition, 'target_task_id': 'work-scope'}
        with closing(connect(config.paths.db)) as con, con:
            con.execute('UPDATE agent_commands SET context_json=? WHERE command_id=?',
                        (json.dumps({'assistant_request': contract}), cid))
        return binding
    binding = parent('parent-create', initial_text, 'task_create')
    request = admit(config, cid='child-create', sid='child', text=initial_text, state='succeeded', source='assistant_task',
                    extra={'assistant_request_binding': binding})
    with closing(connect(config.paths.db)) as con, con:
        con.execute('INSERT INTO assistant_work VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    ('work-scope', 'main', 'parent', 'child', 'Scope task', 'general', 'fp', 'child-create',
                     json.dumps(request), 'owner', 1792900000))
        con.execute("UPDATE agent_commands SET state='running' WHERE command_id='child-create'")
    original = scope.ensure_contract(config, gateway=FakeGateway(initial_choose))
    with closing(connect(config.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='child-create'")
    binding = parent('parent-follow', followup, 'task_follow_up')
    admit(config, cid='child-follow', sid='child', text=followup, created=1792900600, position=2,
          source='assistant_task_message', extra={'assistant_request_binding': binding})
    return original


def test_followup_inherits_original_request_and_window_without_topic_rewrite(env):
    original = assistant_chain(env, 'Continue and finish the task; do not enable trading.')
    fake = FakeGateway()
    follow = scope.ensure_contract(env, gateway=fake)
    assert follow['constraints'] == original['constraints'] and follow['revision'] == 1
    assert follow['binding']['original_command_id'] == 'child-create'
    assert len(json.loads(fake.calls[0]['messages'][0]['content'])) == 2
    assert scope.current_contract(env) == follow


def test_direct_assistant_task_resume_reuses_verified_scope_without_model(env):
    original = assistant_chain(env, '继续完成原任务，复用已有快照和元数据，不重新采样，不交易。')
    with closing(connect(env.paths.db)) as con, con:
        request = json.loads(con.execute(
            "SELECT request_json FROM agent_commands WHERE command_id='child-follow'").fetchone()[0])
        request.pop('assistant_request_binding')
        request['source'] = 'user_chat'
        request['conversation_kind'] = 'task'
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='child-follow'",
                    (json.dumps(request),))
    def no_reclassification(docs):
        pytest.fail('Scope-free resume must not reclassify verified request history')
    resumed = scope.ensure_contract(env, gateway=FakeGateway(no_reclassification))
    assert resumed['constraints'] == original['constraints']
    assert resumed['normalized_evidence'] == original['normalized_evidence']
    assert resumed['binding']['command_id'] == 'child-follow'
    assert scope.current_contract(env) == resumed


@pytest.mark.parametrize('multiple_answers', [False, True])
def test_question_answer_completes_unspecified_resource_without_persisting_empty_prefix(env, monkeypatch, multiple_answers):
    from types import SimpleNamespace
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.interactions import respond
    from nerya.db.repositories import AgentSessionRepository
    from nerya.tools.native.user_interaction import interaction_handler

    sid, cid = 'unspecified-market', 'unspecified-original'
    text = '创建一个脚本驱动 Agent 趋势策略，回测最近7天，不启用交易。'
    answer = 'BINANCE:BTC/USDT\n1h' if multiple_answers else 'BINANCE:BTC/USDT spot'
    original = admit(env, cid=cid, sid=sid, text=text, source='user_chat',
                     extra={'conversation_kind': 'task'})
    runtime = CommandRuntime(env, lambda *_: {'stopped_reason': 'end_turn'}, epoch='scope-clarification')
    monkeypatch.setattr(runtime, 'kick', lambda _: None)
    with closing(connect(env.paths.db)) as con, con:
        AgentSessionRepository(con).upsert_session(session_id=sid, source='user_chat', title=text)
    questions = [{'id': 'strategy_resource', 'question': '请提供策略市场和交易对。',
                  'options': ['BINANCE:BTC/USDT']}]
    if multiple_answers:
        questions.append({'id': 'timeframe', 'question': 'K线周期？', 'options': ['1h']})
    result = interaction_handler(ToolCall(id='market-question', name='request_user_input',
        turn_id='turn-' + cid, arguments={'title': '策略市场', 'questions': questions}),
        deps=SimpleNamespace(config=env, active_session_id=sid), kind='question')
    assert not result.is_error
    question = result.content[0].data
    with runtime.store.transaction() as con:
        runtime.store._queue(con, sid)
        AgentSessionRepository(con).save_turn_checkpoint(session_id=sid, turn_id='turn-' + cid,
                                                        checkpoint={'resumable': True})
        con.execute("UPDATE agent_commands SET state='awaiting_input' WHERE command_id=?", (cid,))
    answers = ({'strategy_resource': {'selected': ['BINANCE:BTC/USDT'], 'text': ''},
                'timeframe': {'selected': ['1h'], 'text': ''}} if multiple_answers else
               {'strategy_resource': {'selected': [], 'text': answer}})
    continuation = respond(runtime, {'session_id': sid, 'interaction_id': question['interaction_id'],
        'response_id': 'market-answer', 'expected_revision': question['revision'],
        '_auth_actor_id': 'owner', '_auth_scopes': ['execute:strategy'], 'action': 'answer',
        'answers': answers})['command']
    resumed = runtime.store.claim(sid, 'clarification-worker')
    assert resumed['command_id'] == continuation['command_id']
    env.data['runtime'].update(command_id=resumed['command_id'], command_turn_id=resumed['turn_id'])
    follow = '已提供新的有界回放授权，核对后继续原任务，不启用交易。'
    if multiple_answers:
        with runtime.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?",
                        (resumed['command_id'],))
        admit(env, cid='authorization-followup', sid=sid, text=follow,
              created=resumed['created_at'] + 1, position=3, source='user_chat',
              extra={'conversation_kind': 'task'})
        env.data['runtime'].pop('command_turn_id', None)
        snapshot = scope._snapshot(env)
        assert snapshot['scope_answers'] == [resumed['command_id']]
        assert [d['text'] for d in snapshot['binding']['documents']] == [text, answer, follow]

    def choose(docs):
        extracted = spans_output(docs[0], window_days='最近7天')
        if len(docs) == 1:
            extracted['status'] = 'ambiguous'
            return extracted
        if len(docs) > 1:
            assert docs[1]['text'] == answer
            resource = spans_output(docs[1], venue='BINANCE', base='BTC', quote='USDT',
                                    **({'timeframe': '1h'} if multiple_answers else {'instrument': 'spot'}))
            for key in scope.RESOURCE_FIELDS:
                extracted['fields'][key] = resource['fields'][key]
            if multiple_answers:
                extracted['fields']['timeframe'] = resource['fields']['timeframe']
            if not multiple_answers:
                extracted['amendments'] = [{'command_id': docs[1]['command_id'],
                    'start': 0, 'end': len(answer), 'text': answer}]
        return extracted

    with pytest.raises(scope.ScopeError) as unresolved:
        scope.ensure_contract(env, gateway=FakeGateway(lambda docs: {
            **spans_output(docs[0], window_days='最近7天'), 'status': 'ambiguous'}))
    assert unresolved.value.diagnostic_code == 'ambiguous_scope'
    gateway = FakeGateway(choose)
    contract = scope.ensure_contract(env, gateway=gateway)
    assert len(gateway.calls) == (3 if multiple_answers else 2)
    assert answer in json.loads(gateway.calls[-1]['messages'][0]['content'])[1]['text']
    assert contract['constraints']['resources'] == [
        {'venue': 'binance', 'base': 'BTC', 'quote': 'USDT',
         **({} if multiple_answers else {'instrument': 'spot'})}]
    assert contract['constraints']['window_days'] == 7
    assert contract['constraints']['window_anchor'] == 1792900000
    step = 3600 if multiple_answers else 86400
    assert contract['constraints']['end_utc'] == 1792900000 // step * step
    assert contract['constraints']['start_utc'] == contract['constraints']['end_utc'] - 7 * 86400
    assert [d['text'] for d in contract['binding']['documents']] == [text, answer] + ([follow] if multiple_answers else [])
    assert contract['binding']['original_command_id'] == cid
    assert scope.current_contract(env) == contract
    if multiple_answers:
        changed = choose(contract['binding']['documents'])
        changed['fields']['base'] = changed['fields']['quote']
        with pytest.raises(scope.ScopeError) as rejected:
            scope._canonical(changed, scope._snapshot(env), previous=contract)
        assert rejected.value.diagnostic_metadata['reason'] == 'unbound_followup_span'
    cached = FakeGateway(lambda docs: pytest.fail('Saved joint admission must not re-extract empty prefixes'))
    assert scope.ensure_contract(env, gateway=cached) == contract
    assert not cached.calls
    with closing(connect_readonly(env.paths.db)) as con:
        row = con.execute('SELECT request_json,context_json FROM agent_commands WHERE command_id=?',
                          (cid,)).fetchone()
        assert json.loads(row['request_json']) == original  # Includes no-trading restriction unchanged.
        assert 'request_scope' not in json.loads(row['context_json'])


def test_question_answer_can_supply_timeframe_and_window_in_one_free_text_answer(env, monkeypatch):
    initial = '创建一个 Solana Jupiter SOL/USDC 的趋势策略，做 paper 回测并生成报告，不交易。'
    answer = '1h，最近7天'
    documents = [
        {'command_id': 'jupiter-original', 'text': initial, 'created_at': 1793834879},
        {'command_id': 'response-interaction', 'text': answer, 'created_at': 1793834966},
    ]
    initial_fields = spans_output(documents[0], venue='Jupiter', chain='Solana',
                                  base='SOL', quote='USDC')['fields']
    answer_fields = spans_output(documents[1], timeframe='1h', window_days='最近7天')['fields']
    fields = {**initial_fields, 'timeframe': answer_fields['timeframe'],
              'window_days': answer_fields['window_days']}
    output = {'status': 'resolved', 'fields': fields, 'additional_resources': [], 'amendments': []}
    binding = {'command_id': 'response-interaction', 'session_id': 'jupiter-session',
               'actor_id': 'owner', 'original_command_id': 'jupiter-original',
               'documents': documents}
    snapshot = {'binding': binding, 'input_hash': scope._hash(binding),
                'scope_answers': ['response-interaction']}
    contract = scope._canonical(output, snapshot)
    assert contract['constraints']['resources'] == [{'venue': 'jupiter', 'chain': 'solana', 'base': 'SOL', 'quote': 'USDC'}]
    assert contract['constraints']['timeframe'] == 3600
    assert contract['constraints']['window_days'] == 7


@pytest.mark.parametrize('quote_option', [
    'USDT', 'USDC（Hyperliquid 原生结算，ccxt 符号 BTC/USDC:USDC）',
])
def test_question_quote_answer_binds_to_new_scope_document(env, monkeypatch, quote_option):
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.interactions import create_interaction, respond
    from nerya.db.repositories import AgentSessionRepository

    runtime = CommandRuntime(env, lambda *_: {'stopped_reason': 'end_turn'}, epoch='scope-question')
    monkeypatch.setattr(runtime, 'kick', lambda _: None)
    text = '创建一个 Hyperliquid BTC 永续趋势策略，回测最近7天，不启用交易。'
    admit(env, cid='scope-question-original', sid='scope-question-session', text=text)
    with closing(connect(env.paths.db)) as con:
        AgentSessionRepository(con).upsert_session(session_id='scope-question-session',
            source='user_chat', title=text)
        con.commit()
    env.data['runtime'].update(command_id='scope-question-original',
                               command_session_id='scope-question-session',
                               command_turn_id='turn-scope-question-original',
                               financial_actor_id='owner')
    question = create_interaction(env, sid='scope-question-session',
        tid='turn-scope-question-original', call_id='scope-question-call', kind='question',
        payload={'title': '报价资产', 'questions': [{'id': 'quote_asset', 'question': 'Quote?',
            'options': [quote_option, 'USDT（ccxt 符号 BTC/USDT:USDT）',
                        'USD（ccxt 符号 BTC/USD:USD）']}]})
    with runtime.store.transaction() as con:
        runtime.store._queue(con, 'scope-question-session')
        AgentSessionRepository(con).save_turn_checkpoint(session_id='scope-question-session',
            turn_id='turn-scope-question-original', checkpoint={'resumable': True})
        con.execute("UPDATE agent_commands SET state='awaiting_input' WHERE command_id=?",
                    ('scope-question-original',))
    continuation = respond(runtime, {'session_id': 'scope-question-session',
        'interaction_id': question['interaction_id'], 'response_id': 'scope-question-answer',
        'expected_revision': question['revision'], '_auth_actor_id': 'owner',
        '_auth_scopes': ['execute:strategy'], 'action': 'answer', 'text': '',
        'answers': {'quote_asset': {'selected': [quote_option], 'text': ''}}})['command']
    resumed = runtime.store.claim('scope-question-session', 'scope-question-resumed')
    env.data['runtime'].update(command_id=continuation['command_id'], command_turn_id=resumed['turn_id'])

    def choose(docs):
        first = docs[0]
        fields = dict.fromkeys(scope.FIELDS)
        base = first['text'].index('BTC')
        window = first['text'].index('最近7天')
        venue = first['text'].index('Hyperliquid')
        fields['venue'] = {'command_id': first['command_id'], 'start': venue,
                           'end': venue + len('Hyperliquid'), 'text': 'Hyperliquid'}
        if len(docs) == 1:
            fields.update(
                asset={'command_id': first['command_id'], 'start': base, 'end': base + 3, 'text': 'BTC'},
                window_days={'command_id': first['command_id'], 'start': window, 'end': window + 4, 'text': '最近7天'},
            )
            return {'status': 'resolved', 'fields': fields, 'additional_resources': [], 'amendments': []}
        answer = docs[-1]
        assert answer['text'] == quote_option
        quote_asset = quote_option.split('（', 1)[0]
        quote = answer['text'].index(quote_asset)
        fields.update(
            base={'command_id': first['command_id'], 'start': base, 'end': base + 3, 'text': 'BTC'},
            quote={'command_id': answer['command_id'], 'start': quote,
                   'end': quote + len(quote_asset), 'text': quote_asset},
            window_days={'command_id': first['command_id'], 'start': window, 'end': window + 4, 'text': '最近7天'},
        )
        excluded = []
        if 'Hyperliquid' in answer['text']:
            start = answer['text'].index('Hyperliquid')
            end = answer['text'].index('BTC/')
            excluded.append({'command_id': answer['command_id'], 'start': start,
                             'end': end, 'text': answer['text'][start:end],
                             'role': 'provider_route'})
        return {'status': 'resolved', 'fields': fields, 'additional_resources': [],
                'excluded_spans': excluded,
                'amendments': [{'command_id': answer['command_id'], 'start': 0,
                                'end': len(answer['text']), 'text': answer['text']}]}

    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints']['resources'] == [
        {'venue': 'hyperliquid', 'base': 'BTC', 'quote': quote_option.split('（', 1)[0]}]
    original_scope = scope.current_contract(env, command_id='scope-question-original')
    for key in ('window_days', 'window_anchor', 'start_utc', 'end_utc'):
        assert contract['constraints'][key] == original_scope['constraints'][key]
    assert scope.current_contract(env) == contract
    assert contract['binding']['original_command_id'] == 'scope-question-original'
    assert contract['binding']['documents'][0]['text'] == text
    if contract['evidence'].get('excluded_spans'):
        unbound = deepcopy(contract['evidence'])
        unbound['amendments'] = []
        with pytest.raises(scope.ScopeError):
            scope._canonical(unbound, scope._snapshot(env), original_scope, previous=original_scope)


def test_answer_pool_restatement_binds_confirmed_value_not_span_object(env, monkeypatch):
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.interactions import create_interaction, respond
    from nerya.db.repositories import AgentSessionRepository

    runtime = CommandRuntime(env, lambda *_: {'stopped_reason': 'end_turn'}, epoch='aave-pool')
    monkeypatch.setattr(runtime, 'kick', lambda _: None)
    text = '创建一个 Aave V3 Ethereum USDC 收益率策略，回测最近30天，不启用交易。'
    pool = 'aa70268e-4b52-42bf-a116-608b370f9501'
    other = 'bb111111-2222-3333-4444-555555555555'
    option = pool + '（Aave V3 Ethereum USDC 主池，ID ' + pool + '；备用池 ' + other + ' 未经确认）'
    admit(env, cid='aave-pool-original', sid='aave-pool-session', text=text)
    with closing(connect(env.paths.db)) as con:
        AgentSessionRepository(con).upsert_session(session_id='aave-pool-session', source='user_chat', title=text)
        con.commit()
    env.data['runtime'].update(command_id='aave-pool-original', command_session_id='aave-pool-session',
                               command_turn_id='turn-aave-pool-original', financial_actor_id='owner')
    question = create_interaction(env, sid='aave-pool-session',
        tid='turn-aave-pool-original', call_id='aave-pool-call', kind='question',
        payload={'title': '确认池', 'questions': [{'id': 'pool', 'question': 'Pool?', 'options': [option]}]})
    with runtime.store.transaction() as con:
        runtime.store._queue(con, 'aave-pool-session')
        AgentSessionRepository(con).save_turn_checkpoint(session_id='aave-pool-session',
            turn_id='turn-aave-pool-original', checkpoint={'resumable': True})
        con.execute('UPDATE agent_commands SET state=? WHERE command_id=?', ('awaiting_input', 'aave-pool-original'))
    continuation = respond(runtime, {'session_id': 'aave-pool-session',
        'interaction_id': question['interaction_id'], 'response_id': 'aave-pool-answer',
        'expected_revision': question['revision'], '_auth_actor_id': 'owner',
        '_auth_scopes': ['execute:strategy'], 'action': 'answer', 'text': '',
        'answers': {'pool': {'selected': [option], 'text': ''}}})['command']
    resumed = runtime.store.claim('aave-pool-session', 'aave-pool-resumed')
    env.data['runtime'].update(command_id=continuation['command_id'], command_turn_id=resumed['turn_id'])

    def choose(docs):
        first = docs[0]
        fields = dict.fromkeys(scope.FIELDS)
        def bind(doc, value):
            start = doc['text'].index(value)
            return {'command_id': doc['command_id'], 'start': start, 'end': start + len(value), 'text': value}
        fields.update(protocol=bind(first, 'Aave V3'), chain=bind(first, 'Ethereum'),
                      asset=bind(first, 'USDC'), window_days=bind(first, '最近30天'),
                      **({} if len(docs) == 1 else {'pool': bind(docs[-1], pool)}))
        return {'status': 'resolved', 'fields': fields, 'additional_resources': [],
                'amendments': [], 'excluded_spans': []}

    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints']['resources'][0]['pool'] == pool
    snapshot = scope._snapshot(env)
    answer_doc = snapshot['binding']['documents'][-1]
    output = choose(snapshot['binding']['documents'])
    second = answer_doc['text'].index(pool, answer_doc['text'].index(pool) + 1)
    output['fields']['pool'].update(start=second, end=second + len(pool))
    verified = scope._canonical(output, snapshot, contract, previous=contract)
    assert verified['constraints']['resources'][0]['pool'] == pool
    output['fields']['pool'] = {**output['fields']['pool'],
                               'start': answer_doc['text'].index(other),
                               'end': answer_doc['text'].index(other) + len(other), 'text': other}
    with pytest.raises(scope.ScopeError) as raised:
        scope._canonical(output, snapshot, contract, previous=contract)
    assert raised.value.diagnostic_metadata['reason'] == 'unbound_followup_span'

def test_explicit_user_scope_change_gets_new_bound_revision(env):
    text = 'Change quote asset to USDC and retain all other constraints.'
    original = assistant_chain(env, text)
    def choose(docs):
        result = output(docs[0])
        pos = docs[-1]['text'].index('USDC')
        result['fields']['quote'] = {'command_id': docs[-1]['command_id'], 'start': pos, 'end': pos + 4, 'text': 'USDC'}
        result['amendments'] = [{'command_id': docs[-1]['command_id'], 'start': 0, 'end': len(text), 'text': text}]
        return result
    changed = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert changed['revision'] == 2 and changed['constraints']['quote'] == 'USDC'
    assert changed['constraints']['end_utc'] == original['constraints']['end_utc']
    assert changed['request_scope_hash'] != original['request_scope_hash']
    assert scope.current_contract(env) == changed


def test_followup_cannot_amend_from_title_without_change_evidence(env):
    assistant_chain(env, 'Continue the USDC task')
    def choose(docs):
        result = output(docs[0])
        start = docs[-1]['text'].index('USDC')
        result['fields']['quote'] = {'command_id': docs[-1]['command_id'], 'start': start, 'end': start + 4, 'text': 'USDC'}
        return result
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(choose))


@pytest.mark.parametrize('change', ['actor', 'parent_text', 'binding'])
def test_followup_ownership_and_original_parent_binding_cannot_be_borrowed(env, change):
    assistant_chain(env, 'Continue')
    with closing(connect(env.paths.db)) as con, con:
        if change == 'actor':
            con.execute("UPDATE assistant_work SET actor_id='other'")
        elif change == 'parent_text':
            con.execute("UPDATE agent_commands SET request_json='{}' WHERE command_id='parent-create'")
        else:
            con.execute("UPDATE agent_commands SET context_json='{}' WHERE command_id='parent-follow'")
    fake = FakeGateway()
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=fake)
    assert not fake.calls


def replay_fixture(config, pid, sid, binding, contract, *, market='BINANCE:ETHUSDT'):
    from nerya.evolution.patch_proposal import list_proposals
    from test_task_delivery import _persist_replay_outputs
    proposal = next(p for p in list_proposals(config.paths) if p.id == pid)
    start, end = contract['constraints']['start_utc'], contract['constraints']['end_utc']
    data = {'ok': True, 'backtest_status': 'completed', 'verdict': 'FAIL',
        'performance_evidence': True, 'requested_window_complete': True,
        'start_utc': scope._iso(start), 'end_utc': scope._iso(end), 'tf': '1h',
        'run_receipt': 'backtests/scope-positive/run.json',
        'replay': {'errors': 0, 'sdk_errors': 0, 'order_accounting_ok': True},
        'provenance': {'data_kind': 'historical', 'source_revision': binding['revision'],
            'source_changed_during_run': False, 'datasets': [{'market': market, 'timeframe': '1h',
                'rows': 720, 'first_ts': int(start), 'last_ts': int(end - 3600), 'duplicate_timestamps': 0,
                'out_of_order': False, 'sha256': 'd' * 64}]}}
    _persist_replay_outputs(config, proposal, sid, data)
    return data


def test_positive_exact_request_replay_and_complete_delivery_then_stale(env, monkeypatch, fake_execution):
    from types import SimpleNamespace
    from pathlib import Path
    from nerya.tools.native import delivery
    from nerya.tools.native.skill import SkillIndex
    from nerya.skills.registry import SkillRegistry
    from nerya.task_delivery import read_delivery, update_delivery
    install_fake(monkeypatch, FakeGateway())
    registry = SkillRegistry.load_builtin()
    deps = SimpleNamespace(config=env, paths=env.paths, active_session_id='scope-session',
        skill_index=SkillIndex([], registry_provider=lambda: registry))
    fence = chr(96) * 3
    skill = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/SKILL.md'
    args = json.loads(skill.read_text().split('Start once before authoring', 1)[1].split(fence + 'json', 1)[1].split(fence, 1)[0])
    started = delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments=args), deps=deps)
    assert not started.is_error, started.text()
    created = draft(env)
    assert not created.is_error, created.text()
    pid = created.content[0].data['proposal_id']
    binding = source_binding(env, ToolCall(name='strategy_backtest', arguments={'proposal_id': pid}))
    contract = scope.current_contract(env)
    seen = []
    def run(**kwargs):
        seen.append(kwargs)
        assert kwargs['settings']['start_utc'] == scope._iso(contract['constraints']['start_utc'])
        assert kwargs['settings']['end_utc'] == scope._iso(contract['constraints']['end_utc'])
        return replay_fixture(env, pid, 'scope_fixture', binding, contract)
    monkeypatch.setattr('nerya.skills.builtin.backtest.scripts.backtest_run.run_strategy_backtest', run)
    replay = strategy_runtime.strategy_backtest_handler(ToolCall(name='strategy_backtest', arguments={
        'proposal_id': pid, 'engine': 'native'}), config=env)
    assert not replay.is_error, replay.text()
    assert replay.content[0].data['request_scope']['status'] == 'passed', replay.content[0].data['request_scope']
    assert len(seen) == 1
    row = read_delivery(env.paths, command_id='scope-1', session_id='scope-session')
    checks = {c['check_id']: c for c in row['checks']}
    assert checks['request_scope_match']['status'] == 'passed'
    assert checks['request_scope_match']['required'] is True
    row = update_delivery(env.paths, command_id='scope-1', session_id='scope-session', expected_revision=row['delivery_revision'],
        stages=[{'id': s['id'], 'state': 'completed'} for s in args['stages']],
        artifacts=[{'artifact_id': a, 'ref': checks[c]['evidence_ref']} for a, c in (
            ('candidate', 'source_binding'), ('report', 'report_output'), ('run_receipt', 'historical_run'))])
    final = delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize', arguments={'expected_revision': row['delivery_revision']}), deps=deps)
    assert tool_json_data(final)['completion']['allowed'], tool_json_data(final)['completion']
    # Altering producer evidence after completion invalidates its scope receipt.
    metrics = env.paths.root / 'backtests/scope-positive/metrics.json'
    data = json.loads(metrics.read_text())
    data['provenance']['datasets'][0]['market'] = 'BINANCE:ETHUSDC'
    metrics.write_text(json.dumps(data))
    versions = delivery_source_versions(env, tool_json_data(final))
    assert versions[binding['artifact_id']] == binding['revision']
    assert versions[(binding['artifact_id'], 'request_scope_match')] == 'request_scope_stale'


def test_wrong_replay_scope_cannot_be_overwritten_by_later_validation(env, monkeypatch, fake_execution):
    install_fake(monkeypatch, FakeGateway())
    created = draft(env)
    pid = created.content[0].data['proposal_id']
    binding = source_binding(env, ToolCall(name='strategy_validate', arguments={'proposal_id': pid}))
    data = replay_fixture(env, pid, 'scope_fixture', binding, scope.current_contract(env), market='BINANCE:ETHUSDC')
    result = attach_strategy_checks(ToolResult.from_json(tool_use_id='replay', name='strategy_backtest', data=data),
                                   before=binding, after=binding, kind='backtest', config=env)
    checks = {c['check_id']: c for c in result.metadata['verification']['checks']}
    assert checks['request_scope_match']['status'] == 'failed'
    later = strategy_runtime.strategy_validate_handler(ToolCall(name='strategy_validate', arguments={'proposal_id': pid}), config=env)
    assert 'request_scope_match' not in {c['check_id'] for c in later.metadata['verification']['checks']}


def spans_output(doc, **values):
    fields = dict.fromkeys(scope.FIELDS)
    for key, text in values.items():
        start = doc['text'].index(text)
        fields[key] = {'command_id': doc['command_id'], 'start': start, 'end': start + len(text), 'text': text}
    return {'status': 'resolved', 'fields': fields, 'additional_resources': [], 'amendments': []}


def test_pool_price_authorization_provider_text_is_scope_amendment(env):
    authorization = '允许使用 GeckoTerminal 的 Solana SOL/USDC 池价作为 Jupiter 的 paper 模拟价格源。'
    assert scope._scope_amendment_requested(authorization) is True
    assert scope._scope_amendment_requested('可以自行决定策略参数，但不要使用池价模拟。') is False


@pytest.mark.parametrize('amend', [True, False])
def test_lp_wrapped_asset_requires_bound_user_amendment_preserving_original_window(env, amend):
    text = ('本次仅将池资产绑定改为 WETH/USDC，基础资产使用原请求资产的包装形式，报告明确标注；'
            '保留原协议、原区间与策略目标，不执行包装、授权或交易。')
    initial = '新建 Uniswap v3 ETH/USDC LP策略，回测最近30天，不启用交易。'
    def original_output(docs):
        return spans_output(docs[0], protocol='Uniswap v3', base='ETH', quote='USDC', window_days='30天')
    original = assistant_chain(env, text, initial_text=initial, initial_choose=original_output)
    def choose(docs):
        result = original_output(docs)
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        if amend:
            result['amendments'] = [{'command_id': docs[-1]['command_id'], 'start': 0,
                                      'end': len(text), 'text': text}]
        return result
    if not amend:
        with pytest.raises(scope.ScopeError):
            scope.ensure_contract(env, gateway=FakeGateway(choose))
        assert scope.current_contract(env, command_id='child-create') == original
        return
    changed = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert changed['revision'] == 2
    assert changed['constraints']['base'] == 'WETH'
    assert changed['constraints']['resources'][0]['base'] == 'WETH'
    expected = deepcopy(original['constraints'])
    expected['base'] = expected['resources'][0]['base'] = 'WETH'
    assert changed['constraints'] == expected
    assert changed['request_scope_hash'] != original['request_scope_hash']
    assert scope.current_contract(env, command_id='child-create') == original
    # Reopen configuration/storage: the admitted scope is durable, not a model
    # assertion or a global native/wrapped alias.
    reopened = Config(env.paths, deepcopy(env.data))
    assert scope.current_contract(reopened) == changed
    candidate = {'markets': ['UNISWAP_V3:WETH/USDC'], 'backtest': {'window_days': 30}}
    assert scope.compare(changed, candidate)
    with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
        scope.compare(original, candidate)


@pytest.mark.parametrize('text,values,market', [
    ('新建 ETH/USDT 均线+Agent确认策略，回测最近30天，不启用交易。',
     {'base': 'ETH', 'quote': 'USDT', 'window_days': '30天'}, 'BINANCE:ETHUSDT'),
    ('新建 BTC/USDT 均线策略，回测最近30天，不启用交易。',
     {'base': 'BTC', 'quote': 'USDT', 'window_days': '30天'}, 'BINANCE:BTCUSDT'),
    ('新建一个 Aave USDC 收益策略，回测最近30天，不启用交易。',
     {'protocol': 'Aave', 'asset': 'USDC', 'window_days': '30天'}, 'AAVE_V3:ETH-USDC'),
    ('新建 Uniswap v3 ETH/USDC LP策略，回测最近30天，不启用交易。',
     {'protocol': 'Uniswap v3', 'base': 'ETH', 'quote': 'USDC', 'window_days': '30天'}, 'UNISWAP_V3:ETH/USDC'),
    ('新建 Jupiter SOL/USDC 趋势策略，回测最近30天，不启用交易。',
     {'venue': 'Jupiter', 'base': 'SOL', 'quote': 'USDC', 'window_days': '30天'}, 'JUPITER:SOL/USDC'),
])
def test_real_simple_prompt_shapes_have_positive_producer_scope(env, monkeypatch, fake_execution, text, values, market):
    admit(env, cid='shape', sid='shape-session', text=text)
    fake = FakeGateway(lambda docs: spans_output(docs[0], **values))
    install_fake(monkeypatch, fake)
    # Keep the two contracts separate: extraction proves the admitted scope;
    # LP draft authoring may then fail closed on missing canonical identity.
    if market == 'UNISWAP_V3:ETH/USDC':
        scope.ensure_contract(env, gateway=fake)
    created = draft(env, market)
    contract = scope.current_contract(env)
    assert set(contract['constraints']['resources'][0]) == set(values) - {'window_days'}
    if market == 'UNISWAP_V3:ETH/USDC':
        # Scope extraction is positive, but LP authoring is intentionally
        # fail-closed until the draft carries canonical pool identity and
        # lp_source.  Do not turn a scope fixture into a fake pool receipt.
        assert created.is_error
        payload = tool_json_data(created)
        assert payload['reason'] == 'capability_gap'
        assert payload['status'] == 'blocked'
        assert payload['capability_gap']['code'] == 'lp_pool_identity_required'
        assert payload['capability_gap']['retryable'] is False
        assert payload['capability_gap']['missing'] == ['canonical_pool_identity', 'requested_window_history']
        assert payload['next_required_action']['preserve_requested_scope'] is True
        assert payload['next_required_action']['allow_proxy_substitution'] is False
        return
    assert not created.is_error, created.text()
    submitted = strategy_runtime.strategy_submit_proposal_handler(ToolCall(name='strategy_submit_proposal', arguments={
        'proposal_id': created.content[0].data['proposal_id']}), config=env)
    assert not submitted.is_error, submitted.text()
    assert submitted.content[0].data['request_scope']['status'] == 'passed'
    pinned = scope.compare(contract, manifest(market))
    end = contract['constraints']['end_utc']
    assert pinned == {'start_utc': scope._iso(end - 30 * 86400), 'end_utc': scope._iso(end)}
    admitted_window = {key: scope._iso(contract['constraints'][key]) for key in ('start_utc', 'end_utc')}
    assert tool_json_data(created)['request_scope_window'] == admitted_window
    from nerya.llm.tool_compaction import compact_tool_result
    compacted = compact_tool_result('strategy_draft_proposal', tool_json_data(created), size_threshold=1)
    assert compacted.kept['request_scope_window'] == admitted_window
    assert len(fake.calls) == 1


@pytest.mark.parametrize('market', ['ETHEREUM:USDC', 'EVM:USDC'])
def test_unparseable_candidate_market_returns_precise_repair_without_scope_reextraction(env, monkeypatch, market):
    admit(env, cid='aave-market', sid='aave-market-session',
          text='新建一个 Aave USDC 收益策略，回测最近30天，不启用交易。')
    gateway = FakeGateway(lambda docs: spans_output(docs[0], protocol='Aave', asset='USDC', window_days='30天'))
    install_fake(monkeypatch, gateway)
    call = ToolCall(name='strategy_draft_proposal')
    failure, pinned = scope.guard(env, call, manifest=manifest(market))
    assert failure.is_error and pinned == {}
    payload = tool_json_data(failure)
    assert payload['reason'] == 'request_scope_unverified'
    assert payload['diagnostic_code'] == 'candidate_market_unresolved'
    hint = payload['next_required_action']
    assert hint['action'] == 'repair_candidate_resource'
    assert hint['safe_to_retry'] is False
    assert hint['differences'] == [{'field': 'markets[0]', 'reason': 'market_identifier_unresolved'}]
    assert 'RPC' in hint['message']
    assert scope.current_contract(env)['constraints']['asset'] == 'USDC'
    valid, pinned = scope.guard(env, call, manifest=manifest('AAVE_V3:ETH-USDC'))
    assert valid is None and pinned
    assert len(gateway.calls) == 1


def test_onchain_pool_market_identity_is_parsed_without_proxy_substitution():
    pool = '2QdhepnKRTLjjSqPL1PtKNwqrUkoLee5Gqs8bvZhRdMv'
    assert scope._market(f'ONCHAIN:solana:{pool}') == {'chain': 'solana', 'pool': pool}
    expected = {'venue': 'Jupiter', 'base': 'SOL', 'quote': 'USDC', 'chain': 'solana', 'pool': pool}
    assert scope._differences(expected, {'chain': 'solana', 'pool': pool}) == []
    assert scope._protocol('Raydium V3')['id'] == 'raydium_v3'


@pytest.mark.parametrize('field', ['venue', 'timeframe', 'window_days'])
def test_extractor_cannot_drop_explicit_native_name_or_time_constraint(env, field):
    def choose(docs):
        result = output(docs[0]); result['fields'][field] = None
        return result
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(choose))


def test_execution_budget_is_scoped_without_becoming_strategy_timeframe(env):
    text = 'Binance BTC/USDT 趋势策略，使用 1h K线，回测最近 30 天；最多 20 次模型调用、100000 token、30 分钟，不启用交易。'
    admit(env, cid='budget', sid='budget-session', text=text)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
        docs[0], venue='Binance', base='BTC', quote='USDT', timeframe='1h',
        window_days='30 天', execution_budget='30 分钟')))
    assert contract['constraints']['timeframe'] == 3600
    assert contract['constraints']['window_days'] == 30
    assert contract['constraints']['execution_budget'] == 1800
    assert contract['normalized_evidence']['fields']['execution_budget']['text'] == '30 分钟'


def test_execution_budget_does_not_cover_missing_candle_timeframe(env):
    text = 'Binance BTC/USDT 趋势策略，使用 1h K线，回测最近 30 天；最多 20 次模型调用、30 分钟，不启用交易。'
    admit(env, cid='budget-missing-timeframe', sid='budget-missing-timeframe-session', text=text)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
            docs[0], venue='Binance', base='BTC', quote='USDT',
            window_days='30 天', execution_budget='30 分钟')))


def test_multiple_explicit_resources_preserve_all_pairs(env):
    text = 'Binance BTC/USDT and ETH/USDT strategies, last 30 days'
    admit(env, cid='multi', sid='multi-session', text=text)
    def choose(docs):
        doc = docs[0]
        result = spans_output(doc, venue='Binance', base='BTC', quote='USDT', window_days='30 days')
        other = dict.fromkeys(scope.RESOURCE_FIELDS)
        other['venue'] = result['fields']['venue']
        for key, value in [('base', 'ETH'), ('quote', 'USDT')]:
            start = doc['text'].index(value, doc['text'].index('ETH'))
            other[key] = {'command_id': doc['command_id'], 'start': start, 'end': start + len(value), 'text': value}
        result['additional_resources'] = [other]
        return result
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    candidate = {**manifest(), 'markets': ['BINANCE:ETHUSDT', 'BINANCE:BTCUSDT']}
    assert scope.compare(contract, candidate)
    with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
        scope.compare(contract, manifest())


def test_lending_financial_binding_cannot_disagree_with_declared_market(env):
    text = 'Aave USDC yield, last 30 days'
    admit(env, cid='lending', sid='lending-session', text=text)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
        docs[0], protocol='Aave', asset='USDC', window_days='30 days')))
    candidate = {**manifest('AAVE_V3:ETH-USDC'), 'params': {'financial_request': {
        'protocol': 'aave_v3', 'component_id': 'builtin:aave', 'asset': 'USDC', 'chain': 'ethereum'}}}
    assert scope.compare(contract, candidate)
    candidate['params']['financial_request']['asset'] = 'USDT'
    with pytest.raises(scope.ScopeError) as raised:
        scope.compare(contract, candidate)
    assert 'financial.asset' in {d['field'] for d in raised.value.differences}


def test_lending_pool_binding_survives_market_grammar_and_rejects_substitution(env):
    pool = 'aa70268e-4b52-42bf-a116-608b370f9501'
    text = f'Aave V3 Ethereum USDC yield strategy, pool {pool}, last 30 days'
    admit(env, cid='aave-pool', sid='aave-pool-session', text=text)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
        docs[0], protocol='Aave V3', chain='Ethereum', asset='USDC', pool=pool, window_days='30 days')))
    assert contract['constraints']['resources'] == [{
        'protocol': {'id': 'aave_v3', 'component_id': 'builtin:aave', 'market_types': ['lending']},
        'asset': 'USDC', 'chain': 'ethereum', 'pool': pool}]
    candidate = {**manifest('AAVE_V3:ethereum-USDC'), 'params': {
        'rate_source': {'pool_id': pool, 'project': 'aave-v3', 'chain': 'Ethereum', 'asset': 'USDC'},
        'financial_request': {'protocol': 'aave_v3', 'component_id': 'builtin:aave',
                              'asset': 'USDC', 'chain': 'ethereum', 'pool_id': pool}}}
    assert scope.compare(contract, candidate)
    candidate['params']['financial_request'].pop('pool_id')
    assert scope.compare(contract, candidate)
    candidate['params']['rate_source']['pool_id'] = 'bb111111-2222-3333-4444-555555555555'
    with pytest.raises(scope.ScopeError) as raised:
        scope.compare(contract, candidate)
    assert 'pool' in {d['field'] for d in raised.value.differences}
    candidate['params']['financial_request']['pool_id'] = pool
    with pytest.raises(scope.ScopeError) as raised:
        scope.compare(contract, candidate)
    assert {d['field'] for d in raised.value.differences} == {'params.pool_id'}
    assert next(iter(raised.value.differences))['reason'] == 'conflicting_pool_bindings'
    with pytest.raises(scope.ScopeError) as raised:
        scope.compare(contract, manifest('AAVE_V3:ethereum-USDC'))
    assert 'pool' in {d['field'] for d in raised.value.differences}


def test_lp_pool_binding_survives_market_grammar_and_rejects_substitution(env):
    pool = '0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640'
    other_pool = '0x1111111111111111111111111111111111111111'
    text = f'Uniswap v3 Ethereum WETH/USDC LP strategy, pool {pool}, last 30 days'
    admit(env, cid='lp-pool', sid='lp-pool-session', text=text)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
        docs[0], protocol='Uniswap v3', chain='Ethereum', base='WETH', quote='USDC',
        pool=pool, window_days='30 days')))
    original = deepcopy(contract)
    assert contract['constraints']['resources'][0]['pool'] == pool
    candidate = {**manifest('UNISWAP_V3:WETH/USDC'), 'chain': 'ethereum', 'params': {}}
    bindings = ('lp_source', 'lp_execution', 'financial_request')
    financial = {'protocol': 'uniswap_v3', 'chain': 'ethereum', 'market': 'UNISWAP_V3:WETH/USDC'}
    with pytest.raises(scope.ScopeError) as raised:
        scope.compare(contract, candidate)
    assert {'field': 'pool', 'expected': pool, 'actual': None} in raised.value.differences
    for key in bindings:
        candidate['params'] = {key: {'pool_id': other_pool, **(financial if key == 'financial_request' else {})}}
        with pytest.raises(scope.ScopeError) as raised:
            scope.compare(contract, candidate)
        assert 'pool' in {d['field'] for d in raised.value.differences}
        candidate['params'] = {binding: {
            'pool_id': other_pool if binding == key else pool,
            **(financial if binding == 'financial_request' else {}),
        } for binding in bindings}
        with pytest.raises(scope.ScopeError) as raised:
            scope.compare(contract, candidate)
        assert any(d['field'] == 'params.pool_id' and d.get('reason') == 'conflicting_pool_bindings'
                   for d in raised.value.differences)
    for keys in (('lp_source',), ('lp_execution',), bindings):
        candidate['params'] = {key: {
            'pool_id': pool, **(financial if key == 'financial_request' else {}),
        } for key in keys}
        assert scope.compare(contract, candidate)
        for market in ('UNISWAP_V3:ETH/USDC', 'UNISWAP_V3:WETH/USDT'):
            wrong_pair = {**candidate, 'markets': [market]}
            with pytest.raises(scope.ScopeError) as raised:
                scope.compare(contract, wrong_pair)
            assert {'base', 'quote'} & {d['field'] for d in raised.value.differences}
    assert contract == original


def test_legacy_followup_lazily_resolves_original_request_without_saved_scope(env):
    original = assistant_chain(env, 'Continue the task')
    with closing(connect(env.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET context_json='{}' WHERE command_id='child-create'")
    fake = FakeGateway()
    follow = scope.ensure_contract(env, gateway=fake)
    assert follow['constraints'] == original['constraints']
    # One bounded structured-output repair is enough; the original prefix
    # remains intact and the producer does not repeat the same failed call.
    assert len(fake.calls) == 2


def test_chinese_unicode_offsets_are_characters_not_utf8_bytes(env):
    text = '请继续：Jupiter SOL/USDC，回测30天。'
    admit(env, cid='unicode', sid='unicode-session', text=text)
    def choose(docs):
        result = spans_output(docs[0], venue='Jupiter', base='SOL', quote='USDC', window_days='30天')
        result['fields']['venue']['start'] = len(text[:text.index('Jupiter')].encode('utf-8'))
        result['fields']['venue']['end'] = result['fields']['venue']['start'] + len('Jupiter')
        return result
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints']['base'] == 'SOL'
    assert scope.current_contract(env) == contract
    assert contract['evidence']['fields']['venue']['start'] != text.index('Jupiter')


def test_scope_source_hash_stales_when_original_request_changes(env):
    scope.ensure_contract(env, gateway=FakeGateway())
    with closing(connect(env.paths.db)) as con, con:
        raw = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id='scope-1'").fetchone()[0])
        raw['payload']['text'] += ' changed'
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='scope-1'", (json.dumps(raw),))
    with pytest.raises(scope.ScopeError):
        scope.current_contract(env)


@pytest.mark.parametrize('correct', [False, True])
def test_repeated_quote_requires_correct_offset(env, correct):
    text = 'Binance ETH/USDT strategy; USDT data. 1h candles, last 30 days'
    admit(env, cid='repeated-source', sid='repeat-session', text=text)
    def choose(docs):
        value = output(docs[0])
        if not correct:
            value['fields']['quote']['start'] += 1
        return value
    if correct:
        assert scope.ensure_contract(env, gateway=FakeGateway(choose))['constraints']['venue'] == 'binance'
    else:
        with pytest.raises(scope.ScopeError):
            scope.ensure_contract(env, gateway=FakeGateway(choose))


def test_followup_same_unique_quotes_inherit_scope_despite_model_offset_variation(env):
    assistant_chain(env, 'Continue')
    def choose(docs):
        value = output(docs[0])
        if len(docs) > 1:
            for span in value['fields'].values():
                if span is not None:
                    span['start'] += 1
                    span['end'] += 1
        return value
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints']['base'] == 'ETH'
    assert scope.current_contract(env) == contract


def test_stopped_original_does_not_cancel_fresh_authorized_followup_scope(env):
    assistant_chain(env, 'Continue')
    with closing(connect(env.paths.db)) as con, con:
        row=con.execute("SELECT context_json FROM agent_commands WHERE command_id='child-create'").fetchone()
        context=json.loads(row[0]);context['stop_requested']=True
        con.execute("UPDATE agent_commands SET state='interrupted',context_json=? WHERE command_id='child-create'",(json.dumps(context),))
    contract=scope.ensure_contract(env,gateway=FakeGateway(lambda docs:output(docs[0])))
    assert contract['constraints']['base']=='ETH'
    with closing(connect(env.paths.db)) as con, con:
        row=con.execute("SELECT context_json FROM agent_commands WHERE command_id='child-follow'").fetchone()
        context=json.loads(row[0]);context['stop_requested']=True
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='child-follow'",(json.dumps(context),))
    with pytest.raises(scope.ScopeError):scope.ensure_contract(env,gateway=FakeGateway())


def test_candidate_without_window_can_validate_but_replay_window_is_host_pinned(env):
    contract=scope.ensure_contract(env,gateway=FakeGateway())
    candidate=manifest()
    candidate.pop('backtest')
    pinned=scope.compare(contract,candidate)
    assert set(pinned)=={'start_utc','end_utc'}
    with pytest.raises(scope.ScopeError,match='request_scope_mismatch'):
        scope.compare(contract,{**candidate,'backtest':{'window_days':7}})
    with pytest.raises(scope.ScopeError):
        scope.compare(contract,candidate,replay={'provenance':{'datasets':[]}})


def test_no_routine_extraction_retry_or_fallback_after_invalid_json(env, monkeypatch):
    calls = []
    def invalid(self, **kwargs):
        calls.append(kwargs)
        return MessagesResponse(provider=kwargs['model_provider'], model=kwargs['model_id'],
            content=[{'type': 'text', 'text': 'private invalid output'}])
    monkeypatch.setattr(LLMGateway, 'call_messages', invalid)
    result = draft(env)
    assert result.is_error and result.content[0].data['reason'] == 'request_scope_unverified'
    assert len(calls) == 1 and 'private' not in result.text()
    assert not env.paths.proposals.exists()


@pytest.mark.parametrize('failure,code', [
    ('json', 'invalid_json'), ('duplicate', 'invalid_json'), ('schema', 'invalid_schema'),
    ('span', 'invalid_span'), ('coverage', 'incomplete_scope_evidence'),
    ('transport', 'transport_error'), ('rate_limit', 'provider_rate_limited'),
    ('model', 'model_mismatch'), ('truncated', 'incomplete_response'),
    ('nontext', 'invalid_tool_response'), ('ambiguous', 'ambiguous_scope'),
])
def test_extraction_diagnostics_are_safe_and_remain_fail_closed(env, monkeypatch, caplog, failure, code):
    calls = []
    def respond(self, **kwargs):
        calls.append(kwargs)
        doc = json.loads(kwargs['messages'][0]['content'])[0]
        value = output(doc)
        if failure in {'transport', 'rate_limit'}:
            error = RuntimeError('private-provider-body token=secret')
            if failure == 'rate_limit':
                error.status_code = 429
            raise error
        if failure == 'schema': value['private-extra'] = 'private-model-body'
        if failure == 'span': value['fields']['quote']['text'] = 'not in admitted request'
        if failure == 'coverage': value['fields']['venue'] = None
        if failure == 'ambiguous': value['status'] = 'ambiguous'
        raw = ('private-model-body' if failure == 'json' else
               '{"private-key":1,"private-key":2}' if failure == 'duplicate' else json.dumps(value))
        return MessagesResponse(provider=kwargs['model_provider'],
            model='private-wrong-model' if failure == 'model' else kwargs['model_id'],
            stop_reason='max_tokens' if failure == 'truncated' else 'end_turn',
            content=[{'type': 'tool_use', 'name': 'private-tool'}] if failure == 'nontext' else [{'type': 'text', 'text': raw}])
    monkeypatch.setattr(LLMGateway, 'call_messages', respond)
    result = draft(env)
    assert result.is_error and result.semantic_success is False
    data = result.content[0].data
    assert data['reason'] == 'request_scope_unverified'
    assert data['diagnostic_code'] == code
    assert result.error.recovery_hint['diagnostic_code'] == code
    assert result.error.recovery_hint['action'] == 'inspect_scope_extraction'
    assert 'not a connector failure' in result.error.recovery_hint['message']
    assert f'diagnostic_code={code}' in caplog.text and 'command_id=scope-1' in caplog.text
    assert 'private-' not in result.text() + result.error.message + caplog.text
    assert len(calls) == 1 and not env.paths.proposals.exists()
    with closing(connect_readonly(env.paths.db)) as con:
        saved = json.loads(con.execute("SELECT context_json FROM agent_commands WHERE command_id='scope-1'").fetchone()[0])
    assert 'request_scope' not in saved


def test_invalid_admission_diagnostic_never_calls_model(env, caplog):
    with closing(connect(env.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET actor_id='other' WHERE command_id='scope-1'")
    result = draft(env)
    assert result.is_error
    assert result.content[0].data['diagnostic_code'] == 'invalid_binding'
    assert 'diagnostic_code=invalid_binding' in caplog.text


@pytest.mark.parametrize('failure,exception_type,function', [
    ('copy', 'TypeError', '_extract'), ('span', 'ScopeError', '_span'),
])
def test_ensure_host_log_has_bounded_exception_location_without_private_data(
        env, monkeypatch, caplog, failure, exception_type, function):
    import re
    secret = 'PRIVATE_PROVIDER_BODY_AND_LOCAL_SECRET'
    if failure == 'copy':
        def cannot_copy(value):
            private_local = secret
            raise TypeError(private_local)
        monkeypatch.setattr(scope, 'deepcopy', cannot_copy)
        gateway = FakeGateway()
    else:
        def invalid_quote(docs):
            value = output(docs[0])
            value['fields']['quote']['text'] = secret
            return value
        gateway = FakeGateway(invalid_quote)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    records = [r for r in caplog.records if r.name == scope.__name__]
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert f'error_type={exception_type}' in message
    assert f'function={function}' in message
    assert re.search(r'\bline=[1-9][0-9]{0,5}\b', message)
    assert 'command_id=scope-1' in message and len(message) < 400
    assert secret not in caplog.text and TEXT not in caplog.text
    assert 'cannot_copy' not in message and 'invalid_quote' not in message
    assert record.exc_info is None and record.stack_info is None
    with closing(connect_readonly(env.paths.db)) as con:
        context = json.loads(con.execute("SELECT context_json FROM agent_commands WHERE command_id='scope-1'").fetchone()[0])
    assert 'request_scope' not in context


@pytest.mark.parametrize('damage', [None, 'wrong_name', 'two_calls', 'missing_input', 'string_input',
                                   'wrong_stop', 'truncated', 'prose', 'invalid_span', 'invalid_schema'])
def test_preferred_scope_tool_output_is_data_never_executed(env, monkeypatch, damage):
    calls = []
    def respond(self, **kwargs):
        calls.append(kwargs)
        assert kwargs['tool_choice'] == {'type': 'tool', 'name': 'record_request_scope'}
        assert len(kwargs['tools']) == 1 and kwargs['tools'][0]['input_schema'] == scope.SCHEMA
        doc = json.loads(kwargs['messages'][0]['content'])[0]
        value = output(doc)
        if damage == 'invalid_span': value['fields']['quote']['text'] = 'not quoted'
        if damage == 'invalid_schema': value['extra'] = 'private-model-content'
        block = {'type': 'tool_use', 'id': 'scope-output', 'name': 'record_request_scope', 'input': value}
        if damage == 'wrong_name': block['name'] = 'private-executable-tool'
        if damage == 'missing_input': del block['input']
        if damage == 'string_input': block['input'] = json.dumps(value)
        content = [{'type': 'thinking', 'thinking': 'private-reasoning'}, block]
        if damage == 'two_calls': content.append(deepcopy(block))
        if damage == 'prose': content.append({'type': 'text', 'text': 'private-prose'})
        return MessagesResponse(provider=kwargs['model_provider'], model=kwargs['model_id'], content=content,
            stop_reason='end_turn' if damage == 'wrong_stop' else 'max_tokens' if damage == 'truncated' else 'tool_use')
    monkeypatch.setattr(LLMGateway, 'call_messages', respond)
    if damage is None:
        contract = scope.ensure_contract(env)
        assert contract['constraints']['quote'] == 'USDT'
        assert 'private-' not in json.dumps(contract)
        assert scope.ensure_contract(env) == contract
    elif damage == 'prose':
        contract = scope.ensure_contract(env)
        assert contract['constraints']['quote'] == 'USDT'
        assert 'private-' not in json.dumps(contract)
    else:
        result = draft(env)
        assert result.is_error and result.semantic_success is False
        assert result.content[0].data['reason'] == 'request_scope_unverified'
        assert 'private-' not in result.text() + result.error.message
        assert not env.paths.proposals.exists()
    assert len(calls) == 1


@pytest.fixture
def real_assistant_child(assistant_env, monkeypatch):
    env = assistant_env
    monkeypatch.setattr(LLMGateway, 'call_messages', lambda *a, **kw: pytest.fail('REAL_MODEL_FORBIDDEN'))
    env.config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model', 'allowed_tasks': ['agent.loop']}}
    env.config.data['agent']['native']['tier'] = 'medium'
    text = '新建 ETH/USDT 均线+Agent确认策略，回测最近30天，不启用交易。'
    cfg, contract = parent_request(env, text=text, scopes=('execute:strategy',))
    receipt = create_task(env, cfg, contract, prompt=text)
    row = env.manager.store.claim(receipt['task_session_id'], 'scope-child-worker')
    assert row and row['command_id'] == receipt['command_id']
    child = json.loads(row['request_json'])
    with closing(connect_readonly(env.config.paths.db)) as con:
        work = dict(con.execute('SELECT * FROM assistant_work WHERE task_id=?', (receipt['task_id'],)).fetchone())
    saved = json.loads(work['request_json'])
    assert 'assistant_request_binding' not in child  # Real CommandRuntime allowlist.
    assert child['payload']['assistant_request_binding'] == saved['assistant_request_binding']
    assert saved['payload']['assistant_request_binding'] == saved['assistant_request_binding']
    config = Config(env.config.paths, deepcopy(env.config.data))
    config.data['runtime'].update(command_id=row['command_id'], command_session_id=row['session_id'],
                                 command_turn_id=row['turn_id'], financial_actor_id=row['actor_id'])
    return env, config, receipt, child, saved


def test_scope_real_assistant_create_survives_command_runtime_allowlist(real_assistant_child):
    _, config, receipt, _, _ = real_assistant_child
    gateway = FakeGateway(lambda docs: spans_output(docs[0], base='ETH', quote='USDT', window_days='30天'))
    contract = scope.ensure_contract(config, gateway=gateway)
    assert len(gateway.calls) == 1
    assert contract['binding']['original_command_id'] == receipt['command_id']
    assert contract['constraints']['resources'] == [{'base': 'ETH', 'quote': 'USDT'}]
    assert scope.current_contract(config) == contract


@pytest.mark.parametrize('damage', ['child_binding', 'work_binding', 'work_payload_binding', 'parent_binding', 'actor', 'text'])
def test_nested_child_binding_requires_durable_work_and_exact_parent(real_assistant_child, damage):
    _, config, receipt, child, saved = real_assistant_child
    parent_id = saved['assistant_request_binding']['command_id']
    with closing(connect(config.paths.db)) as con, con:
        if damage == 'child_binding':
            child['payload']['assistant_request_binding']['command_id'] = 'foreign'
            con.execute('UPDATE agent_commands SET request_json=? WHERE command_id=?', (json.dumps(child), receipt['command_id']))
        elif damage in {'work_binding', 'work_payload_binding'}:
            target = saved if damage == 'work_binding' else saved['payload']
            target['assistant_request_binding']['command_id'] = 'foreign'
            con.execute('UPDATE assistant_work SET request_json=? WHERE task_id=?', (json.dumps(saved), receipt['task_id']))
        elif damage == 'parent_binding':
            con.execute("UPDATE agent_commands SET context_json='{}' WHERE command_id=?", (parent_id,))
        elif damage == 'actor':
            con.execute("UPDATE assistant_work SET actor_id='foreign' WHERE task_id=?", (receipt['task_id'],))
        else:
            child['payload']['text'] += ' changed'
            con.execute('UPDATE agent_commands SET request_json=? WHERE command_id=?', (json.dumps(child), receipt['command_id']))
    gateway = FakeGateway()
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=gateway)
    assert not gateway.calls


def test_real_followup_inherits_create_binding_after_allowlist(real_assistant_child):
    env, config, receipt, _, _ = real_assistant_child
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (receipt['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (receipt['task_session_id'],))
    text = '继续同一候选 prp_fixture，重新验证并回测最近7天；不要创建新候选、不要改市场、不要启用交易。'
    cfg, parent = parent_request(env, operation='task_follow_up', target=receipt['task_id'],
                                 cid='parent-followup', text=text, scopes=('execute:strategy',))
    follow = followup_task(env, cfg, parent, text=text)
    row = env.manager.store.claim(follow['task_session_id'], 'scope-followup-worker')
    assert row and row['command_id'] == follow['command_id']
    config.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])
    gateway = FakeGateway(lambda docs: spans_output(docs[0], base='ETH', quote='USDT', window_days='30天'))
    contract = scope.ensure_contract(config, gateway=gateway)
    assert contract['constraints']['resources'] == [{'base': 'ETH', 'quote': 'USDT'}]
    assert contract['binding']['original_command_id'] == receipt['command_id']
    assert len(contract['binding']['documents']) == 2 and len(gateway.calls) == 1


@pytest.fixture
def three_scope_documents(real_assistant_child):
    env, config, receipt, _, _ = real_assistant_child
    commands = [receipt['command_id']]
    for index in (1, 2):
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (commands[-1],))
            con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (receipt['task_session_id'],))
        text = '继续完成，不启用交易。'
        cfg, parent = parent_request(env, operation='task_follow_up', target=receipt['task_id'],
            cid=f'prefix-parent-{index}', text=text, scopes=('execute:strategy',))
        follow = followup_task(env, cfg, parent, text=text)
        row = env.manager.store.claim(follow['task_session_id'], f'prefix-worker-{index}')
        assert row and row['command_id'] == follow['command_id']
        commands.append(row['command_id'])
        config.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])
    return env, config, commands


def _original_scope_output(docs):
    result = spans_output(docs[0], base='ETH', quote='USDT', window_days='30天')
    # Preserve main's exact-quote normalization and raw audit evidence.
    result['fields']['base'].update(start=0, end=1)
    return result


def _scope_rows(config):
    with closing(connect_readonly(config.paths.db)) as con:
        return {row['command_id']: json.loads(row['context_json']) for row in con.execute('SELECT * FROM agent_commands')}


def test_incremental_scope_keeps_successful_prefixes_and_retries_only_failed_third(three_scope_documents):
    _, config, commands = three_scope_documents
    calls = []
    def choose(docs):
        calls.append([d['command_id'] for d in docs])
        if len(docs) == 3 and len(calls) == 3:
            error = RuntimeError('private rate-limit response')
            error.status_code = 429
            raise error
        return _original_scope_output(docs)
    fake = FakeGateway(choose)
    with pytest.raises(scope.ScopeError) as failed:
        scope.ensure_contract(config, gateway=fake)
    assert failed.value.diagnostic_code == 'provider_rate_limited'
    saved = _scope_rows(config)
    for index, cid in enumerate(commands[:2]):
        contract = saved[cid]['request_scope']
        assert contract['binding']['command_id'] == cid
        assert [d['command_id'] for d in contract['binding']['documents']] == commands[:index + 1]
        assert contract == scope.current_contract(config, command_id=cid)
        assert contract['evidence']['fields']['base']['start'] == 0
        assert contract['normalized_evidence']['fields']['base']['start'] > 0
    assert 'request_scope' not in saved[commands[2]]
    contract = scope.ensure_contract(config, gateway=fake)
    assert calls == [commands[:1], commands[:2], commands, commands]
    assert contract['binding']['command_id'] == commands[2] and contract['revision'] == 1
    assert scope.ensure_contract(config, gateway=fake) == contract
    assert len(calls) == 4
    assert all(_scope_rows(config)[cid]['request_scope'] == saved[cid]['request_scope'] for cid in commands[:2])


@pytest.mark.parametrize('damage', ['evidence', 'hash', 'binding'])
def test_incremental_scope_rejects_saved_original_tampering_even_with_saved_latest(three_scope_documents, damage):
    env, config, commands = three_scope_documents
    scope.ensure_contract(config, gateway=FakeGateway(_original_scope_output))
    saved = _scope_rows(config)[commands[0]]
    if damage == 'evidence': saved['request_scope']['constraints']['quote'] = 'USDC'
    elif damage == 'hash': saved['request_scope']['request_scope_hash'] = 'a' * 64
    else: saved['request_scope']['binding']['command_id'] = commands[-1]
    with env.manager.store.transaction() as con:
        con.execute('UPDATE agent_commands SET context_json=?,revision=revision+1 WHERE command_id=?',
            (json.dumps(saved), commands[0]))
    fake = FakeGateway(_original_scope_output)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=fake)
    assert not fake.calls


def test_incremental_scope_rejects_nonequivalent_followup_without_losing_original(three_scope_documents):
    _, config, commands = three_scope_documents
    def choose(docs):
        result = _original_scope_output(docs)
        if len(docs) == 2:
            result['fields']['window_days'] = None
        return result
    fake = FakeGateway(choose)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=fake)
    saved = _scope_rows(config)
    assert scope.current_contract(config, command_id=commands[0]) == saved[commands[0]]['request_scope']
    assert 'request_scope' not in saved[commands[1]] and 'request_scope' not in saved[commands[2]]
    # Dropping the window fails closed at the second document with
    # uncovered_duration, which is not eligible for structured-output repair.
    # The third document is never extracted; the committed prefix stays intact.
    assert len(fake.calls) == 2


def test_incremental_scope_cancellation_keeps_only_already_committed_prefix(three_scope_documents):
    from nerya.harness.cancellation import CancelToken
    _, config, commands = three_scope_documents
    token = CancelToken()
    def choose(docs):
        if len(docs) == 2:
            token.cancel()
        return _original_scope_output(docs)
    with pytest.raises(scope.ScopeError) as failed:
        scope.ensure_contract(config, gateway=FakeGateway(choose), token=token)
    assert failed.value.diagnostic_code == 'cancelled'
    saved = _scope_rows(config)
    assert 'request_scope' in saved[commands[0]]
    assert 'request_scope' not in saved[commands[1]] and 'request_scope' not in saved[commands[2]]


def test_incremental_scope_rechecks_active_binding_before_saving_prefix(three_scope_documents):
    env, config, commands = three_scope_documents
    def choose(docs):
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='stopping',revision=revision+1 WHERE command_id=?", (commands[-1],))
        return _original_scope_output(docs)
    fake = FakeGateway(choose)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=fake)
    assert all('request_scope' not in _scope_rows(config)[cid] for cid in commands)
    assert len(fake.calls) == 1


def test_incremental_scope_cas_preserves_concurrent_context_updates(three_scope_documents):
    env, config, commands = three_scope_documents
    def choose(docs):
        cid = docs[-1]['command_id']
        with env.manager.store.transaction() as con:
            context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?', (cid,)).fetchone()[0])
            context['unrelated_context'] = {'preserve': cid}
            con.execute('UPDATE agent_commands SET context_json=?,revision=revision+1 WHERE command_id=?', (json.dumps(context), cid))
        return _original_scope_output(docs)
    fake = FakeGateway(choose)
    scope.ensure_contract(config, gateway=fake)
    saved = _scope_rows(config)
    assert len(fake.calls) == 3
    for cid in commands:
        assert saved[cid]['unrelated_context'] == {'preserve': cid}
        assert saved[cid]['request_scope']['binding']['command_id'] == cid


def test_incremental_scope_rechecks_saved_prefix_changed_during_later_model_call(three_scope_documents):
    env, config, commands = three_scope_documents
    def choose(docs):
        if len(docs) == 2:
            with env.manager.store.transaction() as con:
                context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?', (commands[0],)).fetchone()[0])
                context['request_scope']['request_scope_hash'] = 'tampered'
                con.execute('UPDATE agent_commands SET context_json=?,revision=revision+1 WHERE command_id=?', (json.dumps(context), commands[0]))
        return _original_scope_output(docs)
    fake = FakeGateway(choose)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=fake)
    saved = _scope_rows(config)
    assert len(fake.calls) == 2
    assert 'request_scope' not in saved[commands[1]] and 'request_scope' not in saved[commands[2]]


@pytest.mark.parametrize('family', ['lending', 'lp'])
@pytest.mark.parametrize('damage', [None, 'asset', 'pool', 'window'])
def test_real_prepared_producer_scope_positive_and_exact_failures(env, tmp_path, family, damage):
    from nerya.data_api.registry import DataApiRegistry
    from nerya.data_api.defillama import defillama_specs
    from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs
    from test_defillama_data_api import Fake, POOL, PID
    if family == 'lending':
        text, market = 'Aave USDC yield, last 30 days', 'AAVE_V3:ETH-USDC'
        values = {'protocol': 'Aave', 'asset': 'USDC', 'window_days': '30 days'}
        pool = deepcopy(POOL)
    else:
        text, market = 'Uniswap v3 ETH/USDC LP, last 30 days', 'UNISWAP_V3:ETH/USDC'
        values = {'protocol': 'Uniswap v3', 'base': 'ETH', 'quote': 'USDC', 'window_days': '30 days'}
        pool = {**POOL, 'project': 'uniswap-v3', 'symbol': 'ETH-USDC'}
    admit(env, cid='prepared', sid='prepared-session', text=text)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(docs[0], **values)))
    candidate = {**manifest(market), 'timeframe': '1d'}
    pinned = scope.compare(contract, candidate)
    declaration = {'version': 1, 'datasets': [{'id': 'rates', 'provider': 'defillama', 'action': 'yield_history',
        'args': {'pool_id': PID, 'start': pinned['start_utc'], 'end': pinned['end_utc']}}]}
    root = tmp_path / 'prepared-fixture'
    root.mkdir()
    (root / 'replay_inputs.json').write_text(json.dumps(declaration))
    if damage == 'asset':
        pool['symbol'] = 'USDT' if family == 'lending' else 'ETH-USDT'
    registry = DataApiRegistry()
    for spec in defillama_specs(Fake(pools=[pool])):
        registry.register_action(spec)
    prepared = prepare_inputs(root, registry=registry)
    if damage == 'pool':
        declaration['datasets'][0]['args']['pool_id'] = 'bb70268e-4b52-42bf-a116-608b370f9501'
        prepared['manifest'] = declaration
    if damage == 'window':
        candidate['backtest']['window_days'] = 7
    if damage:
        with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
            scope.validate_prepared(contract, candidate, declaration, prepared)
    else:
        hashes = scope.validate_prepared(contract, candidate, declaration, prepared)
        assert hashes == {prepared['receipts'][0]['snapshot_file']: prepared['receipts'][0]['sha256']}
        assert prepared['receipts'][0]['requested_window_complete'] is not True


def test_freeform_scope_callback_is_invoked_before_candidate_execution(env, tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts import freeform_run
    from nerya.strategies.package import load_package
    root = env.paths.strategies / 'freeform_fixture'
    root.mkdir(parents=True)
    (root / 'strategy.yml').write_text(yaml_io.dumps({'id': 'freeform_fixture', **manifest(),
        'schedule': {'type': 'cron', 'cron': '0 * * * *', 'enabled': False}}))
    (root / 'main.py').write_text('def run(ctx): return ctx.result.hold()\n')
    (root / 'scripts').mkdir()
    (root / 'scripts/custom_replay.py').write_text('pass\n')
    monkeypatch.setattr(freeform_run, 'load_workspace_config', lambda _: env)
    monkeypatch.setattr(freeform_run, '_load_target_package', lambda *args: load_package(env.paths, 'freeform_fixture'))
    prepared = {'files': {}, 'receipts': [], 'manifest': None}
    monkeypatch.setattr('nerya.skills.builtin.backtest.scripts.replay_inputs.prepare_inputs', lambda *a, **kw: prepared)
    monkeypatch.setattr(freeform_run, 'execute_freeform', lambda *a, **kw: pytest.fail('candidate ran before scope guard'))
    seen = []
    def check(value):
        seen.append(value)
        raise scope.ScopeError('request_scope_mismatch')
    with pytest.raises(scope.ScopeError):
        freeform_run.run_freeform_backtest(strategy_id='freeform_fixture', workspace=tmp_path, validate_prepared_inputs=check)
    assert seen == [prepared]


@pytest.mark.parametrize('authorization', [
    '可自行选择同资产池价格做模拟',
    'You may choose prices from a pool of the same assets for simulated fills.',
    '不要用其他池价格做模拟', '可以自行决定策略参数', None,
])
def test_pool_price_permission_is_semantic_exact_admission_not_phrase_matching(env, authorization):
    affirmative = authorization is not None and authorization.startswith(('可自行选择', 'You may'))
    text = '创建 Jupiter SOL/USDC，回测最近7天；' + (authorization or '继续，不交易。')
    admit(env, cid='pool-permission', sid='pool-permission', text=text)
    def choose(docs):
        value = spans_output(docs[0], venue='Jupiter', base='SOL', quote='USDC', window_days='7天')
        if affirmative:
            evidence = spans_output(docs[0], price_simulation=authorization)['fields']['price_simulation']
            value['fields']['price_simulation'] = {'scope': 'same_asset_pool_price', 'evidence': evidence}
        else:
            value['fields'].pop('price_simulation')
        return value
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints'].get('price_simulation') == ('same_asset_pool_price' if affirmative else None)
    assert scope.current_contract(env) == contract
    assert scope.ensure_contract(env, gateway=FakeGateway()) == contract
    if affirmative:
        evidence = contract['normalized_evidence']['fields']['price_simulation']['evidence']
        assert text[evidence['start']:evidence['end']] == authorization
        invalid = choose(scope._snapshot(env)['binding']['documents'])
        invalid['fields']['price_simulation']['scope'] = 'any_pool'
        with pytest.raises(scope.ScopeError):
            scope._chain([invalid], scope._snapshot(env))
        invalid['fields']['price_simulation']['scope'] = 'same_asset_pool_price'
        invalid['fields']['price_simulation']['evidence']['command_id'] = 'candidate-not-authority'
        with pytest.raises(scope.ScopeError):
            scope._chain([invalid], scope._snapshot(env))


@pytest.mark.parametrize('damage', [None, 'non_sol_pair', 'permission', 'mint', 'network', 'venue', 'window',
                                      'bytes', 'units', 'proof', 'candle_hash', 'persisted_bytes'])
def test_pool_prices_require_admitted_permission_and_persisted_producer_bytes(env, monkeypatch, damage):
    import hashlib
    from nerya.data.history_store import normalize_row
    from nerya.skills.builtin.backtest.scripts.replay_inputs import json_bytes
    base = 'USDT' if damage == 'non_sol_pair' else 'SOL'
    text = f'创建一个 Jupiter {base}/USDC 趋势策略，回测最近7天；可自行选择同资产池价格做模拟，不交易。'
    admit(env, cid='pool-native', sid='pool-native', text=text)
    def choose(docs):
        value = spans_output(docs[0], venue='Jupiter', base=base, quote='USDC', window_days='7天')
        evidence = spans_output(docs[0], price_simulation='可自行选择同资产池价格做模拟')['fields']['price_simulation']
        value['fields']['price_simulation'] = {'scope': 'same_asset_pool_price', 'evidence': evidence}
        if damage == 'permission':
            value['fields'].pop('price_simulation')
        return value
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    candidate = {'markets': [f'JUPITER:{base}/USDC'], 'timeframe': '1h',
                 'params': {'price_simulation': {'dataset_id': 'pool'}}}
    pinned = scope.compare(contract, candidate)
    pool = {'network': 'solana', 'address': 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE',
            'base': {'address': 'So11111111111111111111111111111111111111112', 'symbol': 'SOL', 'decimals': 9},
            'quote': {'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'symbol': 'USDC', 'decimals': 6}, 'dex': 'orca'}
    if damage == 'non_sol_pair':
        pool['base'] = {'address': 'A'*32, 'symbol': base, 'decimals': 6}
    identities = [{**pool[role], 'identity_scope': 'current_official_catalog',
                   'catalog_source_sha256': 'c'*64} for role in ('base', 'quote')]
    args = {'network': 'solana', 'pool_address': pool['address'], 'base_token': pool['base']['address'],
            'quote_token': pool['quote']['address'], 'start': pinned['start_utc'], 'end': pinned['end_utc'],
            'timeframe': '1h', 'warmup_bars': 0}
    if damage == 'mint':
        pool['quote']['address'] = args['quote_token'] = 'B'*32
    if damage == 'network':
        pool['network'] = args['network'] = 'ethereum'
    if damage == 'venue':
        candidate['markets'] = ['BINANCE:SOL/USDC']
    declaration = {'version': 1, 'datasets': [{'id': 'pool', 'provider': 'geckoterminal',
                                              'action': 'pool_ohlcv', 'args': args}]}
    start = int(contract['constraints']['start_utc'])
    rows = [{'ts': stamp, 'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 5}
            for stamp in range(start, int(contract['constraints']['end_utc']), 3600)]
    envelope = {'provider': 'geckoterminal', 'action': 'pool_ohlcv', 'mock': False,
                'observation_kind': 'historical_pool_ohlcv', 'pool': pool, 'timeframe': '1h',
                'quote_units': 'quote_token_per_base_token', 'volume_units': 'upstream_reported_usd',
                'warmup_bars': 0, 'requested_window': {'start': args['start'], 'end': args['end'], 'inclusive': False},
                'token_identities': identities,
                'sources': [{'url': 'https://api.geckoterminal.com/api/v2/networks/solana/pools/' + pool['address'],
                             'sha256': 'b'*64, 'hash_basis': 'canonical_parsed_json_utf8_sorted_keys'},
                            {'url': 'https://lite-api.jup.ag/tokens/v2/search?query='
                                    + ','.join(token['address'] for token in identities),
                             'sha256': 'c'*64, 'hash_basis': 'canonical_parsed_json_utf8_sorted_keys'}]}
    if damage == 'window':
        envelope['requested_window']['start'] = scope._iso(start + 3600)
    if damage == 'units':
        envelope['quote_units'] = 'usd'
    blob = json_bytes({'rows': rows, '_envelope': envelope})
    receipt = {'id': 'pool', 'provider': 'geckoterminal', 'action': 'pool_ohlcv',
               'sha256': hashlib.sha256(blob).hexdigest(), 'rows': len(rows), 'sources': envelope['sources'],
               'requested_window': envelope['requested_window'], 'snapshot_file': 'prepared_inputs/pool.json'}
    prepared = {'manifest': declaration, 'files': {'pool.json': blob}, 'receipts': [receipt]}
    if damage == 'bytes':
        prepared['files']['pool.json'] += b' '
    if damage in {'permission', 'mint', 'network', 'venue', 'window', 'bytes', 'units'}:
        with pytest.raises(scope.ScopeError):
            scope.validate_prepared(contract, candidate, declaration, prepared)
        return
    assert scope.validate_prepared(contract, candidate, declaration, prepared) == {receipt['snapshot_file']: receipt['sha256']}
    proof = {'scope': 'historical_pool_price_simulation', 'execution_target': candidate['markets'][0],
             'dataset_id': 'pool', 'prepared_input_sha256': receipt['sha256'],
             'quote_units': pool['quote']['symbol'], 'price_units': envelope['quote_units'],
             **{key: envelope[key] for key in ('pool', 'sources', 'requested_window')}}
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(normalize_row(row), sort_keys=True, ensure_ascii=False, default=str).encode())
        digest.update(b'\n')
    replay = {'run_receipt': 'backtests/pool/run_receipt.json',
              'requested_start_utc': pinned['start_utc'], 'requested_end_utc': pinned['end_utc'],
              'provenance': {'data_kind': 'historical', 'price_simulation': proof, 'prepared_datasets': [receipt],
                  'datasets': [{'market': candidate['markets'][0], 'timeframe': '1h', 'rows': len(rows),
                                'first_ts': rows[0]['ts'], 'last_ts': rows[-1]['ts'], 'sha256': digest.hexdigest()}]}}
    root = env.paths.root / 'backtests/pool/prepared_inputs'
    root.mkdir(parents=True)
    (root / 'pool.json').write_bytes(blob + (b' ' if damage == 'persisted_bytes' else b''))
    if damage == 'proof':
        proof['pool'] = {**pool, 'address': 'different-pool'}
    if damage == 'candle_hash':
        replay['provenance']['datasets'][0]['sha256'] = 'c'*64
    monkeypatch.setattr(scope, 'target_manifest', lambda *_: candidate)
    monkeypatch.setattr(scope, '_input_declaration', lambda *_: declaration)
    binding = {'artifact_id': 'strategy:pool:proposal:installed', 'revision': 'fixture-revision'}
    result = scope.check_receipt(env, binding, replay=replay)
    if damage not in {None, 'non_sol_pair'}:
        assert result['status'] != 'passed'
        with pytest.raises(scope.ScopeError):
            scope.compare(contract, candidate, replay=replay, config=env)
    else:
        assert result['status'] == 'passed', result
        assert result['prepared_inputs'] == {receipt['snapshot_file']: receipt['sha256']}
        assert scope.compare(contract, candidate, replay=replay, config=env) == pinned
    assert scope.current_contract(env) == contract


@pytest.mark.parametrize(('message', 'amend'), [
    ('Compare the price change across different markets.', False),
    ('Do not change the window, continue the original task.', False),
    ('不要调整周期，继续原任务。', False),
    ('Please switch to the weekly window.', True),
    ('不要调整周期，但把资产改为 ETH。', True),
])
def test_scope_amendment_needs_an_affirmative_request(message, amend):
    assert scope._scope_amendment_requested(message) is amend
