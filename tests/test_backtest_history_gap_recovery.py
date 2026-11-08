"""Offline producer/catch-path checks; fixture candles are not market evidence."""
from copy import deepcopy
import socket

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.agent.tool_projection import project_tool_results
from nerya.evolution.patch_proposal import create_proposal
from nerya.skills.builtin.backtest.scripts import backtest_run, data_cache
from nerya.strategies.tool_verification import source_binding
from nerya.tools.native.strategy_runtime import strategy_backtest_handler
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
T = 1700006400


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Network is forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(tmp_path / 'nerya.yml', {'runtime': {'live_trading_enabled': False}})
    proposal = create_proposal(paths, kind='strategy_package_proposal', summary='History fixture',
        extra_files={
            'after/strategies/history_fixture/strategy.yml': yaml_io.dumps({
                'strategy_id': 'history_fixture', 'markets': ['NOHISTORY:SOL/USDC'],
                'mode': 'paper', 'entrypoint': 'main.py:run', 'accounts': ['paper'],
                'execution_mode': 'script', 'schedule': {'type': 'none', 'enabled': False},
                'policy': {'allow_direct_order': False}}),
            'after/strategies/history_fixture/main.py':
                "def run(ctx):\n    return ctx.result.hold(reason='fixture')\n",
        })
    call = ToolCall(name='strategy_backtest', arguments={
        'proposal_id': proposal.id, 'engine': 'native', 'allow_mock': False,
        'settings': {'tf': '1h', 'start_utc': str(T), 'end_utc': str(T + 6 * 3600),
                     'warmup_bars': 0, 'data_mode': 'local'}})
    return Config(paths=paths), call


def assert_failure(result, reason, retryable):
    body = tool_json_data(result)
    assert result.is_error and result.semantic_success is False
    assert body['ok'] is False and body['backtest_status'] == 'blocked'
    assert body['coverage_ok'] is False and body['reason'] == reason
    assert result.error.retryable is retryable
    recovery = result.error.recovery_hint
    assert recovery['reason'] == reason and recovery['safe_to_retry'] is retryable
    assert recovery['preserve_candidate'] and recovery['preserve_requested_constraints']
    assert recovery['next_required_action'] == body['next_required_action']
    projected = project_tool_results([result],
        render_tool_result=lambda item: {'type': 'tool_result', 'content': item.text()},
        rendered_tool_result_text=lambda item: item.get('content'))
    assert projected.event_blocks[0]['recovery']['reason'] == reason
    assert projected.event_blocks[0]['result'] is None
    assert not any(row['status'] == 'passed' for row in result.metadata['verification']['checks']
                   if row['check_id'] != 'source_binding')
    return body


def test_actual_unsupported_producer_is_nonretryable_and_keeps_identity(candidate, monkeypatch):
    config, call = candidate
    before_args, before_source = deepcopy(call.arguments), source_binding(config, call)
    def forbidden(*args, **kwargs):
        pytest.fail('Unsupported history must fail before download')
    monkeypatch.setattr(backtest_run, 'get_candles', forbidden)
    result = strategy_backtest_handler(call, config=config)
    body = assert_failure(result, 'historical_provider_unsupported', False)
    assert body['proposal_id'] == call.arguments['proposal_id']
    assert body['next_required_action']['type'] == 'provide_historical_capability'
    assert 'proxy' in body['next_required_action']['message']
    assert body['diagnostics']['markets'] == ['NOHISTORY:SOL/USDC']
    assert body['diagnostics']['start'] == T
    assert body['diagnostics']['end'] == T + 6 * 3600
    assert body['phase'] == 'preflight'
    assert call.arguments == before_args and source_binding(config, call) == before_source


def test_incomplete_download_uses_preparation_action_and_synchronized_recovery(candidate, monkeypatch):
    config, call = candidate
    error = data_cache.NoHistoricalDataError('download timeout')
    error.replay_phase = 'preparing_data'
    error.receipt = {'status': 'incomplete', 'requests': 3, 'rows': 0, 'missing_bars': 6}
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', fail)
    body = assert_failure(strategy_backtest_handler(call, config=config), 'backtest_data_incomplete', True)
    assert body['diagnostics'] == error.receipt
    assert body['next_required_action']['type'] == 'prepare_historical_data'
    assert 'missing ranges' in body['next_required_action']['message']


def test_actual_empty_local_history_is_incomplete_not_a_capability_gap(candidate):
    config, call = candidate
    path = config.paths.root / 'evolution/proposals' / call.arguments['proposal_id'] / 'after/strategies/history_fixture/strategy.yml'
    manifest = yaml_io.load(path)
    manifest['markets'] = ['BYBIT:SOLUSDC']
    yaml_io.dump(path, manifest)
    body = assert_failure(strategy_backtest_handler(call, config=config), 'backtest_data_incomplete', True)
    assert body['phase'] == 'preparing_data'
    assert body['next_required_action']['type'] == 'prepare_historical_data'
    assert body['diagnostics']['downloads']['BYBIT:SOLUSDC']['1h']['requests'] == 0


def test_typed_preflight_history_gap_keeps_recovery_ranges_and_candidate(candidate, monkeypatch):
    from nerya.skills.builtin.backtest.scripts.preflight import BacktestPreflightError

    config, call = candidate
    call.arguments['settings'].update(start_utc=str(T + 50 * 3600),
        end_utc=str(T + 770 * 3600), warmup_bars=50)
    receipt = {
        'blockers': [{'code': 'incomplete_history', 'message': '8 of 770 closed candles missing'}],
        'requested_start_utc': str(T + 50 * 3600),
        'requested_end_utc': str(T + 770 * 3600),
        'requested_window_complete': False, 'coverage_policy': 'strict', 'end_exclusive': True,
        'datasets': [{'market': 'BINANCE:BTCUSDT', 'timeframe': '1h',
            'start': T, 'end': T + 770 * 3600, 'end_exclusive': True,
            'rows': 762, 'expected_bars': 770, 'missing_bars': 8, 'complete': False,
            'missing_ranges': [{'start': T + 762 * 3600, 'end': T + 770 * 3600, 'bars': 8}],
            'download': {'job_id': 'history_fixture', 'resumable': True,
                         'errors': [{'reason': 'unbounded provider content'}]}}],
    }
    error = BacktestPreflightError(receipt)
    error.reason = 'backtest_data_incomplete'
    error.replay_phase = 'validating_data'
    before_args, before_source, before_receipt = deepcopy(call.arguments), source_binding(config, call), deepcopy(receipt)
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise error
    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', fail)
    result = strategy_backtest_handler(call, config=config)
    body = tool_json_data(result)
    assert result.is_error and result.semantic_success is False
    assert body['reason'] == 'backtest_data_incomplete' and body['backtest_status'] == 'blocked'
    assert body['proposal_id'] == call.arguments['proposal_id'] and body['phase'] == 'validating_data'
    action = body['next_required_action']
    assert action['type'] == 'prepare_historical_data' and action['tool'] == 'historical_data'
    assert 'action=inspect' in action['message'] and 'action=download' in action['message']
    assert action['preserve_candidate'] and action['preserve_requested_constraints']
    assert action['automatic'] is False
    assert result.error.retryable is None
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    loop = object.__new__(WorkspaceNativeAgentLoop)
    assert 'and stop' not in loop._retry_directive_for(result.error.kind.value, result)
    assert result.error.recovery_hint['next_required_action'] == action
    diagnostics = body['diagnostics']
    assert diagnostics == result.error.detail['diagnostics'] == result.error.recovery_hint['diagnostics']
    assert diagnostics['requested_start_utc'] == receipt['requested_start_utc']
    assert diagnostics['requested_end_utc'] == receipt['requested_end_utc']
    dataset = diagnostics['datasets'][0]
    assert dataset['start'] == T and dataset['end'] == T + 770 * 3600
    assert dataset['missing_ranges'] == receipt['datasets'][0]['missing_ranges']
    assert dataset['expected_bars'] == 770 and dataset['missing_bars'] == 8
    assert dataset['download'] == {'job_id': 'history_fixture', 'resumable': True}
    projected = project_tool_results([result],
        render_tool_result=lambda item: {'type': 'tool_result', 'content': item.text()},
        rendered_tool_result_text=lambda item: item.get('content'))
    assert projected.event_blocks[0]['recovery']['next_required_action'] == action
    assert len(calls) == 1 and call.arguments == before_args
    assert source_binding(config, call) == before_source and receipt == before_receipt


@pytest.mark.parametrize('market,action', [
    ('BYREAL_ONCHAIN:solana', 'repair_concrete_market_and_rerun'),
    ('BYREAL_ONCHAIN:solana:pool_fixture', 'custom_replay_or_operator_approval'),
    ('NOHISTORY:SOL/USDC', 'report_data_gap'),
])
def test_legacy_missing_history_keeps_event_replay_distinctions(candidate, monkeypatch, market, action):
    config, call = candidate
    path = config.paths.root / 'evolution/proposals' / call.arguments['proposal_id'] / 'after/strategies/history_fixture/strategy.yml'
    manifest = yaml_io.load(path)
    manifest['markets'] = [market]
    yaml_io.dump(path, manifest)
    def fail(**kwargs):
        # Text is deliberately misleading; it must never classify capability.
        raise data_cache.NoHistoricalDataError('unsupported historical data venue for JUPITER:SOL/USDC')
    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', fail)
    result = strategy_backtest_handler(call, config=config)
    body = tool_json_data(result)
    assert body['reason'] == 'no_historical_data'
    assert body['next_required_action']['type'] == action
    assert not result.is_error and result.semantic_success is False
    assert result.error is None
    assert body['next_required_action']['safe_to_retry'] is False
    assert data_cache.NoHistoricalDataError('legacy').retryable is None


def test_typed_cause_wins_over_phase_and_exception_text(candidate, monkeypatch):
    config, call = candidate
    error = data_cache.NoHistoricalDataError('opaque fixture',
        reason='historical_provider_unsupported', retryable=False, receipt={'markets': ['OTHER:SOL/USDC']})
    error.replay_phase = 'preparing_data'
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', fail)
    body = assert_failure(strategy_backtest_handler(call, config=config), 'historical_provider_unsupported', False)
    assert body['next_required_action']['type'] == 'provide_historical_capability'


def test_empty_cache_produces_typed_incomplete_receipt(tmp_path):
    with pytest.raises(data_cache.NoHistoricalDataError) as caught:
        data_cache.get_candles('BYBIT:SOLUSDC', '1h', T, T + 6 * 3600 - 1,
                               tmp_path / 'cache', offline=True)
    assert caught.value.reason == 'backtest_data_incomplete'
    assert caught.value.retryable is True
    assert caught.value.receipt['missing_bars'] == 6


def test_normal_success_keeps_result_and_evidence_gate(candidate, monkeypatch):
    config, call = candidate
    success = {'ok': True, 'strategy_id': 'history_fixture', 'proposal_id': call.arguments['proposal_id'],
               'coverage_ok': True, 'metrics': {'total_return_pct': 0.25}}
    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', lambda **kwargs: deepcopy(success))
    result = strategy_backtest_handler(call, config=config)
    body = tool_json_data(result)
    assert not result.is_error and result.semantic_success is True
    assert body['ok'] is True and body['coverage_ok'] is True
    assert body['metrics'] == success['metrics'] and 'next_required_action' not in body
    # A stubbed success without durable outputs cannot become verified performance.
    assert not any(row['status'] == 'passed' for row in result.metadata['verification']['checks']
                   if row['check_id'] in {'historical_performance', 'report_output', 'data_coverage', 'replay_source'})
