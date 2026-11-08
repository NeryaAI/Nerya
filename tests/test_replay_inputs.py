"""Offline producer-to-sandbox input contracts; fixtures are not live evidence."""
import hashlib
import json
from pathlib import Path

import pytest

from nerya.core.errors import TradingError
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataActionSpec, DataApiError
from nerya.data_api.defillama import defillama_specs
from nerya.harness.cancellation import CancelToken, CancelledError, raise_if_cancelled
from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs
from nerya.skills.builtin.backtest.scripts.freeform_run import run_freeform_backtest
from test_defillama_data_api import Fake, PID
from test_freeform_sandbox import package, macos
import test_request_scope as request_scope_fixtures

real_assistant_child = request_scope_fixtures.real_assistant_child

pytestmark = pytest.mark.smoke


def declaration():
    return {'version': 1, 'datasets': [{'id': 'rates', 'provider': 'defillama',
        'action': 'yield_history', 'args': {'pool_id': PID, 'start': '2026-10-20', 'end': '2026-10-22'}}]}


def registry():
    reg = DataApiRegistry()
    for spec in defillama_specs(Fake()):
        reg.register_action(spec)
    return reg


def test_public_input_preparation_preserves_hashes_and_coverage_gap(tmp_path):
    (tmp_path / 'replay_inputs.json').write_text(json.dumps(declaration()))
    prepared = prepare_inputs(tmp_path, registry=registry())
    receipt = prepared['receipts'][0]
    assert receipt['requested_window_complete'] is False
    assert receipt['coverage']['missing_utc_days'] == ['2026-10-21']
    assert receipt['sha256'] == hashlib.sha256(prepared['files']['rates.json']).hexdigest()
    data = json.loads(prepared['files']['rates.json'])
    assert data['_envelope']['pool']['pool_id'] == PID
    assert len(data['rows']) == 2 and len(receipt['sources']) == 2


def test_replay_registry_requires_installed_opt_in_and_validates_schema():
    reg = DataApiRegistry()
    seen = []
    spec = DataActionSpec(provider='fixture', action='public', title='fixture', description='fixture',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        handler=lambda ctx, args: seen.append(ctx) or {})
    reg.register_action(spec)
    with pytest.raises(DataApiError, match='does not support'):
        reg.call_replay_input('fixture', 'public', {})
    from dataclasses import replace
    reg.register_action(replace(spec, replay_input=True))
    with pytest.raises(DataApiError):
        reg.call_replay_input('fixture', 'public', {'endpoint': 'unsafe'})
    assert not seen
    reg.call_replay_input('fixture', 'public', {})
    assert seen[0].config_like is None and seen[0].workspace is None


def test_replay_registry_passes_cancellation_to_real_provider_handler(tmp_path):
    (tmp_path / "replay_inputs.json").write_text(json.dumps({
        "version": 1,
        "datasets": [{
            "id": "cancelled", "provider": "fixture", "action": "cancelled", "args": {},
        }],
    }))
    token = CancelToken()
    token.cancel("test cancellation")
    def handler(context, args):
        assert context.cancel_token is token
        raise_if_cancelled(context.cancel_token)
        raise AssertionError("cancelled handler continued")

    registry = DataApiRegistry()
    registry.register_action(DataActionSpec(
        provider="fixture", action="cancelled", title="cancelled", description="cancelled",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler, replay_input=True,
    ))

    with pytest.raises(CancelledError):
        prepare_inputs(tmp_path, registry=registry, cancel_token=token)


def test_sampled_protocol_state_never_becomes_complete_window(tmp_path):
    (tmp_path / 'replay_inputs.json').write_text(json.dumps({
        'version':1,'datasets':[{'id':'pool','provider':'fixture','action':'sample', 'args':{}}]}))
    reg = DataApiRegistry()
    reg.register_action(DataActionSpec(provider='fixture',action='sample',title='sample',description='sample',
        input_schema={'type':'object','properties':{},'additionalProperties':False},replay_input=True,
        handler=lambda context,args:{'rows':[{'block_number':1}], '_envelope':{
            'mock':False,'sources':[{'sha256':'a'*64}],
            'coverage':{'completeness':'not_asserted','requested_window_complete':False}}}))
    receipt = prepare_inputs(tmp_path,registry=reg)['receipts'][0]
    assert receipt['requested_window_complete'] is False


def test_snapshot_discovery_uses_durable_task_identity_without_semantic_scope(real_assistant_child):
    from nerya.data_api.replay_snapshot import read, save
    from nerya.db.repositories import AgentSessionRepository
    from nerya.tools.native.data_api import data_api_handler
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall
    from test_assistant_receipts import request as parent_request, create

    env, config, task, _, saved = real_assistant_child
    sid, cid = task['task_session_id'], task['command_id']
    tid = config.get('runtime.command_turn_id')
    parent_id = saved['assistant_request_binding']['command_id']
    output = {'rows': [{'block_number': 1}], '_envelope': {
        'mock': True, 'pool': {'pool_id': 'fixture-pool'},
        'coverage': {'requested_window_complete': False, 'completeness': 'not_asserted'}}}
    calls = []
    reg = DataApiRegistry()
    reg.register_action(DataActionSpec(provider='fixture', action='history', title='history',
        description='offline fixture', replay_input=True,
        handler=lambda ctx, args: calls.append(args) or output,
        input_schema={'type': 'object', 'properties': {'tick': {'type': 'integer'}},
                      'required': ['tick'], 'additionalProperties': False}))
    call = ToolCall(id='snapshot-source', name='data_api', arguments={
        'op': 'call', 'provider': 'fixture', 'action': 'history', 'args': {'tick': 10}})
    result = data_api_handler(call, config_like=config, registry=reg)
    assert not result.is_error, result.text()
    ref = tool_json_data(result)['producer_ref']
    event = dict(event_id='snapshot-source-complete', kind='tool.complete', action='data_api',
        skill_id='native', call_id=call.id, ok=True, session_id=sid, turn_id=tid,
        payload=call.arguments, result=tool_json_data(result))
    env.manager.store.record_event(cid, event)
    # A saved file alone is not producer attestation and must never be discovered.
    unrecorded = save(config, ToolCall(id='unrecorded', name='data_api'), 'fixture', 'history', {'tick': 10}, output)
    assert unrecorded != ref
    with env.manager.store.transaction() as con:
        parent_context = con.execute('SELECT context_json FROM agent_commands WHERE command_id=?',
                                     (parent_id,)).fetchone()[0]
        con.execute("UPDATE agent_commands SET context_json='{}' WHERE command_id=?", (parent_id,))

    def discover():
        return data_api_handler(ToolCall(name='data_api', arguments={
            'op': 'snapshots', 'provider': 'fixture', 'action': 'history'}),
            config_like=config, registry=reg)

    discovered = discover()
    assert not discovered.is_error, discovered.text()
    rows = tool_json_data(discovered)['snapshots']
    assert len(rows) == 1 and rows[0]['producer_ref'] == ref
    assert rows[0]['args'] == {'tick': 10} and rows[0]['row_count'] == 1
    assert rows[0]['coverage'] == output['_envelope']['coverage']
    # Discovery does not relax the separate admission for actual input use.
    with pytest.raises(DataApiError, match='not bound'):
        read(config, ref, 'fixture', 'history', {'tick': 10})
    with env.manager.store.transaction() as con:
        con.execute('UPDATE agent_commands SET context_json=? WHERE command_id=?', (parent_context, parent_id))
    assert read(config, ref, 'fixture', 'history', {'tick': 10}) == output
    for bad_ref, provider, action, args in (
        (ref, 'fixture', 'history', {'tick': 20}), (ref, 'foreign', 'history', {'tick': 10}),
        (ref, 'fixture', 'other', {'tick': 10}), (unrecorded, 'fixture', 'history', {'tick': 10}),
    ):
        with pytest.raises(DataApiError, match='not bound'):
            read(config, bad_ref, provider, action, args)
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET context_json='{}' WHERE command_id=?", (parent_id,))
        con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id=?", (cid,))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (sid,))
        AgentSessionRepository(con).save_turn_checkpoint(session_id=sid, turn_id=tid,
                                                        checkpoint={'resumable': True})
    manual = env.manager.submit({'command_id': 'snapshot-manual-resume', 'session_id': sid,
        'command_type': 'resume', '_auth_actor_id': 'owner', '_auth_scopes': ['execute:strategy'],
        'request': {'source': 'user_chat', 'kind': 'user.chat', 'resume_turn_id': tid,
                    'continuation_feedback': 'List this task saved datasets.', 'payload': {'text': ''}}},
        start=False)['command']
    resumed = env.manager.store.claim(sid, 'snapshot-resume-worker')
    assert resumed and resumed['command_id'] == manual['command_id']
    config.data['runtime'].update(command_id=resumed['command_id'], command_turn_id=resumed['turn_id'])
    discovered = discover()
    assert not discovered.is_error, discovered.text()
    assert tool_json_data(discovered)['snapshots'] == rows
    # Event source arguments and stored bytes must still match the producer ref.
    changed = json.loads(json.dumps(event))
    changed['payload']['args']['tick'] = 20
    with env.manager.store.transaction() as con:
        con.execute('UPDATE agent_command_events SET payload_json=? WHERE command_id=? AND event_id=?',
                    (json.dumps(changed), cid, event['event_id']))
    assert discover().is_error
    with env.manager.store.transaction() as con:
        con.execute('UPDATE agent_command_events SET payload_json=? WHERE command_id=? AND event_id=?',
                    (json.dumps(event), cid, event['event_id']))
    snapshot = config.paths.root / 'state' / 'replay_inputs' / (ref[9:] + '.json')
    original = snapshot.read_bytes()
    snapshot.write_bytes(original + b' ')
    assert discover().is_error
    snapshot.write_bytes(original)
    config.data['runtime']['financial_actor_id'] = 'foreign-owner'
    assert discover().is_error
    config.data['runtime']['financial_actor_id'] = 'owner'
    cfg, parent = parent_request(env, cid='snapshot-other-parent', scopes=('execute:strategy',))
    sibling = create(env, cfg, parent)
    other = env.manager.store.claim(sibling['task_session_id'], 'snapshot-other-worker')
    assert other and other['session_id'] != sid
    config.data['runtime'].update(command_id=other['command_id'], command_session_id=other['session_id'],
                                 command_turn_id=other['turn_id'])
    unrelated = discover()
    assert not unrelated.is_error, unrelated.text()
    assert tool_json_data(unrelated)['snapshots'] == []
    assert calls == [{'tick': 10}]


def test_explicit_producer_snapshot_reuses_only_attested_same_task_input(tmp_path):
    from test_task_delivery import _runtime, _command
    from nerya.tools.native.data_api import data_api_handler
    from nerya.tools.types import ToolCall
    from nerya.tools.result_contracts import tool_json_data
    from nerya.llm.tool_compaction import compact_tool_result

    config, runtime = _runtime(tmp_path)
    queued = _command(runtime, text='Research exact historical pool data', scopes=('execute:strategy',))
    command = runtime.store.claim(queued['session_id'], 'fixture-worker')
    config.data.setdefault('runtime', {}).update(command_id=command['command_id'],
        command_session_id=command['session_id'], command_turn_id=command['turn_id'])
    calls = []
    reg = DataApiRegistry()
    def acquire(ctx, args):
        calls.append(args)
        return {'rows': [{'block_number': i} for i in range(55)], '_envelope': {
            'mock': False, 'sources': [{'sha256': hashlib.sha256(str(i).encode()).hexdigest()}
                                        for i in range(12)], 'pool': {'pool_id': 'fixture-pool'},
            'coverage': {'requested_window_complete': False, 'completeness': 'not_asserted'}}}
    reg.register_action(DataActionSpec(provider='fixture', action='history', title='history',
        description='history', replay_input=True, handler=acquire, input_schema={
            'type': 'object', 'properties': {'tick': {'type': 'integer'}},
            'required': ['tick'], 'additionalProperties': False}))
    call = ToolCall(id='history-call', name='data_api', arguments={
        'op': 'call', 'provider': 'fixture', 'action': 'history', 'args': {'tick': 10}, 'limit': 1})
    result = data_api_handler(call, config_like=config, registry=reg)
    assert not result.is_error, result.text()
    payload = tool_json_data(result)
    ref = payload['producer_ref']
    manifest = {'version': 1, 'datasets': [{'id': 'pool', 'provider': 'fixture',
        'action': 'history', 'args': {'tick': 10}, 'producer_ref': ref}]}
    root = tmp_path / 'candidate'
    root.mkdir()
    (root / 'replay_inputs.json').write_text(json.dumps(manifest))
    with pytest.raises(TradingError, match='not bound'):
        prepare_inputs(root, registry=reg, config=config)
    compacted = compact_tool_result('data_api', payload, size_threshold=0)
    assert compacted.kept['producer_ref'] == ref
    runtime.store.record_event(command['command_id'], dict(event_id='history-complete', kind='tool.complete', action='data_api',
        skill_id='native', call_id=call.id, ok=True, session_id=command['session_id'],
        payload=call.arguments,
        turn_id=command['turn_id'], result=compacted.summary + '\n[compacted_kept]\n' + json.dumps(compacted.kept)))
    prepared = prepare_inputs(root, registry=reg, config=config)
    discovered = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'snapshots', 'provider': 'fixture', 'action': 'history'}), config_like=config, registry=reg)
    assert not discovered.is_error, discovered.text()
    assert tool_json_data(discovered)['snapshots'][0]['producer_ref'] == ref
    assert tool_json_data(discovered)['snapshots'][0]['identity']['pool'] == {'pool_id': 'fixture-pool'}
    assert len(json.loads(prepared['files']['pool.json'])['rows']) == 55
    assert prepared['receipts'][0]['requested_window_complete'] is False
    del manifest['datasets'][0]['producer_ref']
    (root / 'replay_inputs.json').write_text(json.dumps(manifest))
    reused = prepare_inputs(root, registry=reg, config=config)
    assert reused['files'] == prepared['files']
    assert reused['receipts'][0]['producer_ref'] == ref
    assert calls == [{'tick': 10}]
    manifest['datasets'][0]['producer_ref'] = ref
    (root / 'replay_inputs.json').write_text(json.dumps(manifest))
    snapshot = tmp_path / 'state' / 'replay_inputs' / (ref[9:] + '.json')
    original = snapshot.read_bytes()
    snapshot.write_bytes(original + b' ')
    with pytest.raises(TradingError, match='not bound'):
        prepare_inputs(root, registry=reg, config=config)
    snapshot.unlink()
    import os
    os.mkfifo(snapshot)
    with pytest.raises(TradingError, match='not bound'):
        prepare_inputs(root, registry=reg, config=config)
    snapshot.unlink()
    snapshot.write_bytes(original)
    manifest['datasets'][0]['args']['tick'] = 20
    (root / 'replay_inputs.json').write_text(json.dumps(manifest))
    with pytest.raises(TradingError, match='not bound'):
        prepare_inputs(root, registry=reg, config=config)
    assert calls == [{'tick': 10}]
    store = snapshot.parent
    store.rename(store.with_name('saved_inputs'))
    store.symlink_to(store.with_name('saved_inputs'), target_is_directory=True)
    manifest['datasets'][0]['args']['tick'] = 10
    (root / 'replay_inputs.json').write_text(json.dumps(manifest))
    with pytest.raises(TradingError, match='not bound'):
        prepare_inputs(root, registry=reg, config=config)


@pytest.mark.parametrize('damage', ['traversal', 'duplicate', 'authored_receipt', 'symlink', 'oversize'])
def test_invalid_input_manifest_never_dispatches(tmp_path, damage):
    value = declaration()
    if damage == 'traversal':
        value['datasets'][0]['id'] = '../private'
    if damage == 'duplicate':
        value['datasets'] *= 2
    if damage == 'authored_receipt':
        value['datasets'][0]['coverage_ok'] = True
    path = tmp_path / 'replay_inputs.json'
    if damage == 'symlink':
        (tmp_path / 'other').write_text(json.dumps(value))
        path.symlink_to(tmp_path / 'other')
    else:
        path.write_text(' ' * 33000 if damage == 'oversize' else json.dumps(value))
    class Never:
        def call_replay_input(self, *args, cancel_token=None):
            pytest.fail('invalid input dispatched')
    with pytest.raises((TradingError, OSError)):
        prepare_inputs(tmp_path, registry=Never())


@macos
def test_real_sandbox_reads_only_host_prepared_data_and_retains_receipt(tmp_path, monkeypatch):
    script = '''
from nerya.strategies.research import dataset, write_json
data = dataset('rates')
assert len(data['rows']) == 2
assert data['_envelope']['pool']['symbol'] == 'USDC'
write_json('result.json', {'equity_curve': [{'ts': 1792454400, 'equity': 100}, {'ts': 1792627200, 'equity': 101}], 'trades': []})
'''
    root = package(tmp_path, script, {'replay_inputs.json': json.dumps(declaration())})
    monkeypatch.setattr('nerya.skills.builtin.backtest.scripts.replay_inputs.build_data_api_registry', registry)
    result = run_freeform_backtest(strategy_id='sandbox_test', workspace=tmp_path)
    assert result['ok'] and result['coverage_ok'] is False and result['performance_evidence'] is False
    receipt = result['provenance']['datasets'][0]
    assert result['isolation']['prepared_inputs'][receipt['snapshot_file']] == receipt['sha256']
    state = json.loads(Path(result['run_receipt']).read_text())
    assert state['status'] == 'completed' and state['strategy_id'] == 'sandbox_test'
    assert state['source_revision'] == result['provenance']['source_revision']
    assert not (root / 'history').exists(), 'host data is never written into candidate source'
    assert Path(result['report_path']).is_file() or (tmp_path / result['report_path']).is_file()
    saved = Path(result['run_receipt']).parent / receipt['snapshot_file']
    assert saved.is_file() and hashlib.sha256(saved.read_bytes()).hexdigest() == receipt['sha256']
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.strategies.tool_verification import attach_strategy_checks, source_binding
    from nerya.tools.types import ToolCall, ToolResult
    config = Config(paths=WorkspacePaths(tmp_path))
    binding = source_binding(config, ToolCall(name='strategy_backtest', arguments={'strategy_id': 'sandbox_test'}))
    verified = attach_strategy_checks(ToolResult.from_json(tool_use_id='replay', name='strategy_backtest', data=result),
        before=binding, after=binding, kind='backtest', config=config)
    checks = {check['check_id']: check for check in verified.metadata['verification']['checks']}
    assert checks['report_output']['status'] == checks['source_binding']['status'] == 'passed'
    assert checks['historical_run']['evidence_ref'].endswith('/run.json')
    assert checks['data_coverage']['status'] == 'failed'
    assert checks['execution_integrity']['status'] == 'missing'
    assert checks['historical_performance']['status'] == 'missing'
    chart=json.loads(Path(result["chart_path"]).read_text())
    assert chart["meta"]["requested_window_complete"] is False
    assert chart["meta"]["requested_window_days"] == 2
    assert chart["meta"]["performance_evidence"] is False
    assert chart["meta"]["provenance"] == result["provenance"]


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_preparation_failure_has_durable_failed_run(tmp_path, monkeypatch, status):
    from types import SimpleNamespace
    detail = {'provider': 'uniswap', 'action': 'pool_history',
        'reason': 'historical_rpc_evidence_failure', 'failure_stage': 'http_status',
        'rpc_provider': 'publicnode', 'rpc_method': 'eth_call', 'request_count': 8,
        'http_status': status, 'coverage_complete': False, 'safe_to_retry': False}
    kind = 'configuration_required' if status in (401, 403) else 'provider_error'
    def fail(*args, **kwargs):
        raise DataApiError('Historical RPC evidence failure: http_status', kind=kind,
            retryable=False, detail={**detail, 'body': 'PRIVATE_PROVIDER_BODY',
                                    'headers': {'Authorization': 'Bearer PRIVATE_TOKEN'}})
    package(tmp_path, 'print("unused")', {'replay_inputs.json': json.dumps(declaration())})
    monkeypatch.setattr('nerya.skills.builtin.backtest.scripts.replay_inputs.build_data_api_registry',
                        lambda: SimpleNamespace(call_replay_input=fail))
    with pytest.raises(TradingError) as exc:
        run_freeform_backtest(strategy_id='sandbox_test', workspace=tmp_path)
    assert exc.value.replay_phase == 'preparing_data'
    receipt = json.loads(Path(exc.value.run_receipt).read_text())
    assert receipt['status'] == 'failed'
    expected = detail
    assert exc.value.receipt == expected
    assert isinstance(exc.value.__cause__, DataApiError)
    failure = json.loads(Path(exc.value.failure_path).read_text())
    assert failure['details'] == expected
    assert 'PRIVATE_' not in json.dumps(receipt) + json.dumps(failure)
