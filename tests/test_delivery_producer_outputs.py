"""Real producer output contracts in isolated workspaces; no model path trust."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from nerya.strategies.tool_verification import attach_strategy_checks, source_binding
from nerya.tools.types import ToolCall, ToolResult
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.native import delivery as native_delivery
from nerya.task_delivery import update_delivery
from nerya.core import yaml_io
from nerya.evolution.patch_proposal import create_proposal
from nerya.strategies.request_scope import ensure_contract, compare
from test_task_delivery import _runtime, _deps, _persist_replay_outputs
from test_request_scope import FakeGateway, TEXT

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('NETWORK_OR_REAL_MODEL_FORBIDDEN')
    monkeypatch.setattr('socket.socket.connect', denied)
    monkeypatch.setattr('socket.create_connection', denied)
    monkeypatch.setattr('nerya.llm.gateway.LLMGateway.call_messages', denied)


def fixture(tmp_path, text=TEXT):
    from nerya.agent.tool_projection import project_tool_results

    config, runtime = _runtime(tmp_path)
    config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model',
                                           'allowed_tasks': ['agent.loop']}}
    config.data['agent']['native']['tier'] = 'medium'
    command = runtime.submit({
        'command_id': 'delivery-command', 'session_id': 'delivery-session',
        '_auth_actor_id': 'operator', '_auth_scopes': ['execute:strategy'],
        'request': {'payload': {'text': text}},
    }, start=False)['command']
    claimed = runtime.store.claim(command['session_id'], 'scope-fixture-worker')
    assert claimed and claimed['command_id'] == command['command_id']
    deps = _deps(config, command)
    config.data['runtime'].update(command_session_id=command['session_id'],
        command_turn_id=command['turn_id'], financial_actor_id='operator')
    gateway = FakeGateway()
    contract = ensure_contract(config, gateway=gateway)
    assert len(gateway.calls) == 1
    sid = 'eth_ema_trend_1h'
    manifest = {'id': sid, 'mode': 'paper', 'markets': ['BINANCE:ETH/USDT'],
                'timeframe': '1h', 'backtest': {'window_days': 30}}
    proposal = create_proposal(config.paths, kind='strategy_package_proposal', summary='candidate',
        rationale='test', test_plan='test', rollback='test', target=f'strategies/{sid}',
        extra_files={f'after/strategies/{sid}/main.py': 'def run(ctx): return ctx.result.hold()\n',
                     f'after/strategies/{sid}/strategy.yml': yaml_io.dumps(manifest)})
    call = ToolCall(id='completed-replay', name='strategy_backtest',
                    arguments={'strategy_id': sid, 'proposal_id': proposal.id})
    binding = source_binding(config, call)
    window = compare(contract, manifest)
    start, end = contract['constraints']['start_utc'], contract['constraints']['end_utc']
    data = {'ok': True, 'backtest_status': 'completed', 'verdict': 'FAIL', 'total_return_pct': -6.7,
        'requested_window_complete': True, 'performance_evidence': True, **window,
        'run_receipt': 'backtests/20261022_040449/run.json',
        'replay': {'errors': 0, 'sdk_errors': 0, 'order_accounting_ok': True},
        'provenance': {'data_kind': 'historical', 'source_revision': binding['revision'],
            'source_changed_during_run': False, 'datasets': [{'market': 'BINANCE:ETH/USDT',
                'timeframe': '1h', 'rows': 770, 'first_ts': int(start), 'last_ts': int(end - 3600),
                'duplicate_timestamps': 0, 'out_of_order': False, 'sha256': 'd' * 64}]}}
    # These remain offline replay fixtures. The producer scope comparison and
    # persisted artifact/hash checks are real, not model-supplied pass flags.
    _persist_replay_outputs(config, proposal, sid, data)
    produced = attach_strategy_checks(ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data),
        before=binding, after=binding, kind='backtest', config=config)
    assert produced.content[0].data['request_scope']['request_scope_hash'] == contract['request_scope_hash']
    scope_check = next(c for c in produced.metadata['verification']['checks'] if c['check_id'] == 'request_scope_match')
    # The producer still attests the match; it is not a universal replay prerequisite.
    assert scope_check['status'] == 'passed' and scope_check['required'] is False
    block = project_tool_results([produced], render_tool_result=lambda _: {'content': []},
        rendered_tool_result_text=lambda _: '[fixture receipt]').event_blocks[0]
    event = {**block, 'event_id': 'completed-replay-event', 'kind': 'tool.complete',
             'session_id': command['session_id'], 'turn_id': command['turn_id'], 'payload': call.arguments}
    runtime.store.record_event(command['command_id'], event)
    args = {'skill_id': 'backtest', 'workflow_id': 'strategy_replay', 'requested_scope': {'summary': TEXT},
            'stages': [{'id': 'replay', 'state': 'completed', 'required_artifacts': ['report'],
                        'required_checks': [c['check_id'] for c in produced.metadata['verification']['checks'] if c['required']]},
                       {'id': 'deliver', 'state': 'pending'}]}
    run = config.paths.root / 'backtests/20261022_040449/run.json'
    data = json.loads(run.with_name('metrics.json').read_text())
    data.update(run_receipt=str(run), report_path=str(run.with_name('report.md')))
    return config, runtime, command, deps, proposal, event, args, run, data, binding


def checks(config, data, binding, **kwargs):
    result = attach_strategy_checks(ToolResult.from_json(tool_use_id='output', name='strategy_backtest',
        data=deepcopy(data)), before=binding, after=binding, kind='backtest', config=config, **kwargs)
    return {row['check_id']: row for row in result.metadata['verification']['checks']}


def test_real_writer_outputs_bind_all_skill_artifacts_and_keep_economic_loss(tmp_path):
    config, runtime, command, deps, proposal, event, args, run, data, binding = fixture(tmp_path)
    rows = checks(config, data, binding)
    candidate = (proposal.path / 'after/strategies/eth_ema_trend_1h').relative_to(config.paths.root).as_posix()
    assert rows['source_binding']['evidence_ref'] == candidate
    assert rows['historical_run']['evidence_ref'] == run.relative_to(config.paths.root).as_posix()
    assert rows['report_output']['status'] == 'passed'
    assert rows['report_output']['evidence_ref'] == run.with_name('report.md').relative_to(config.paths.root).as_posix()
    import nerya.task_delivery as delivery_module
    skill = Path(delivery_module.__file__).parent / 'skills/builtin/strategy_author/SKILL.md'
    args = json.loads(skill.read_text().split('```json', 1)[1].split('```', 1)[0])
    row = tool_json_data(native_delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments=args), deps=deps))
    # Actual validation producer adapter, alongside the real persisted replay outputs.
    attach_strategy_checks(ToolResult.from_json(tool_use_id='validate', name='strategy_validate',
        data={'ok': True, 'structural': {'status': 'passed'}, 'behavioral': {'status': 'passed', 'exit_code': 0}}),
        before=binding, after=binding, kind='validation', config=config)
    from nerya.task_delivery import read_delivery
    row = read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], stages=[{'id': stage['id'], 'state': 'completed'} for stage in args['stages']],
        artifacts=[{'artifact_id': name, 'ref': ref} for name, ref in [
            ('candidate', candidate), ('report', rows['report_output']['evidence_ref']),
            ('run_receipt', rows['historical_run']['evidence_ref'])]], limitations=['Economic FAIL; no live trading'])
    result = native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=deps)
    assert result.semantic_success is True, tool_json_data(result)['completion']
    assert tool_json_data(result)['completion']['status'] == 'partial'
    assert data['verdict'] == 'FAIL'


def test_later_goal_rejects_finalized_receipt_after_source_changes(tmp_path):
    from contextlib import closing
    from nerya.core.paths import WorkspacePaths
    from nerya.db.sqlite import connect_readonly
    from nerya.task_delivery import completion_is_current, read_delivery
    test_real_writer_outputs_bind_all_skill_artifacts_and_keep_economic_loss(tmp_path)
    paths=WorkspacePaths(tmp_path)
    with closing(connect_readonly(paths.db)) as con:
        row=con.execute("SELECT command_id,session_id FROM agent_commands WHERE delivery_json IS NOT NULL").fetchone()
    delivery=read_delivery(paths,command_id=row[0],session_id=row[1])
    assert completion_is_current(paths,delivery)
    ref=next(check['evidence_ref'] for check in delivery['checks'] if check['check_id']=='source_binding')
    (paths.root/ref/'main.py').write_text('# source edited after finalize\n')
    assert not completion_is_current(paths,delivery)


@pytest.mark.parametrize('performance,status', [(False, 'partial'), (True, 'complete')])
def test_native_finalize_retains_producer_performance_scope_without_model_limitations(tmp_path, performance, status):
    from nerya.agent.tool_projection import project_tool_results
    from nerya.task_delivery import read_delivery

    config, runtime, command, deps, proposal, event, args, run, data, binding = fixture(tmp_path)
    skill = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/SKILL.md'
    start_args = json.loads(skill.read_text().split('```json', 1)[1].split('```', 1)[0])
    started = native_delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments=start_args), deps=deps)
    assert not started.is_error
    attach_strategy_checks(ToolResult.from_json(tool_use_id='validate', name='strategy_validate',
        data={'ok': True, 'structural': {'status': 'passed'}, 'behavioral': {'status': 'passed', 'exit_code': 0}}),
        before=binding, after=binding, kind='validation', config=config)
    data['performance_evidence'] = performance
    replay = attach_strategy_checks(ToolResult.from_json(tool_use_id='current-replay', name='strategy_backtest', data=data),
        before=binding, after=binding, kind='backtest', config=config)
    block = project_tool_results([replay], render_tool_result=lambda _: {'content': []},
        rendered_tool_result_text=lambda _: '[compacted]').event_blocks[0]
    runtime.store.record_event(command['command_id'], {**block, 'kind': 'tool.complete',
        'event_id': 'current-replay', 'session_id': command['session_id'], 'turn_id': command['turn_id']})
    row = read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])
    receipt = {check['check_id']: check for check in row['checks']}
    candidate = (proposal.path / 'after/strategies/eth_ema_trend_1h').relative_to(config.paths.root).as_posix()
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], stages=[{'id': stage['id'], 'state': 'completed'} for stage in start_args['stages']],
        artifacts=[{'artifact_id': name, 'ref': ref} for name, ref in [('candidate', candidate),
            ('report', receipt['report_output']['evidence_ref']), ('run_receipt', receipt['historical_run']['evidence_ref'])]],
        limitations=[])
    result = native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=deps)
    final = tool_json_data(result)
    assert result.semantic_success is True and final['completion']['allowed']
    assert final['completion']['status'] == status
    persisted = read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])
    assert persisted['completion'] == final['completion']
    check = next(item for item in persisted['checks'] if item['check_id'] == 'historical_performance')
    assert check['producer_owned'] and check['required'] is False
    assert check['status'] == ('passed' if performance else 'missing')


@pytest.mark.parametrize('damage', ['missing_report', 'empty_report', 'symlink_report', 'outside_report',
    'other_run_report', 'missing_run', 'running_run', 'wrong_proposal', 'bad_metrics', 'wrong_revision',
    'changed_source', 'preflight', 'pending_result'])
def test_unattested_outputs_never_gain_passing_report_check(tmp_path, damage):
    config, runtime, command, deps, proposal, event, args, run, data, binding = fixture(tmp_path)
    report, metrics = run.with_name('report.md'), run.with_name('metrics.json')
    options = {}
    if damage == 'missing_report':
        report.unlink()
    elif damage == 'empty_report':
        report.write_text('')
    elif damage == 'symlink_report':
        other = report.with_name('other.md')
        report.rename(other)
        report.symlink_to(other)
    elif damage == 'outside_report':
        data['report_path'] = '/tmp/untrusted-report.md'
    elif damage == 'other_run_report':
        other = config.paths.root / 'other/report.md'
        other.parent.mkdir()
        other.write_text('unrelated')
        data['report_path'] = str(other)
    elif damage == 'missing_run':
        run.unlink()
    elif damage in {'running_run', 'wrong_proposal'}:
        record = json.loads(run.read_text())
        record.update({'status': 'running'} if damage == 'running_run' else {'proposal_id': 'different'})
        run.write_text(json.dumps(record))
    elif damage == 'bad_metrics':
        metrics.write_text('[]')
    elif damage in {'wrong_revision', 'changed_source'}:
        record = json.loads(metrics.read_text())
        record['provenance'].update({'source_revision': 'different'} if damage == 'wrong_revision'
                                    else {'source_changed_during_run': True})
        metrics.write_text(json.dumps(record))
    elif damage == 'preflight':
        options['preflight_only'] = True
    else:
        data['status'] = 'pending'
    row = checks(config, data, binding, **options)['report_output']
    assert row['status'] == 'missing' and row['evidence_ref'] == ''


def test_forged_candidate_payload_cannot_replace_actual_source_ref(tmp_path):
    config, runtime, command, deps, proposal, event, args, run, data, binding = fixture(tmp_path)
    data['strategy_root'] = 'model-invented/candidate'
    assert checks(config, data, binding)['source_binding']['evidence_ref'] == (
        proposal.path / 'after/strategies/eth_ema_trend_1h').relative_to(config.paths.root).as_posix()


def test_candidate_ref_is_not_attested_after_actual_source_edit(tmp_path):
    config, runtime, command, deps, proposal, event, args, run, data, binding = fixture(tmp_path)
    (proposal.path / 'after/strategies/eth_ema_trend_1h/main.py').write_text('# changed')
    assert checks(config, data, binding)['source_binding']['evidence_ref'] == ''
