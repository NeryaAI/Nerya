"""Direct task-chat admission and scope inheritance, using tmp workspaces only."""
import json
from types import SimpleNamespace

import pytest

from nerya.api import routes_conversation_commands
from nerya.api.auth import AuthResult, authorize_route
from nerya.api.local_server import _stamp_trusted_auth
from nerya.strategies import request_scope as scope
import test_request_scope as request_scope_fixtures
from test_request_scope import FakeGateway, followup_task, parent_request, spans_output

assistant_env = request_scope_fixtures.assistant_env
env = request_scope_fixtures.env
real_assistant_child = request_scope_fixtures.real_assistant_child

pytestmark = pytest.mark.smoke


def original_output(docs):
    return spans_output(docs[0], base='ETH', quote='USDT', window_days='30天')


@pytest.mark.parametrize('amend', [False, True])
def test_host_task_admission_binds_plain_followup_and_extracts_intervening_amendment(env, monkeypatch, amend):
    from copy import deepcopy
    from nerya.agent.assistants import AssistantStore
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.command_store import CommandError

    config, sid = env, 'host-task-context'
    AssistantStore(config.paths).bind(sid, 'main', kind='task')
    manager = CommandRuntime(config, lambda *a: pytest.fail('no runtime execution'))
    monkeypatch.setattr(manager, 'accepted_model', lambda request: None)

    def submit(cid, text, actor='owner'):
        receipt = manager.submit({'command_id': cid, 'session_id': sid,
            '_auth_actor_id': actor, '_auth_scopes': ['execute:strategy'],
            'context': {'task_context_binding': {'root_command_id': 'forged'}},
            'request': {'source': 'user_chat', 'kind': 'user.chat', 'conversation_kind': 'task',
                'task_context_binding': {'root_command_id': 'forged'},
                'payload': {'text': text, 'task_context_binding': {'root_command_id': 'forged'}}}}, start=False)
        row = receipt['command']
        config.data['runtime'].update(command_id=cid, command_session_id=sid,
            command_turn_id=row['turn_id'], financial_actor_id='owner')
        with manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='running' WHERE command_id=?", (cid,))
        return row

    submit('host-root', '创建 ETH/USDT 策略，回测最近30天，不交易。')
    original = scope.ensure_contract(config, gateway=FakeGateway(original_output))
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='awaiting_input' WHERE command_id='host-root'")
    tail = '将同一候选回测窗口改为7天，不交易。' if amend else '只检查同一候选保存的数据，不下载。'
    submit('host-middle', tail)
    with manager.store.transaction() as con:
        # Existing read-only operator turns predate this admission binding.
        context = json.loads(con.execute("SELECT context_json FROM agent_commands WHERE command_id='host-middle'").fetchone()[0])
        context.pop('task_context_binding')
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='host-middle'", (json.dumps(context),))
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='host-middle'")
    for index in range(13):
        submit(f'host-read-{index}', '只检查已保存状态，不下载、不改策略。')
        with manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (f'host-read-{index}',))
            if index == 0:
                # Legacy candidate continuation cached a valid sparse history,
                # omitting an earlier operator request (possibly an amendment).
                root_delivery = {'artifacts': [{'artifact_id': 'candidate', 'ref': 'fixture', 'revision': 'a' * 64}],
                    'checks': [{'check_id': 'source_binding', 'producer_owned': True, 'status': 'passed',
                        'artifact_id': 'strategy:fixture:proposal:prp_fixture', 'evidence_ref': 'fixture', 'revision': 'a' * 64}]}
                con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id='host-root'", (json.dumps(root_delivery),))
                legacy = con.execute("SELECT request_json,context_json FROM agent_commands WHERE command_id='host-read-0'").fetchone()
                request, legacy_context = json.loads(legacy[0]), json.loads(legacy[1])
                request.update(conversation_kind='task', payload={'text': '继续当前候选，不交易。'})
                legacy_context.pop('task_context_binding')
                con.execute("UPDATE agent_commands SET request_json=?,context_json=? WHERE command_id='host-read-0'",
                            (json.dumps(request), json.dumps(legacy_context)))
                legacy_snapshot = scope._snapshot(config, connection=con)
                assert [d['command_id'] for d in legacy_snapshot['binding']['documents']] == ['host-root', 'host-read-0']
                legacy_context['request_scope'] = scope._chain(
                    [original['normalized_evidence'], original['normalized_evidence']], legacy_snapshot)
                con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='host-read-0'", (json.dumps(legacy_context),))
    submit('host-current', '加载当前 backtest Skill，用正式工具回测原 LP 候选一次；缺数据就停止并简要说明，不补数据、不改策略、不交易。')
    snapshot = scope._snapshot(config)
    assert snapshot['task_prefix'] == original  # Valid sparse cache is not an aligned prefix.
    binding = snapshot['context'].get('task_context_binding')
    assert binding and binding['root_command_id'] == 'host-root'
    assert binding['previous_command_id'] == 'host-read-12' and len(binding['history_hash']) == 64
    pending_ids = ['host-middle', *[f'host-read-{i}' for i in range(13)], 'host-current']
    assert [d['command_id'] for d in snapshot['binding']['documents']] == ['host-root', *pending_ids]
    assert snapshot['scope_continuations'] == []
    with manager.store.transaction() as con:
        damaged = deepcopy(legacy_context)
        damaged['request_scope']['request_scope_hash'] = '0' * 64
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='host-read-0'", (json.dumps(damaged),))
        with pytest.raises(scope.ScopeError):
            scope._snapshot(config, connection=con)
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='host-read-0'", (json.dumps(legacy_context),))

    def evidence(docs):
        result = original_output(docs)
        if amend:
            middle = docs[1]
            result['fields']['window_days'] = spans_output(middle, window_days='7天')['fields']['window_days']
            result['amendments'] = [{'command_id': middle['command_id'], 'start': 0, 'end': len(tail), 'text': tail}]
        return result

    def choose(docs):
        return evidence(docs)

    class TailGateway(FakeGateway):
        def call_messages(self, **kwargs):
            response = super().call_messages(**kwargs)
            if amend:
                value = json.loads(response.content[0]['text'])
                response.content = [{'type': 'tool_use', 'id': 'scope-tail',
                    'name': 'record_request_scope', 'input': value}]
                response.stop_reason = 'tool_use'
            if getattr(self, 'truncated', False):
                response.stop_reason = 'max_tokens'
            return response

    truncated = TailGateway(choose)
    truncated.truncated = True
    with pytest.raises(scope.ScopeError) as failure:
        scope.ensure_contract(config, gateway=truncated)
    assert failure.value.diagnostic_code == 'incomplete_response'
    assert len(truncated.calls) == 1
    assert scope._snapshot(config)['context'].get('request_scope') is None
    gateway = TailGateway(choose)
    contract = scope.ensure_contract(config, gateway=gateway)
    assert len(gateway.calls) == 1  # Cached root plus all fifteen tail documents in one response.
    assert gateway.calls[0]['max_tokens'] == 16384
    assert 'extractions' not in contract and len(contract['binding']['documents']) == 16
    assert contract['constraints']['window_days'] == (7 if amend else 30)
    assert contract['normalized_evidence']['fields']['base'] == original['normalized_evidence']['fields']['base']
    if amend:
        with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
            scope.compare(contract, {'markets': ['BINANCE:ETHUSDT'], 'backtest': {'window_days': 30}})
    assert scope.current_contract(config) == contract
    assert scope.ensure_contract(config, gateway=FakeGateway(lambda docs: pytest.fail('cached'))) == contract
    with manager.store.transaction() as con:
        assert json.loads(con.execute("SELECT context_json FROM agent_commands WHERE command_id='host-middle'").fetchone()[0]) == context
        context = json.loads(con.execute("SELECT context_json FROM agent_commands WHERE command_id='host-current'").fetchone()[0])
        for key, value in [('previous_command_id', 'host-root'), ('history_hash', '0' * 64),
                           ('command_ids', ['host-middle']), ('assistant_id', 'other')]:
            damaged = deepcopy(context)
            damaged['task_context_binding'][key] = value
            con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='host-current'", (json.dumps(damaged),))
            with pytest.raises(scope.ScopeError):
                scope._snapshot(config, connection=con)
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='host-current'", (json.dumps(context),))
    with pytest.raises(CommandError):
        submit('host-foreign', '回测一次。', actor='other')
    follow_contract = contract

    # A copied approval context has the source scope binding, not its own.
    # The following question must still find that approval predecessor even
    # though approval contributes no user document.
    from nerya.core import jsonl
    from nerya.approval_service import ApprovalService
    from nerya.agent.interactions import create_interaction, respond
    from nerya.db.repositories import AgentSessionRepository
    import time
    monkeypatch.setattr(manager, 'kick', lambda sid: None)
    with manager.store.transaction() as con:
        current = con.execute("SELECT * FROM agent_commands WHERE command_id='host-current'").fetchone()
        con.execute("UPDATE agent_commands SET state='awaiting_approval' WHERE command_id='host-current'")
    record = {'approval_id': 'host-tail-approval', 'state': 'pending', 'kind': 'tool_permission_batch',
        'turn_id': current['turn_id'], 'session_id': sid, 'requester_actor_id': 'owner',
        'requester_session_id': sid, 'expires_at': time.time() + 60,
        'items': [{'tool': {'name': 'fixture_read'}}]}
    jsonl.append(config.paths.approvals_pending, record)
    approved = ApprovalService(config).move(record['approval_id'], state='approved', resolver_actor_id='owner')
    approval = manager.resolve_approval(approved, start=False)['command']
    config.data['runtime'].update(command_id=approval['command_id'], command_turn_id=approval['turn_id'])
    assert scope.current_contract(config)['constraints'] == follow_contract['constraints']
    question = create_interaction(config, sid=sid, tid=approval['turn_id'], call_id='host-question',
                                  kind='question', payload={'title': 'Which report?'})
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='awaiting_input' WHERE command_id=?", (approval['command_id'],))
        AgentSessionRepository(con).save_turn_checkpoint(session_id=sid, turn_id=approval['turn_id'],
                                                        checkpoint={'resumable': True})
    answer = respond(manager, {'session_id': sid, 'interaction_id': question['interaction_id'],
        'response_id': 'host-answer', 'expected_revision': 1, '_auth_actor_id': 'owner',
        '_auth_scopes': ['execute:strategy'], 'action': 'answer', 'text': 'The saved report'})['command']
    config.data['runtime'].update(command_id=answer['command_id'], command_turn_id=answer['turn_id'])
    assert scope.current_contract(config)['constraints'] == follow_contract['constraints']
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE session_id=?", (sid,))
    submit('host-after-question', '核对原回放结果。')
    after_question = TailGateway(choose)
    final = scope.ensure_contract(config, gateway=after_question)
    assert len(after_question.calls) == 1 and len(final['binding']['documents']) == 18
    assert final['constraints'] == follow_contract['constraints']
    assert scope.current_contract(config) == final


def test_standalone_task_continuation_keeps_original_before_candidate_binding(env):
    from contextlib import closing
    from nerya.db.sqlite import connect
    from test_request_scope import admit

    config = env
    identity = {'source': 'user_chat', 'extra': {'conversation_kind': 'task'}}
    original = '创建一个 ETH/USDT 脚本驱动 Agent 趋势策略，回测最近7天，不启用交易。'
    admit(config, cid='task-original', sid='direct-task', text=original, **identity)
    with closing(connect(config.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='task-original'")
    follow = '回放授权已续期。继续原任务，创建并回测，不启用交易。'
    admit(config, cid='task-continue', sid='direct-task', text=follow,
          created=1792900600, position=2, **identity)
    gateway = FakeGateway(lambda docs: spans_output(docs[0], base='ETH', quote='USDT', window_days='7天'))
    contract = scope.ensure_contract(config, gateway=gateway)
    assert [d['text'] for d in contract['binding']['documents']] == [original, follow]
    assert contract['constraints']['window_days'] == 7
    assert len(gateway.calls) == 2  # Both extractions receive the admitted original.

    with closing(connect(config.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='task-continue'")
    admit(config, cid='task-next', sid='direct-task', created=1792900700, position=3,
          text='继续同一候选和原定7天区间，使用已支持的 drpc 历史数据源完成回测，不启用交易。',
          **identity)
    cached = FakeGateway(lambda docs: pytest.fail('Verified continuation must reuse admitted scope'))
    latest = scope.ensure_contract(config, gateway=cached)
    assert latest['constraints'] == contract['constraints']
    assert latest['binding']['original_command_id'] == 'task-original'
    assert len(latest['binding']['documents']) == 3
    assert scope.current_contract(config) == latest


def test_standalone_current_candidate_uses_unique_durable_scope_anchor(tmp_path, monkeypatch):
    from contextlib import closing
    from copy import deepcopy
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.db.sqlite import connect
    from test_request_scope import admit

    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model',
                                            'allowed_tasks': ['agent.loop']}}
    config.data['agent']['native']['tier'] = 'medium'
    identity = {'source': 'user_chat', 'extra': {'conversation_kind': 'task'}}
    text = '创建 ETH/USDT 策略，回测最近7天，不启用交易。'
    admit(config, cid='original', text=text, **identity)
    prior = scope.ensure_contract(config, gateway=FakeGateway(
        lambda docs: spans_output(docs[0], base='ETH', quote='USDT', window_days='7天')))
    ref = 'strategy:fixture:proposal:prp_fixture'
    path = 'evolution/proposals/prp_fixture/after/strategies/fixture'
    delivery = {'artifacts': [{'artifact_id': 'candidate', 'ref': path, 'revision': 'a' * 64}],
                'checks': [{'check_id': 'source_binding', 'producer_owned': True,
                            'status': 'passed', 'artifact_id': ref,
                            'evidence_ref': path, 'revision': 'a' * 64}]}
    with closing(connect(config.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='interrupted',delivery_json=? WHERE command_id='original'",
                    (json.dumps(delivery),))
    followup = '继续当前候选和原定7天窗口，不启用交易。主机已修复请求范围校验，请重新验证后继续回测并读取报告。'
    admit(config, cid='follow', text=followup, created=1792900600, position=2, **identity)
    gateway = FakeGateway(lambda docs: pytest.fail('Anchored unchanged scope must not be re-extracted'))
    contract = scope.ensure_contract(config, gateway=gateway)
    assert contract['constraints'] == prior['constraints']
    assert contract['binding']['original_command_id'] == 'original'
    assert [d['text'] for d in contract['binding']['documents']] == [text, followup]
    assert scope.current_contract(config) == contract
    assert not gateway.calls

    with closing(connect(config.paths.db)) as con, con:
        request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id='follow'").fetchone()[0])
        for changed in ({'conversation_kind': 'chat'}, {'payload': {'text': '创建一个新的任务。'}},
                        {'_auth_actor_id': 'other'}):
            con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='follow'",
                        (json.dumps({**request, **changed}),))
            try:
                assert len(scope._snapshot(config, connection=con)['binding']['documents']) == 1
            except scope.ScopeError:
                assert '_auth_actor_id' in changed
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='follow'", (json.dumps(request),))
        other_path = 'evolution/proposals/prp_other/after/strategies/other'
        delivery['artifacts'].append({'artifact_id': 'candidate', 'ref': other_path, 'revision': 'b' * 64})
        delivery['checks'].append({**delivery['checks'][0], 'artifact_id': 'strategy:other:proposal:prp_other',
                                   'evidence_ref': other_path, 'revision': 'b' * 64})
        con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id='original'", (json.dumps(delivery),))
        with pytest.raises(scope.ScopeError):
            scope._snapshot(config, connection=con)


@pytest.fixture
def task_chat(real_assistant_child, monkeypatch):
    env, config, receipt, child, saved = real_assistant_child
    assert env.manager.store.finish(receipt['command_id'], receipt['task_session_id'],
                                    'scope-child-worker', 'succeeded', {})
    # Exercise the real route and admission, but never start a worker.
    monkeypatch.setattr(routes_conversation_commands, 'runtime', lambda *_: env.manager)
    submit = next(handler for method, path, handler in
                  routes_conversation_commands.command_routes(env.manager.execute)
                  if method == 'POST' and path == '/agent/commands')
    return SimpleNamespace(env=env, config=config, receipt=receipt, child=child,
                           saved=saved, submit=submit)


def send(task, *, cid='direct-followup', text='继续完成，不启用交易。',
         actor='owner', scopes=('write:chat', 'execute:strategy'), raw=None, route=True):
    auth = AuthResult(ok=True, actor=actor, scope='write:chat', scopes=frozenset(scopes))
    if route:
        auth = authorize_route(task.config, auth, method='POST', path='/agent/commands')
    assert auth.ok
    payload = _stamp_trusted_auth({
        'command_id': cid, 'session_id': task.receipt['task_session_id'],
        'command_type': 'send',
        'request': {'source': 'user_chat', 'kind': 'user.chat',
                    'payload': {'text': text, 'channel': 'dashboard'}, **(raw or {})},
    }, auth)
    accepted = (task.submit(SimpleNamespace(config=task.config), payload) if route
                else task.env.manager.submit(payload, start=False))
    assert accepted['ok'], accepted
    row = task.env.manager.store.claim(task.receipt['task_session_id'], 'direct-worker')
    assert row and row['command_id'] == cid
    task.config.data['runtime'].update(command_id=cid, command_session_id=row['session_id'],
                                     command_turn_id=row['turn_id'], financial_actor_id=actor)
    return row


@pytest.mark.parametrize('cached', [False, True])
def test_route_admitted_direct_followup_preserves_original_scope(task_chat, cached):
    task = task_chat
    original_id = task.receipt['command_id']
    original = None
    if cached:
        # Reuse the original running row only to seed its existing contract.
        with task.env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='running' WHERE command_id=?", (original_id,))
        original = scope.ensure_contract(task.config, gateway=FakeGateway(original_output))
        with task.env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (original_id,))
    row = send(task)
    admitted = json.loads(row['request_json'])
    assert row['kind'] == 'send' and admitted['source'] == 'user_chat'
    assert admitted['_auth_actor_id'] == row['actor_id'] == 'owner'
    assert admitted['_auth_scopes'] == ['execute:strategy', 'write:chat']
    assert 'assistant_request_binding' not in admitted
    assert 'assistant_request_binding' not in admitted['payload']
    gateway = FakeGateway(original_output)
    contract = scope.ensure_contract(task.config, gateway=gateway)
    prefix = scope.current_contract(task.config, command_id=original_id)
    assert original is None or prefix == original
    assert contract['constraints'] == prefix['constraints']
    assert contract['constraints']['resources'] == [{'base': 'ETH', 'quote': 'USDT'}]
    assert contract['constraints']['window_days'] == 30
    assert contract['revision'] == 1
    assert contract['binding']['original_command_id'] == original_id
    docs = contract['binding']['documents']
    assert docs[:1] == prefix['binding']['documents']
    assert [doc['command_id'] for doc in docs] == [original_id, row['command_id']]
    with task.env.manager.store.transaction() as con:
        parent = con.execute('SELECT created_at FROM agent_commands WHERE command_id=?',
                             (task.saved['assistant_request_binding']['command_id'],)).fetchone()
    assert docs[0]['created_at'] == parent['created_at']
    assert docs[1]['created_at'] == row['created_at']
    assert contract['constraints']['window_anchor'] == parent['created_at']
    assert scope.current_contract(task.config) == contract
    assert len(gateway.calls) == (1 if cached else 2)


def test_direct_same_candidate_followup_reuses_scope_without_model_extraction(task_chat):
    task = task_chat
    first_row = send(task, cid='same-candidate-first', text='创建 ETH/USDT 策略，回测最近30天，不启用交易。')
    first = first_row['command_id']
    scope.ensure_contract(task.config, gateway=FakeGateway(original_output))
    finish_direct(task, first_row)
    row = send(task, cid='same-candidate-followup',
               text='继续同一候选 prp_fixture，重新验证并回测最近7天；不要创建新候选、不要改市场、不要启用交易。')
    gateway = FakeGateway(original_output)
    contract = scope.ensure_contract(task.config, gateway=gateway)
    assert contract['binding']['original_command_id'] == task.receipt['command_id']
    assert [doc['command_id'] for doc in contract['binding']['documents']] == [
        task.receipt['command_id'], first, row['command_id']]
    assert not gateway.calls


@pytest.mark.parametrize('cached', [False, True])
def test_task_context_scope_free_continue_reuses_verified_prefix_without_model(env, monkeypatch, cached):
    from nerya.agent.assistants import AssistantStore
    from nerya.agent.command_runtime import CommandRuntime

    config, sid = env, 'scope-free-host-task'
    AssistantStore(config.paths).bind(sid, 'main', kind='task')
    manager = CommandRuntime(config, lambda *a: pytest.fail('no runtime execution'))
    monkeypatch.setattr(manager, 'accepted_model', lambda request: None)

    def submit(cid, text):
        receipt = manager.submit({
            'command_id': cid, 'session_id': sid, '_auth_actor_id': 'owner',
            '_auth_scopes': ['execute:strategy'],
            'request': {'source': 'user_chat', 'kind': 'user.chat',
                        'conversation_kind': 'task', 'payload': {'text': text}},
        }, start=False)
        row = receipt['command']
        config.data['runtime'].update(command_id=cid, command_session_id=sid,
                                      command_turn_id=row['turn_id'], financial_actor_id='owner')
        with manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='running' WHERE command_id=?", (cid,))
        return row

    root = submit('scope-free-root', '创建 ETH/USDT 策略，回测最近30天，不交易。')
    original = scope.ensure_contract(config, gateway=FakeGateway(original_output)) if cached else None
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (root['command_id'],))
    follow = submit('scope-free-follow', '继续原任务，不启用交易。')
    snapshot = scope._snapshot(config, active=True)
    assert snapshot['task_context'] is True
    assert snapshot['context']['task_context_binding']['command_ids'] == [root['command_id']]
    assert snapshot['task_prefix'] == original

    def tail(docs):
        return original_output(docs)

    gateway = FakeGateway(lambda docs: pytest.fail('cached scope-free continuation must not call model')
                          if cached else tail(docs))
    contract = scope.ensure_contract(config, gateway=gateway)
    if original is not None:
        assert contract['constraints'] == original['constraints']
    assert contract['constraints']['resources'] == [{'base': 'ETH', 'quote': 'USDT'}]
    assert contract['constraints']['window_days'] == 30
    assert contract['binding'] == snapshot['binding']
    assert contract['input_hash'] == snapshot['input_hash']
    assert [doc['command_id'] for doc in contract['binding']['documents']] == [root['command_id'], follow['command_id']]
    assert len(gateway.calls) == (0 if cached else 1)
    assert scope.current_contract(config) == contract
    assert scope.ensure_contract(config, gateway=FakeGateway(lambda docs: pytest.fail('cached'))) == contract
    with manager.store.transaction() as con:
        saved = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                                       (follow['command_id'],)).fetchone()[0])
        assert saved['request_scope'] == contract
        if not cached:
            root_context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                                                  (root['command_id'],)).fetchone()[0])
            assert 'request_scope' not in root_context
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (follow['command_id'],))

    drift = submit('scope-free-drift', '继续原任务，BTC/USDC，最近7天，不启用交易。')
    assert scope._snapshot(config, active=True)['task_context'] is True
    with manager.store.transaction() as con:
        before = tuple(con.execute('SELECT context_json,revision FROM agent_commands WHERE command_id=?',
                                   (drift['command_id'],)).fetchone())
    rejected = FakeGateway(lambda docs: spans_output(
        docs[-1], base='BTC', quote='USDC', window_days='7天'))
    with pytest.raises(scope.ScopeError) as failure:
        scope.ensure_contract(config, gateway=rejected)
    assert failure.value.diagnostic_code == 'incomplete_scope_evidence'
    assert failure.value.diagnostic_metadata['reason'] == 'uncovered_pair_asset'
    assert len(rejected.calls) == 1
    with manager.store.transaction() as con:
        assert tuple(con.execute('SELECT context_json,revision FROM agent_commands WHERE command_id=?',
                                 (drift['command_id'],)).fetchone()) == before
        con.execute("UPDATE agent_commands SET state='failed' WHERE command_id=?", (drift['command_id'],))
    # A later scope-free message must not skip an unverified conflicting tail.
    submit('scope-free-after-drift', '继续原任务，不启用交易。')
    def conflicting_tail(docs):
        return spans_output(docs[-2], base='BTC', quote='USDC', window_days='7天')
    rejected_tail = FakeGateway(conflicting_tail)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(config, gateway=rejected_tail)
    assert len(rejected_tail.calls) == 1
    assert scope._snapshot(config)['context'].get('request_scope') is None


def finish_direct(task, row):
    assert task.env.manager.store.finish(row['command_id'], row['session_id'],
                                        'direct-worker', 'succeeded', {})


def edit_request(task, cid, change):
    # Deliberately corrupt persisted records to exercise fail-closed recovery.
    with task.env.manager.store.transaction() as con:
        request = json.loads(con.execute('SELECT request_json FROM agent_commands WHERE command_id=?',
                                        (cid,)).fetchone()[0])
        change(request)
        con.execute('UPDATE agent_commands SET request_json=? WHERE command_id=?',
                    (json.dumps(request), cid))


def assert_invalid(task):
    gateway = FakeGateway(original_output)
    with pytest.raises(scope.ScopeError) as failed:
        scope.ensure_contract(task.config, gateway=gateway)
    assert failed.value.diagnostic_code == 'invalid_binding'
    assert not gateway.calls
    with pytest.raises(scope.ScopeError):
        scope.current_contract(task.config)


def test_direct_prefixes_and_later_bound_parent_followup_keep_original_scope(task_chat):
    task = task_chat
    first = send(task, cid='direct-one', scopes=('api:all',), raw={'kind': 'user.chat'})
    first_contract = scope.ensure_contract(task.config, gateway=FakeGateway(original_output))
    finish_direct(task, first)
    second = send(task, cid='direct-two', text='请汇报进度。')
    finish_direct(task, second)
    text = '继续完成，不启用交易。'
    cfg, parent = parent_request(task.env, operation='task_follow_up',
        target=task.receipt['task_id'], cid='parent-followup', text=text,
        scopes=('execute:strategy',))
    follow = followup_task(task.env, cfg, parent, text=text)
    row = task.env.manager.store.claim(follow['task_session_id'], 'parent-followup-worker')
    assert row and row['command_id'] == follow['command_id']
    task.config.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])
    gateway = FakeGateway(original_output)
    contract = scope.ensure_contract(task.config, gateway=gateway)
    ids = [task.receipt['command_id'], first['command_id'], second['command_id'], row['command_id']]
    assert contract['constraints'] == first_contract['constraints']
    assert contract['revision'] == 1
    for index, cid in enumerate(ids):
        prefix = scope.current_contract(task.config, command_id=cid)
        assert prefix['constraints'] == contract['constraints']
        assert prefix['binding']['documents'] == contract['binding']['documents'][:index + 1]
    assert scope.current_contract(task.config, command_id=first['command_id']) == first_contract
    assert len(gateway.calls) == 2


@pytest.mark.parametrize('location', ['top', 'nested', 'both'])
@pytest.mark.parametrize('binding', [None, {}, 'original'])
def test_direct_chat_rejects_any_durable_binding_claim(task_chat, location, binding):
    task = task_chat
    row = send(task)
    if binding == 'original':
        binding = task.saved['assistant_request_binding']
    def forge(request):
        if location in {'top', 'both'}:
            request['assistant_request_binding'] = binding
        if location in {'nested', 'both'}:
            request['payload']['assistant_request_binding'] = binding
    edit_request(task, row['command_id'], forge)
    assert_invalid(task)


@pytest.mark.parametrize('tamper', [None, 'frozen_window', 'session', 'actor', 'unauthorized_history',
                                  'lost_amendment', 'cas'])
def test_long_aave_mixed_history_classifies_current_scope_once(assistant_env, monkeypatch, tamper):  # noqa: F811
    from copy import deepcopy
    from nerya.agent.file_state import FileStateCache
    from nerya.core.config import Config
    from nerya.evolution.patch_proposal import create_proposal
    from nerya.tools.native.bootstrap import NativeToolDeps, register_native_tools
    from nerya.tools.native.skill import SkillIndex
    from nerya.tools.native.task import TaskState
    from nerya.tools.registry import ToolRegistry
    from test_proposal_file_task_binding import mutate, record_draft
    from test_request_scope import admit, create_task, spans_output
    from nerya.strategies import request_scope as scope

    monkeypatch.setattr('nerya.llm.gateway.LLMGateway.call_messages',
                        lambda *a, **kw: pytest.fail('REAL_MODEL_FORBIDDEN'))
    env = assistant_env
    env.config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model',
                                               'allowed_tasks': ['agent.loop']}}
    env.config.data['agent']['native']['tier'] = 'medium'
    text = '创建一个 Ethereum Aave V3 USDC 收益率策略，回测最近30天并生成报告，不交易。'
    admitted_at = 1793966225.2190259  # Nov 6 admission, before Nov 7 continuation.
    with monkeypatch.context() as admission_clock:
        admission_clock.setattr('nerya.agent.command_store.time.time', lambda: admitted_at)
        config, parent = parent_request(env, text=text, scopes=('execute:strategy',))
        task = create_task(env, config, parent, prompt=text)
    row = env.manager.store.claim(task['task_session_id'], 'aave-worker')
    config = Config(config.paths, deepcopy(config.data))
    config.data['runtime'].update(command_id=row['command_id'], command_session_id=row['session_id'],
                                 command_turn_id=row['turn_id'])
    def evidence(docs):
        result = spans_output(docs[0], protocol='Aave V3', chain='Ethereum',
                              asset='USDC', window_days='最近30天')
        amendment = next((doc for doc in docs if doc['command_id'] == 'aave-followup-17'), None)
        if amendment and tamper != 'frozen_window':
            result['fields']['window_days'] = spans_output(amendment, window_days='7天')['fields']['window_days']
            result['amendments'] = [{'command_id': amendment['command_id'], 'start': 0,
                                    'end': len(amendment['text']), 'text': amendment['text']}]
        return result

    def classify(docs):
        assert gateway.calls[-1]['tools'][0]['input_schema'] == scope.SCHEMA
        return evidence(docs[:17] if tamper == 'lost_amendment' and len(docs) == 39 else docs)

    gateway = FakeGateway(classify)
    original = scope.ensure_contract(config, gateway=gateway)
    assert scope._iso(original['constraints']['start_utc']) == '2026-10-07T00:00:00Z'
    assert scope._iso(original['constraints']['end_utc']) == '2026-11-06T00:00:00Z'
    deps = NativeToolDeps(workspace_root=config.paths.root, file_state=FileStateCache(),
        task_state=TaskState(), skill_index=SkillIndex([]), config=config,
        paths=config.paths, active_session_id=row['session_id'], active_actor_id='owner')
    registry = ToolRegistry()
    register_native_tools(registry, deps)
    proposal = create_proposal(config.paths, kind='strategy_package_proposal', summary='Owned Aave draft',
        extra_files={'after/strategies/observer/main.py': 'MARKET = "old"'})
    path = proposal.path / 'after/strategies/observer/main.py'
    tools = config, deps, registry, proposal, path

    # Draft on command three, then uncached operational research, an explicit
    # amendment, and more research before the current continuation at 39.
    for index in range(1, 39):
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?",
                        (config.get('runtime.command_id'),))
        text = ('继续原任务，完成模拟和报告，不交易。' if index <= 2 or index == 38 else
                '只检查已保存的储备采样，不下载、不重跑、不交易。' if index % 2 else
                '核对已保存采样的实际区块时间，修正证据文件格式，不查新数据、不重跑、不交易。')
        if index == 17 and tamper != 'frozen_window':
            text = '将同一候选的回测窗口改为7天，不交易。'
        admit(config, cid=f'aave-followup-{index}', sid=task['task_session_id'],
              created=1794044447 + index, position=-index, source='user_chat',
              text=text,
              extra={'kind': 'user.chat', 'conversation_kind': 'task',
                     'assistant_id': task['assistant_id']})
        if index == 2 or (index == 17 and tamper == 'lost_amendment'):
            # Existing producer contracts may still have legacy full chains.
            # Seed those as input; the current producer must not recreate them.
            snapshot = scope._snapshot(config)
            legacy = scope._chain([evidence(snapshot['binding']['documents'][:i + 1])
                                   for i in range(len(snapshot['binding']['documents']))], snapshot)
            with env.manager.store.transaction() as con:
                context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                    (config.get('runtime.command_id'),)).fetchone()[0])
                context['request_scope'] = legacy
                con.execute('UPDATE agent_commands SET context_json=?,revision=revision+1 WHERE command_id=?',
                            (json.dumps(context), config.get('runtime.command_id')))
            assert scope.current_contract(config) == legacy
        if index == 2:
            record_draft(tools, cid=config.get('runtime.command_id'), session=task['task_session_id'],
                         turn=config.get('runtime.command_turn_id'))
            gateway.calls.clear()
    admission_tamper = tamper in {'session', 'actor', 'unauthorized_history'}
    if admission_tamper:
        with env.manager.store.transaction() as con:
            if tamper in {'session', 'actor'}:
                con.execute(f"UPDATE agent_commands SET {tamper}_id='foreign' WHERE command_id=?",
                            (task['command_id'],))
            else:
                saved = json.loads(con.execute(
                    "SELECT request_json FROM agent_commands WHERE command_id='aave-followup-20'").fetchone()[0])
                saved['_auth_scopes'] = ['read:config']
                con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='aave-followup-20'",
                            (json.dumps(saved),))
    result = mutate(tools, 'edit_file')
    assert result.is_error is admission_tamper, result.text()
    assert path.read_text() == ('MARKET = "old"' if admission_tamper else 'MARKET = "new"')
    with env.manager.store.transaction() as con:
        before = [tuple(r) for r in con.execute(
            'SELECT command_id,revision,context_json FROM agent_commands WHERE command_id!=? ORDER BY command_id',
            (config.get('runtime.command_id'),))]
    if tamper == 'cas':
        def change_current_request():
            with env.manager.store.transaction() as con:
                request = json.loads(con.execute('SELECT request_json FROM agent_commands WHERE command_id=?',
                    (config.get('runtime.command_id'),)).fetchone()[0])
                request['payload']['text'] += ' changed'
                con.execute('UPDATE agent_commands SET request_json=?,revision=revision+1 WHERE command_id=?',
                            (json.dumps(request), config.get('runtime.command_id')))
        gateway.effect = change_current_request
    if tamper not in (None, 'frozen_window'):
        with pytest.raises(scope.ScopeError) as failure:
            scope.ensure_contract(config, gateway=gateway)
        if tamper == 'cas':
            assert failure.value.diagnostic_code == 'persistence_conflict'
        with env.manager.store.transaction() as con:
            context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                (config.get('runtime.command_id'),)).fetchone()[0])
            assert 'request_scope' not in context
    else:
        snapshot = scope._snapshot(config)
        resumed = scope.ensure_contract(config, gateway=gateway)
        assert len(gateway.calls) <= 1
        assert 'extractions' not in resumed
        assert resumed['binding'] == snapshot['binding']
        assert resumed['evidence'] == resumed['normalized_evidence']
        assert len(json.loads(gateway.calls[0]['messages'][0]['content'])) == 39
        assert gateway.calls[0]['max_tokens'] == 16384
        if tamper is None:
            print(f"39-command classifier response: {len(scope._json(evidence(snapshot['binding']['documents'])).encode())} bytes; one scope")
        assert resumed['constraints']['resources'] == original['constraints']['resources']
        if tamper == 'frozen_window':
            assert resumed['constraints'] == original['constraints']
        else:
            assert resumed['constraints']['window_days'] == 7
            assert resumed['constraints']['window_anchor'] == 1794044447 + 17
        assert resumed['binding']['original_command_id'] == task['command_id']
        assert len(resumed['binding']['documents']) == 39
        assert scope.current_contract(config) == resumed
    with env.manager.store.transaction() as con:
        assert [tuple(r) for r in con.execute(
            'SELECT command_id,revision,context_json FROM agent_commands WHERE command_id!=? ORDER BY command_id',
            (config.get('runtime.command_id'),))] == before
    if tamper in (None, 'frozen_window'):
        with env.manager.store.transaction() as con:
            context = con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                                  ('aave-followup-2',)).fetchone()[0]
            damaged = json.loads(context)
            damaged['request_scope']['request_scope_hash'] = '0' * 64
            con.execute('UPDATE agent_commands SET context_json=? WHERE command_id=?',
                        (json.dumps(damaged), 'aave-followup-2'))
        with pytest.raises(scope.ScopeError):
            scope.ensure_contract(config, gateway=gateway)
        with env.manager.store.transaction() as con:
            con.execute('UPDATE agent_commands SET context_json=? WHERE command_id=?',
                        (context, 'aave-followup-2'))
    assert len(gateway.calls) == (0 if admission_tamper else 1)


def test_route_preserves_nested_binding_claim_for_rejection(task_chat):
    send(task_chat, raw={'payload': {'text': '继续完成。',
        'assistant_request_binding': task_chat.saved['assistant_request_binding']}})
    assert_invalid(task_chat)


@pytest.mark.parametrize('source', ['assistant_task', 'assistant_task_message', 'goal_continue',
                                  'other', None])
@pytest.mark.parametrize('nested', [False, True])
def test_delegated_and_unknown_sources_cannot_use_direct_chat_authority(task_chat, source, nested):
    task = task_chat
    row = send(task)
    def forge(request):
        request['source'] = source
        if nested:
            request['payload']['assistant_request_binding'] = task.saved['assistant_request_binding']
    edit_request(task, row['command_id'], forge)
    assert_invalid(task)


def test_task_message_provenance_cannot_be_relabelled_as_direct_chat(task_chat):
    task = task_chat
    send(task, raw={'payload': {'text': '继续完成。', 'task_message': {
        'sender_session_id': task.env.sid, 'target_task_id': task.receipt['task_id'],
        'request_id': 'delegated-followup',
    }}})
    assert_invalid(task)


@pytest.mark.parametrize('scopes', [None, [], 'execute:strategy', {'execute:strategy': True},
    ['write:chat'], ['execute:strategy', 'write:chat', False]])
@pytest.mark.parametrize('prefix', [False, True])
def test_direct_chat_requires_typed_execution_grants_even_in_saved_prefix(task_chat, scopes, prefix):
    task = task_chat
    row = send(task)
    if prefix:
        finish_direct(task, row)
        send(task, cid='later-direct')
    def forge(request):
        request['_auth_scopes'] = scopes
        request['_auth_scope'] = 'api:all'
        request['payload']['_auth_scopes'] = ['api:all']
    edit_request(task, row['command_id'], forge)
    assert_invalid(task)


@pytest.mark.parametrize('state', ['interrupted', 'failed'])
@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('grants', [('execute:strategy',), ('api:all',)])
def test_stopped_direct_prefix_retains_scope_for_later_direct_followup(task_chat, state, cached, grants):
    task = task_chat
    # CommandRuntime preserves the admitted grants; recovery must not impose
    # an additional write:chat grant on an already authenticated command.
    first = send(task, cid='stopped-direct', scopes=grants, route=False)
    original = scope.ensure_contract(task.config, gateway=FakeGateway(original_output)) if cached else None
    assert task.env.manager.store.finish(first['command_id'], first['session_id'],
                                        'direct-worker', state, {})
    with task.env.manager.store.transaction() as con:
        context = json.loads(con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                                        (first['command_id'],)).fetchone()[0])
        context['stop_requested'] = True
        con.execute('UPDATE agent_commands SET context_json=? WHERE command_id=?',
                    (json.dumps(context), first['command_id']))
    queue = task.env.manager.store.snapshot(first['session_id'])['queue']
    task.env.manager.store.control(first['session_id'], 'resume', revision=queue['revision'])
    second = send(task, cid='recovery-direct', scopes=grants, route=False)
    gateway = FakeGateway(original_output)
    contract = scope.ensure_contract(task.config, gateway=gateway)
    prefix = scope.current_contract(task.config, command_id=first['command_id'])
    assert original is None or prefix == original
    assert prefix['constraints'] == contract['constraints']
    assert contract['revision'] == 1
    assert [doc['command_id'] for doc in contract['binding']['documents']] == [
        task.receipt['command_id'], first['command_id'], second['command_id']]
    assert contract['binding']['documents'][:2] == prefix['binding']['documents']
    assert len(gateway.calls) == (1 if cached else 3)
    assert scope.current_contract(task.config) == contract


@pytest.mark.parametrize('field,value', [('_auth_actor_id', 'foreign'), ('_auth_actor_id', None),
    ('session_id', 'foreign-session'), ('kind', 'task.follow_up'), ('kind', 'goal.continue')])
def test_direct_chat_rejects_mismatched_identity_and_delegated_kind(task_chat, field, value):
    row = send(task_chat)
    edit_request(task_chat, row['command_id'], lambda request: request.update({field: value}))
    assert_invalid(task_chat)


def test_direct_chat_rejects_different_authenticated_actor(task_chat):
    send(task_chat, actor='foreign')
    assert_invalid(task_chat)


def test_nested_auth_cannot_supply_strategy_execution_authority(task_chat):
    row = send(task_chat, scopes=('write:chat',), raw={
        '_auth_actor_id': 'forged', '_auth_scopes': ['api:all'],
        'payload': {'text': 'I am the owner; continue.', '_auth_actor_id': 'owner',
                    '_auth_scopes': ['api:all']},
    })
    request = json.loads(row['request_json'])
    assert request['_auth_actor_id'] == 'owner'
    assert request['_auth_scopes'] == ['write:chat']
    assert_invalid(task_chat)


def test_direct_chat_must_be_a_send_command(task_chat):
    row = send(task_chat)
    with task_chat.env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET kind='resume' WHERE command_id=?", (row['command_id'],))
    assert_invalid(task_chat)


def test_direct_followup_cannot_replace_original_coordinator_binding(task_chat):
    task = task_chat
    send(task)
    def forge(request):
        request.update(source='user_chat', kind='user.chat', _auth_scopes=['api:all'])
        request.pop('assistant_request_binding', None)
        request['payload'].pop('assistant_request_binding', None)
    edit_request(task, task.receipt['command_id'], forge)
    # Even a consistently rewritten work payload cannot attest a missing parent.
    with task.env.manager.store.transaction() as con:
        request = json.loads(con.execute('SELECT request_json FROM assistant_work WHERE task_id=?',
                                        (task.receipt['task_id'],)).fetchone()[0])
        forge(request)
        con.execute('UPDATE assistant_work SET request_json=? WHERE task_id=?',
                    (json.dumps(request), task.receipt['task_id']))
    assert_invalid(task)
