"""Expanded replay reports require saved producer visuals, including empty trades."""
import json
from contextlib import closing

import pytest

from nerya.agent.tool_projection import project_tool_results
from nerya.db.sqlite import connect
from nerya.skills.builtin.backtest.scripts.freeform_run import _render_chart
from nerya.skills.builtin.backtest.scripts.run_state import atomic_json
from nerya.strategies.tool_verification import attach_strategy_checks
from nerya.task_delivery import completion_is_current, read_delivery, update_delivery
from nerya.tools.native import delivery as native_delivery
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall, ToolResult
from test_delivery_producer_outputs import fixture as replay_fixture
from test_request_scope import TEXT

pytestmark = pytest.mark.smoke


def setup_replay(tmp_path, expanded=True, trades=None):
    env = replay_fixture(tmp_path, text=TEXT + (' 请把回测报告展开，包含图表和成交明细。' if expanded else ''))
    config, _, _, _, _, _, _, run, data, binding = env
    # Real producer format; unit-bearing mode avoids optional chat/bulk blocks.
    chart = _render_chart(out_dir=run.parent, strategy_id='eth_ema_trend_1h',
        backtest_ts=run.parent.name, equity_rows=[{'ts': 1790000000, 'equity': 1000},
            {'ts': 1790003600, 'equity': 1000}], trade_rows=trades or [],
        metrics={'accounting_currency': 'USDC', 'total_trades': len(trades or [])},
        workspace_root=config.paths.root)
    atomic_json(run.with_name('chart.json'), chart)
    return env


def produce(env):
    config, runtime, command, _, _, _, _, _, data, binding = env
    result = attach_strategy_checks(ToolResult.from_json(tool_use_id='visual-replay',
        name='strategy_backtest', data=data), before=binding, after=binding, kind='backtest', config=config)
    block = project_tool_results([result], render_tool_result=lambda _: {'content': []},
        rendered_tool_result_text=lambda _: '[receipt]').event_blocks[0]
    runtime.store.record_event(command['command_id'], {**block, 'kind': 'tool.complete',
        'event_id': 'visual-replay', 'session_id': command['session_id'], 'turn_id': command['turn_id']})
    return result


def prepare_delivery(env):
    config, _, command, deps, _, _, args, _, data, _ = env
    # Model omits the visualization requirement; admitted text still owns it.
    args['stages'] = [{'id': 'deliver', 'state': 'completed'}]
    result = native_delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments=args), deps=deps)
    assert not result.is_error, result.text()
    row = tool_json_data(result)
    return update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], artifacts=[
            {'artifact_id': 'report', 'ref': str(data['report_path']).removeprefix(str(config.paths.root) + '/')},
            {'artifact_id': 'run_receipt', 'ref': str(data['run_receipt']).removeprefix(str(config.paths.root) + '/')}])


def finalize(env, row):
    result = native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=env[3])
    assert not result.is_error, result.text()
    return tool_json_data(result)


@pytest.mark.parametrize('trades', [[], [{'ts': 1790003600, 'side': 'buy', 'price': 10}]])
def test_saved_producer_chart_and_trade_table_allow_completion(tmp_path, trades):
    env = setup_replay(tmp_path, trades=trades)
    result = produce(env)
    check = next(row for row in result.metadata['verification']['checks'] if row['check_id'] == 'visualization_output')
    assert check['status'] == 'passed' and check['required'] is True
    assert check['evidence_ref'].endswith('/chart.json')
    final = finalize(env, prepare_delivery(env))
    assert final['completion']['allowed'], final['completion']
    assert completion_is_current(env[0].paths, final)


def damage_output(env, damage):
    run = env[7]
    chart_path = run.with_name('chart.json')
    chart = json.loads(chart_path.read_text())
    if damage == 'missing_chart':
        chart_path.unlink()
        # Tool/UI flags cannot replace persisted producer output.
        env[8].update(chart_blocks=[{'rendered': True}], has_equity_curve=True, has_trade_details=True)
        return
    if damage == 'invalid_json':
        chart_path.write_text('{')
        return
    if damage == 'symlink_chart':
        other = chart_path.with_name('other.json')
        chart_path.rename(other)
        chart_path.symlink_to(other)
        return
    if damage.startswith('run_'):
        receipt = json.loads(run.read_text())
        if damage == 'run_missing_identity':
            receipt.pop('backtest_ts')
        elif damage == 'run_wrong_identity':
            receipt['backtest_ts'] = 'other-run'
        elif damage == 'run_missing_path':
            receipt.pop('run_path')
        else:
            receipt['run_path'] = str(run.parent.parent / 'other-run')
        atomic_json(run, receipt)
        return
    if damage == 'missing_meta':
        chart.pop('meta')
    elif damage == 'missing_chart_identity':
        chart['meta'].pop('backtest_ts')
    elif damage == 'wrong_chart_identity':
        chart['meta']['backtest_ts'] = 'other-run'
    elif damage == 'wrong_strategy':
        chart['meta']['strategy_id'] = 'other'
    elif damage == 'bad_panels':
        chart['panels'] = {}
    elif damage == 'bad_series':
        chart['panels'][0]['series'] = ['not-a-series']
    elif damage == 'empty_curve':
        chart['panels'][0]['series'][0]['data'] = []
    elif damage == 'bad_number':
        chart['panels'][0]['series'][0]['data'][0]['value'] = 'NaN'
    elif damage == 'nonfinite_number':
        chart_path.write_text(chart_path.read_text().replace('1000.0', 'NaN', 1))
        return
    elif damage == 'bad_time':
        chart['panels'][0]['series'][0]['data'][0]['time'] = True
    elif damage == 'reversed_time':
        chart['panels'][0]['series'][0]['data'].reverse()
    elif damage == 'missing_tables':
        chart.pop('tables')
    elif damage == 'missing_trades':
        chart['tables'] = []
    elif damage == 'bad_columns':
        chart['tables'][0]['columns'] = ['side', 'side']
    elif damage == 'bad_row':
        chart['tables'][0].update(columns=['side'], rows=[{'side': 'buy'}])
    elif damage == 'wrong_row_width':
        chart['tables'][0].update(columns=['side'], rows=[['buy', 10]])
    elif damage == 'nested_cell':
        chart['tables'][0].update(columns=['side'], rows=[[{'side': 'buy'}]])
    atomic_json(chart_path, chart)


@pytest.mark.parametrize('damage', ['missing_chart', 'invalid_json', 'symlink_chart',
    'run_missing_identity', 'run_wrong_identity', 'run_missing_path', 'run_wrong_path',
    'missing_meta', 'missing_chart_identity', 'wrong_chart_identity', 'wrong_strategy',
    'bad_panels', 'bad_series', 'empty_curve', 'bad_number', 'nonfinite_number', 'bad_time', 'reversed_time',
    'missing_tables', 'missing_trades', 'bad_columns', 'bad_row', 'wrong_row_width', 'nested_cell'])
def test_invalid_visualization_blocks_native_finalize(tmp_path, damage):
    env = setup_replay(tmp_path)
    damage_output(env, damage)
    result = produce(env)
    check = next(row for row in result.metadata['verification']['checks'] if row['check_id'] == 'visualization_output')
    assert check['status'] == 'missing' and not check['evidence_ref']
    final = finalize(env, prepare_delivery(env))
    assert final['completion']['allowed'] is False
    assert 'check_missing:visualization_output' in final['completion']['reasons']


def test_chart_corruption_after_receipt_is_rechecked_at_finalize_and_later_goal(tmp_path):
    env = setup_replay(tmp_path)
    produce(env)
    final = finalize(env, prepare_delivery(env))
    assert final['completion']['allowed']
    damage_output(env, 'wrong_chart_identity')
    assert not completion_is_current(env[0].paths, final)
    assert finalize(env, final)['completion']['allowed'] is False


def test_expanded_report_cannot_complete_with_legacy_receipts_only(tmp_path):
    env = setup_replay(tmp_path)
    config, _, command = env[:3]
    # Simulate historical host metadata before visualization checks existed.
    with closing(connect(config.paths.db)) as con, con:
        rows = con.execute('SELECT id,payload_json FROM agent_command_events WHERE command_id=?',
                           (command['command_id'],)).fetchall()
        for row in rows:
            event = json.loads(row['payload_json'])
            if 'verification' in event:
                event['verification']['checks'] = [check for check in event['verification']['checks']
                    if check['check_id'] != 'visualization_output']
                con.execute('UPDATE agent_command_events SET payload_json=? WHERE id=?', (json.dumps(event), row['id']))
    final = finalize(env, prepare_delivery(env))
    assert final['completion']['allowed'] is False
    assert 'check_missing:visualization_output' in final['completion']['reasons']
    assert not any(check['check_id'] == 'visualization_output' for check in final['checks'])


def test_nonexpanded_legacy_replay_does_not_require_chart(tmp_path):
    env = setup_replay(tmp_path, expanded=False)
    damage_output(env, 'missing_chart')
    assert finalize(env, prepare_delivery(env))['completion']['allowed']


def test_model_cannot_forge_visualization_pass(tmp_path):
    env = setup_replay(tmp_path)
    prepare_delivery(env)
    config, _, command, deps = env[:4]
    row = read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])
    result = native_delivery.delivery_update_handler(ToolCall(name='delivery_update', arguments={
        'expected_revision': row['delivery_revision'],
        'checks': [{'check_id': 'visualization_output', 'status': 'passed'}]}), deps=deps)
    assert result.is_error
    assert finalize(env, row)['completion']['allowed'] is False
