"""Proposal writes require durable host association, never model path claims."""
from copy import deepcopy
from contextlib import closing
import json

import pytest

from nerya.agent.file_state import FileStateCache
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.db.sqlite import connect
from nerya.tools.verification import verification_receipt
from nerya.tools.native.bootstrap import NativeToolDeps, register_native_tools
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.task import TaskState
from nerya.tools.native.evolve import evolve_proposals_handler
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall, ToolErrorKind
from test_request_scope import (
    admit, FakeGateway,
    parent_request, followup_task,
)
from test_request_scope import fake_execution, real_assistant_child, assistant_env  # noqa: F401
from nerya.strategies.request_scope import ensure_contract
from nerya.agent.tool_projection import project_tool_results

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def isolated_db_mode(monkeypatch):
    # Set before assistant_env creates its DB; its child fixture uses delete.
    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')


@pytest.fixture
def bound_tools(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('network/model access forbidden')
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setattr('nerya.llm.gateway.LLMGateway.call_messages', forbidden)
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    admit(config)
    deps = NativeToolDeps(workspace_root=tmp_path, file_state=FileStateCache(),
        task_state=TaskState(), skill_index=SkillIndex([]), config=config,
        paths=config.paths, active_session_id='scope-session', active_actor_id='owner')
    registry = ToolRegistry()
    register_native_tools(registry, deps)
    proposal = create_proposal(config.paths, kind='strategy_package_proposal',
        summary='Same title as current request is not authority',
        extra_files={'after/strategies/observer/main.py': 'MARKET = "old"'})
    path = proposal.path / 'after/strategies/observer/main.py'
    return config, deps, registry, proposal, path


def mutate(env, name, *, path=None, **extra):
    config, deps, registry, proposal, candidate = env
    path = path or candidate
    if path.exists():
        result = registry.get('read_file').handler(ToolCall(name='read_file', arguments={'path': str(path)}))
        assert not result.is_error
    arguments = ({'old_string': 'old', 'new_string': 'new'} if name == 'edit_file'
                 else {'contents': 'MARKET = "new"'})
    return registry.get(name).handler(ToolCall(name=name, arguments={
        'path': str(path), **arguments, **extra}))


@pytest.mark.parametrize('name', ['edit_file', 'write_file'])
def test_unrelated_historical_candidate_is_denied_before_write(bound_tools, name):
    path = bound_tools[-1]
    before = path.read_bytes()
    result = mutate(bound_tools, name, allow_outside_conversation=True,
        outside_conversation_reason='User authorized this candidate',
        command_id='old-command', session_id='old-session', skip_binding=True)
    assert result.is_error, 'registered file tool mutated an unassociated proposal'
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert path.read_bytes() == before


def record_draft(env, *, cid='scope-1', session='scope-session', action='strategy_draft_proposal',
                 ok=True, turn=None, channel='verification'):
    config, deps, registry, proposal, path = env
    receipt = verification_receipt(artifact_id=f'strategy:observer:proposal:{proposal.id}',
        revision='original-draft-source', scope='Host producer snapshot',
        checks=[{'check_id': 'source_binding', 'status': 'passed',
                 'evidence_ref': path.parent.relative_to(config.paths.root).as_posix()}])
    event = {'kind': 'tool.complete', 'session_id': session, 'turn_id': turn or 'turn-' + cid,
             'action': action, 'skill_id': 'native', 'ok': ok, channel: receipt}
    with closing(connect(config.paths.db)) as con:
        con.execute('INSERT INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)',
                    (cid, 'draft-receipt-' + cid, json.dumps(event)))


def discover(config, **arguments):
    result = evolve_proposals_handler(ToolCall(name='evolve_proposals', arguments=arguments), config=config)
    assert not result.is_error, result.text()
    return result.content[0].data


@pytest.mark.parametrize('change', [{}, {'ok': False}, {'action': 'strategy_validate'},
                                   {'channel': 'result'}, {'turn': 'foreign-turn'}])
def test_discovery_requires_successful_host_draft_receipt(bound_tools, change):
    config, _, _, proposal, path = bound_tools
    if change:
        record_draft(bound_tools, **change)
    assert discover(config)['proposals'] == []
    for arguments in ({'proposal_id': proposal.id}, {'scope': 'global'}):
        data = discover(config, **arguments)
        ownership = data['proposals'][0]['ownership']
        assert ownership == {'same_task_producer': False, 'recoverable': False,
                             'diagnostic_code': 'producer_receipt_missing'}
    before = path.read_bytes()
    assert mutate(bound_tools, 'write_file').is_error
    assert path.read_bytes() == before


@pytest.mark.parametrize('followup', [False, True])
def test_discovery_recovers_same_task_before_limit(bound_tools, followup):
    config, _, _, proposal, _ = bound_tools
    record_draft(bound_tools)
    if followup:
        with closing(connect(config.paths.db)) as con:
            con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id='scope-1'")
        admit(config, cid='scope-followup', position=2)
    create_proposal(config.paths, kind='strategy_package_proposal', summary='newer unrelated',
                    extra_files={'after/strategies/foreign/main.py': 'VALUE = 0'})
    data = discover(config, limit=1)
    assert data['scope'] == 'task' and data['count'] == 1
    assert data['proposals'][0]['id'] == proposal.id
    assert data['proposals'][0]['ownership'] == {
        'same_task_producer': True, 'recoverable': True, 'diagnostic_code': 'same_task_producer_receipt'}
    assert not mutate(bound_tools, 'write_file').is_error


def test_discovery_same_parent_other_task_is_not_recoverable(real_assistant_child):
    env, config, receipt, _, _ = real_assistant_child
    proposal = create_proposal(config.paths, kind='strategy_package_proposal', summary='same title',
        extra_files={'after/strategies/observer/main.py': 'MARKET = "old"'})
    path = proposal.path / 'after/strategies/observer/main.py'
    record_draft((config, None, None, proposal, path), cid=receipt['command_id'],
                 session=receipt['task_session_id'], turn=config.get('runtime.command_turn_id'))
    cfg, parent = parent_request(env, cid='parent-other-task', scopes=('execute:strategy',))
    from test_assistant_receipts import create
    sibling = create(env, cfg, parent)
    row = env.manager.store.claim(sibling['task_session_id'], 'other-task-worker')
    assert row and sibling['task_session_id'] != receipt['task_session_id']
    config.data['runtime'].update(command_id=row['command_id'], command_session_id=row['session_id'],
                                 command_turn_id=row['turn_id'])
    assert discover(config)['proposals'] == []
    data = discover(config, proposal_id=proposal.id)
    assert data['found'] is True
    assert data['proposal']['ownership']['same_task_producer'] is False
    assert data['proposal']['ownership']['recoverable'] is False
    from nerya.tools.native.file_ops import _proposal_task_guard
    denied = _proposal_task_guard(ToolCall(name='write_file'),
        path=path.relative_to(config.paths.root).as_posix(), root=config.paths.root,
        config=config, task_session_id=None, task_actor_id=None)
    assert denied.error.detail['diagnostic_code'] == 'producer_receipt_missing'


def test_standalone_discovery_remains_global_without_claiming_ownership(bound_tools):
    config, _, _, proposal, _ = bound_tools
    config.data['runtime'].pop('command_id')
    data = discover(config)
    assert data['scope'] == 'global' and data['proposals'][0]['id'] == proposal.id
    assert data['proposals'][0]['ownership'] == {
        'same_task_producer': None, 'recoverable': False, 'diagnostic_code': 'no_command_context'}


@pytest.mark.parametrize('change', ['stopped', 'missing_db'])
def test_discovery_fails_closed_when_receipt_context_unavailable(bound_tools, change):
    config, _, _, proposal, _ = bound_tools
    record_draft(bound_tools)
    if change == 'stopped':
        with closing(connect(config.paths.db)) as con:
            con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='scope-1'",
                        (json.dumps({'stop_requested': True}),))
    else:
        config.paths.db.rename(config.paths.db.with_suffix('.saved'))
    assert discover(config)['proposals'] == []
    assert discover(config, proposal_id=proposal.id)['proposal']['ownership']['recoverable'] is False


@pytest.mark.parametrize('name', ['edit_file', 'write_file'])
@pytest.mark.parametrize('followup', [False, True])
def test_successful_own_draft_receipt_allows_current_and_prior_followup(bound_tools, name, followup):
    config, deps, registry, proposal, path = bound_tools
    record_draft(bound_tools)
    if followup:
        with closing(connect(config.paths.db)) as con:
            con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id='scope-1'")
        admit(config, cid='scope-followup', position=2, text='Continue the same draft.')
    # Source revision changes after each legitimate edit; original ownership
    # must survive without requiring a new validation receipt or duplicate draft.
    assert not mutate(bound_tools, name).is_error
    assert path.read_text() == 'MARKET = "new"'
    assert not mutate(bound_tools, 'write_file', contents='MARKET = "newer"').is_error
    assert path.read_text() == 'MARKET = "newer"'


@pytest.mark.parametrize('actor', ['local:loopback', 'local:anonymous', 'admin:password'])
def test_local_owner_memory_alias_preserves_exact_command_auth(bound_tools, actor):
    config, deps, _, _, path = bound_tools
    with closing(connect(config.paths.db)) as con:
        row = con.execute(
            "SELECT request_json FROM agent_commands WHERE command_id='scope-1'",
        ).fetchone()
        request = json.loads(row[0])
        request['_auth_actor_id'] = actor
        con.execute(
            "UPDATE agent_commands SET actor_id=?,request_json=? WHERE command_id='scope-1'",
            (actor, json.dumps(request)),
        )
    config.data['runtime']['financial_actor_id'] = actor
    deps.active_actor_id = 'default'
    record_draft(bound_tools)
    assert not mutate(bound_tools, 'write_file').is_error
    assert path.read_text() == 'MARKET = "new"'
    config.data['runtime']['financial_actor_id'] = 'foreign-owner'
    assert mutate(bound_tools, 'write_file').is_error
    assert path.read_text() == 'MARKET = "new"'


@pytest.mark.parametrize('change', [
    {'session': 'foreign-session'}, {'turn': 'foreign-turn'}, {'ok': False},
    {'action': 'strategy_validate'}, {'channel': 'result'},
])
def test_nonproducer_or_wrong_run_receipts_do_not_grant_ownership(bound_tools, change):
    record_draft(bound_tools, **change)
    before = bound_tools[-1].read_bytes()
    result = mutate(bound_tools, 'write_file')
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert bound_tools[-1].read_bytes() == before


@pytest.mark.parametrize('state', ['queued', 'unconfirmed'])
def test_nonterminal_prior_command_cannot_authorize_followup(bound_tools, state):
    config = bound_tools[0]
    record_draft(bound_tools)
    with closing(connect(config.paths.db)) as con:
        con.execute('UPDATE agent_commands SET state=? WHERE command_id=?', (state, 'scope-1'))
    admit(config, cid='scope-followup', position=2)
    assert mutate(bound_tools, 'write_file').is_error


@pytest.mark.parametrize('change', ['foreign_actor', 'foreign_session', 'foreign_deps_session',
                                  'foreign_deps_actor', 'wrong_turn', 'stopped', 'missing_command', 'missing_db'])
def test_invalid_current_context_denies_even_with_own_receipt(bound_tools, change):
    config, deps, _, _, path = bound_tools
    record_draft(bound_tools)
    if change == 'foreign_actor':
        config.data['runtime']['financial_actor_id'] = 'foreign'
    elif change == 'foreign_session':
        config.data['runtime']['command_session_id'] = 'foreign'
    elif change == 'foreign_deps_session':
        deps.active_session_id = 'foreign'
    elif change == 'foreign_deps_actor':
        deps.active_actor_id = 'foreign'
    elif change == 'wrong_turn':
        config.data['runtime']['command_turn_id'] = 'foreign'
    elif change == 'missing_command':
        config.data['runtime']['command_id'] = 'missing-command'
    elif change == 'missing_db':
        config.paths.db.rename(config.paths.db.with_suffix('.saved'))
    else:
        with closing(connect(config.paths.db)) as con:
            con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='scope-1'",
                        (json.dumps({'stop_requested': True}),))
    before = path.read_bytes()
    assert mutate(bound_tools, 'write_file').is_error
    assert path.read_bytes() == before


def test_new_foreign_file_denial_creates_no_parent_or_cache_entry(bound_tools):
    config, deps, _, proposal, _ = bound_tools
    target = proposal.path / 'after/strategies/observer/new/sub.py'
    before = sorted(p.relative_to(proposal.path) for p in proposal.path.rglob('*'))
    assert mutate(bound_tools, 'write_file', path=target).is_error
    assert not target.parent.exists()
    assert sorted(p.relative_to(proposal.path) for p in proposal.path.rglob('*')) == before


@pytest.mark.parametrize('session', [None, 'scope-session'])
def test_no_runtime_command_preserves_standalone_placement(bound_tools, session):
    config, deps, _, proposal, path = bound_tools
    config.data['runtime'].pop('command_id')
    deps.active_session_id = session
    assert not mutate(bound_tools, 'write_file').is_error
    target = path.parent / 'notes.md'
    assert not mutate(bound_tools, 'write_file', path=target).is_error
    assert target.exists()


def test_stream_events_do_not_consume_draft_receipt_budget(bound_tools):
    config = bound_tools[0]
    with closing(connect(config.paths.db)) as con:
        con.executemany('INSERT INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)',
            [('scope-1', f'stream-{i}', json.dumps({'kind': 'message.delta', 'text': 'token'}))
             for i in range(1100)])
    record_draft(bound_tools)
    assert not mutate(bound_tools, 'write_file').is_error


def test_prior_admission_uses_receipt_snapshot_without_nested_reader(bound_tools, monkeypatch):
    from nerya.tools.native import file_ops
    config = bound_tools[0]
    record_draft(bound_tools)
    with closing(connect(config.paths.db)) as con:
        con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id='scope-1'")
    admit(config, cid='scope-followup', position=2, text='Continue the existing candidate.')
    original = file_ops._proposal_task_commands
    observed = []

    def snapshot(config, connection):
        observed.append(connection)
        assert connection.in_transaction
        return original(config, connection)

    monkeypatch.setattr(file_ops, '_proposal_task_commands', snapshot)
    result = mutate(bound_tools, 'write_file')
    assert not result.is_error, result.text()
    assert observed and all(item is not None for item in observed)


@pytest.mark.parametrize('tamper', [None, 'resume_turn_id', '_auth_actor_id'])
def test_real_registered_draft_receipt_survives_interrupted_followup(real_assistant_child, fake_execution, monkeypatch, tamper):  # noqa: F811
    from nerya.agent.interactions import create_interaction, respond
    from nerya.core import yaml_io
    from nerya.db.repositories import AgentSessionRepository
    from nerya.task_delivery import DeliveryError, delivery_command_chain

    env, config, task, _, _ = real_assistant_child
    deps = NativeToolDeps(workspace_root=config.paths.root, file_state=FileStateCache(),
        task_state=TaskState(), skill_index=SkillIndex([]), config=config,
        paths=config.paths, active_session_id=task['task_session_id'], active_actor_id='owner')
    registry = ToolRegistry()
    register_native_tools(registry, deps)
    # Host task/parent identity survives without semantic admission or scope.
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET context_json='{}' WHERE session_id=?",
                    (env.sid,))
    call = ToolCall(name='strategy_draft_proposal', arguments={
        'strategy_id': 'owned_observer', 'markets': ['BINANCE:ETH/USDT'], 'timeframe': '1h',
        'accounts': ['paper_main'], 'create_tuning': False, 'execution_mode': 'script'})
    produced = registry.get(call.name).handler(call)
    assert not produced.is_error, produced.text()
    monkeypatch.setattr('nerya.strategies.request_scope._snapshot',
                        lambda *a, **kw: pytest.fail('semantic scope lookup forbidden during file edit'))
    receipt = produced.metadata['verification']
    assert receipt['revision'] != 'unbound'
    data = next(part.data for part in produced.content if part.type == 'json')
    assert receipt['artifact_id'] == f"strategy:owned_observer:proposal:{data['proposal_id']}"
    projected = project_tool_results([produced], render_tool_result=lambda _: {'content': []},
                                   rendered_tool_result_text=lambda _: '[producer receipt]')
    sid, tid = task['task_session_id'], config.get('runtime.command_turn_id')
    event = {**projected.event_blocks[0], 'kind': 'tool.complete', 'session_id': sid, 'turn_id': tid}
    with closing(connect(config.paths.db)) as con:
        con.execute('INSERT INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)',
                    (task['command_id'], 'real-native-draft', json.dumps(event)))
    script = config.paths.root / data['proposal_paths']['main_path']
    original_script = script.read_bytes()
    path = script.with_name('strategy.yml')
    manifest = yaml_io.loads(path.read_text())
    manifest.setdefault('params', {})['lp_replay'] = {'mode': 'cadence'}
    tools = config, deps, registry, None, path
    assert not mutate(tools, 'write_file', contents=yaml_io.dumps(manifest)).is_error
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id=?", (task['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (sid,))
    manual = env.manager.submit({'command_id': 'manual-candidate-resume', 'session_id': sid,
        'command_type': 'resume', '_auth_actor_id': 'owner', '_auth_scopes': ['execute:strategy'],
        'request': {'source': 'user_chat', 'kind': 'user.chat', 'resume_turn_id': tid,
                    'continuation_feedback': 'Continue the same candidate.', 'payload': {'text': ''}}},
        start=False)['command']
    resumed = env.manager.store.claim(sid, 'manual-resume-worker')
    assert resumed and resumed['command_id'] == manual['command_id']
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id=?", (manual['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (sid,))
    follow = env.manager.submit({'command_id': 'later-candidate-send', 'session_id': sid,
        '_auth_actor_id': 'owner', '_auth_scopes': ['execute:strategy'],
        'request': {'source': 'user_chat', 'kind': 'user.chat',
                    'payload': {'text': 'Use sampled studies on the existing candidate.'}}},
        start=False)['command']
    later = env.manager.store.claim(sid, 'later-candidate-worker')
    assert later and later['command_id'] == follow['command_id']
    tid = later['turn_id']
    config.data['runtime'].update(command_id=later['command_id'], command_turn_id=tid)
    interaction = create_interaction(config, sid=sid, tid=tid, call_id='sampled-study',
        kind='question', payload={'title': 'Use sampled studies?'})
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='awaiting_input' WHERE command_id=?", (later['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (sid,))
        AgentSessionRepository(con).save_turn_checkpoint(session_id=sid, turn_id=tid,
                                                        checkpoint={'resumable': True})
    answered = respond(env.manager, {'session_id': sid, 'interaction_id': interaction['interaction_id'],
        'response_id': 'permit-sampled-study', 'expected_revision': 1, '_auth_actor_id': 'owner',
        '_auth_scopes': ['execute:strategy'], 'action': 'answer', 'text': 'Use sampled studies on this candidate.'})
    row = env.manager.store.claim(sid, 'sampled-study-worker')
    assert row and row['command_id'] == answered['command']['command_id']
    config.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])
    with env.manager.store.transaction() as con:
        # The current checkpoint belongs to the later turn, not the old resume.
        assert AgentSessionRepository(con).peek_turn_checkpoint(sid)['turn_id'] != resumed['turn_id']
        if tamper:
            request = json.loads(resumed['request_json'])
            request[tamper] = 'foreign'
            con.execute('UPDATE agent_commands SET request_json=? WHERE command_id=?',
                        (json.dumps(request), manual['command_id']))
            with pytest.raises(DeliveryError, match='delivery_continuation_invalid'):
                delivery_command_chain(config.paths, con, manual['command_id'])
        else:
            assert [r['command_id'] for r in delivery_command_chain(config.paths, con, manual['command_id'])] == [
                manual['command_id'], task['command_id']]
        assert [r['command_id'] for r in delivery_command_chain(config.paths, con, row['command_id'])] == [
            row['command_id'], later['command_id']]
    result = mutate(tools, 'edit_file', old_string='mode: cadence', new_string='mode: sampled')
    assert result.is_error is bool(tamper), result.text()
    assert yaml_io.loads(path.read_text())['params']['lp_replay']['mode'] == ('cadence' if tamper else 'sampled')
    assert script.read_bytes() == original_script
    assert [p.name for p in config.paths.proposals.iterdir()] == [data['proposal_id']]


@pytest.mark.parametrize('tamper', [False, True])
def test_admitted_delegated_task_followup_keeps_own_producer_association(real_assistant_child, tamper):  # noqa: F811
    env, config, receipt, _, _ = real_assistant_child
    # This test needs only admission identity, not semantic classification.
    deps = NativeToolDeps(workspace_root=config.paths.root, file_state=FileStateCache(),
        task_state=TaskState(), skill_index=SkillIndex([]), config=config,
        paths=config.paths, active_session_id=receipt['task_session_id'], active_actor_id='owner')
    registry = ToolRegistry()
    register_native_tools(registry, deps)
    proposal = create_proposal(config.paths, kind='strategy_package_proposal', summary='Owned task draft',
        extra_files={'after/strategies/observer/main.py': 'MARKET = "old"'})
    path = proposal.path / 'after/strategies/observer/main.py'
    tools = config, deps, registry, proposal, path
    record_draft(tools, cid=receipt['command_id'], session=receipt['task_session_id'],
                 turn=config.get('runtime.command_turn_id'))
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id=?", (receipt['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?",
                    (receipt['task_session_id'],))
    text = 'Continue the same candidate, without trading.'
    cfg, parent = parent_request(env, operation='task_follow_up', target=receipt['task_id'],
        cid='parent-followup', text=text, scopes=('execute:strategy',))
    follow = followup_task(env, cfg, parent, text=text)
    row = env.manager.store.claim(follow['task_session_id'], 'scope-followup-worker')
    assert row and row['command_id'] == follow['command_id']
    config.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])
    if tamper:
        with env.manager.store.transaction() as con:
            request = json.loads(row['request_json'])
            request['payload']['assistant_request_binding']['actor_id'] = 'foreign'
            con.execute('UPDATE agent_commands SET request_json=? WHERE command_id=?',
                        (json.dumps(request), row['command_id']))
    result = mutate(tools, 'write_file')
    assert result.is_error is tamper, result.text()
    assert path.read_text() == ('MARKET = "old"' if tamper else 'MARKET = "new"')


def test_tool_approval_continuation_reuses_delegated_draft_admission(real_assistant_child):  # noqa: F811
    from nerya.core import jsonl
    from nerya.strategies.request_scope import _snapshot
    from nerya.strategies import request_scope as scope
    from test_request_scope import spans_output

    env, config, receipt, _, _ = real_assistant_child
    deps = NativeToolDeps(workspace_root=config.paths.root, file_state=FileStateCache(),
        task_state=TaskState(), skill_index=SkillIndex([]), config=config,
        paths=config.paths, active_session_id=receipt['task_session_id'], active_actor_id='owner')
    registry = ToolRegistry()
    register_native_tools(registry, deps)
    proposal = create_proposal(config.paths, kind='strategy_package_proposal', summary='Owned task draft',
        extra_files={'after/strategies/observer/main.py': 'MARKET = "old"'})
    path = proposal.path / 'after/strategies/observer/main.py'
    tools = config, deps, registry, proposal, path
    gateway = FakeGateway(lambda docs: spans_output(docs[0], base='ETH', quote='USDT', window_days='30天'))
    original_contract = scope.ensure_contract(config, gateway=gateway)
    record_draft(tools, cid=receipt['command_id'], session=receipt['task_session_id'],
                 turn=config.get('runtime.command_turn_id'))
    original = _snapshot(config, active=True)
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='awaiting_approval' WHERE command_id=?",
                    (receipt['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?",
                    (receipt['task_session_id'],))
    approval = {'approval_id': 'draft-write-permission', 'state': 'approved',
        'kind': 'tool_permission_batch', 'requester_actor_id': 'owner',
        'requester_session_id': receipt['task_session_id'],
        'turn_id': config.get('runtime.command_turn_id')}
    jsonl.append(config.paths.approvals_approved, approval)
    accepted = env.manager.resolve_approval(approval, start=False)
    row = env.manager.store.claim(receipt['task_session_id'], 'approval-worker')
    assert row and row['command_id'] == accepted['command']['command_id']
    config.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])

    result = mutate(tools, 'write_file')
    assert not result.is_error, result.text()
    assert path.read_text() == 'MARKET = "new"'
    resumed = _snapshot(config, active=True)
    assert resumed['binding']['documents'] == original['binding']['documents']
    assert resumed['binding']['command_id'] == row['command_id']
    assert resumed['revision'] == row['revision']
    refreshed = scope.ensure_contract(config, gateway=gateway)
    assert refreshed['binding']['command_id'] == row['command_id']
    assert refreshed['constraints'] == original_contract['constraints']
    assert refreshed['extractions'] == original_contract['extractions']
    assert refreshed['request_scope_hash'] != original_contract['request_scope_hash']
    assert scope.current_contract(config) == refreshed
    assert len(gateway.calls) == 1

    # A real saved question response resumes that approval's exact chain.
    from nerya.agent.interactions import create_interaction, respond
    from nerya.db.repositories import AgentSessionRepository
    interaction = create_interaction(config, sid=row['session_id'], tid=row['turn_id'],
        call_id='clarify-draft', kind='question', payload={'title': 'Continue candidate?'})
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='awaiting_input' WHERE command_id=?", (row['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (row['session_id'],))
        AgentSessionRepository(con).save_turn_checkpoint(session_id=row['session_id'],
            turn_id=row['turn_id'], checkpoint={'resumable': True})
    answer = respond(env.manager, {'session_id': row['session_id'],
        'interaction_id': interaction['interaction_id'], 'response_id': 'continue-candidate',
        'expected_revision': 1, '_auth_actor_id': 'owner', '_auth_scopes': ['execute:strategy'],
        'action': 'answer', 'text': 'Continue the same candidate.'})
    response = env.manager.store.claim(row['session_id'], 'response-worker')
    assert response['command_id'] == answer['command']['command_id']
    config.data['runtime'].update(command_id=response['command_id'], command_turn_id=response['turn_id'])
    assert scope.current_contract(config)['binding']['command_id'] == response['command_id']
    answered = scope.ensure_contract(config, gateway=gateway)
    assert scope.current_contract(config) == answered
    assert answered['binding']['command_id'] == response['command_id']
    assert answered['constraints'] == original_contract['constraints']
    assert answered['normalized_evidence'] == original_contract['normalized_evidence']
    assert len(gateway.calls) == 1
    with env.manager.store.transaction() as con:
        assert scope.equivalent_continuation_scope(config, con, command_id=response['command_id'],
            source_command_id=receipt['command_id'], source_scope_hash=original_contract['request_scope_hash'])
        assert not scope.equivalent_continuation_scope(config, con, command_id=response['command_id'],
            source_command_id=receipt['command_id'], source_scope_hash='wrong-hash')
    assert not mutate(tools, 'write_file', contents='MARKET = "new"').is_error

    # Approval resumes admission; it cannot replace the original parent binding.
    with env.manager.store.transaction() as con:
        request = json.loads(con.execute('SELECT request_json FROM agent_commands WHERE command_id=?',
            (receipt['command_id'],)).fetchone()[0])
        request['payload']['assistant_request_binding']['actor_id'] = 'foreign'
        con.execute('UPDATE agent_commands SET request_json=? WHERE command_id=?',
                    (json.dumps(request), receipt['command_id']))
    assert mutate(tools, 'write_file', contents='TAMPERED').is_error
    assert path.read_text() == 'MARKET = "new"'
