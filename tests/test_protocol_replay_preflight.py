"""Protocol execution requires more than a candle feed, even with complete bars."""
import pytest

from nerya.core import yaml_io
from nerya.skills.builtin.backtest.scripts.preflight import BacktestPreflightError
from test_backtest_preflight_pipeline import package, replay


@pytest.mark.parametrize('market', [
    'UNISWAP_V3:ETH/USDC', 'PANCAKESWAP_V3:BNB/USDT', 'AAVE_V3:ethereum-USDC',
])
def test_native_ohlcv_preflight_explains_missing_protocol_execution_model(tmp_path, monkeypatch, market):
    root = package(tmp_path)
    manifest_path = root / 'strategy.yml'
    manifest = yaml_io.load(manifest_path)
    manifest['markets'] = [market]
    yaml_io.dump(manifest_path, manifest)
    def forbidden(*args, **kwargs):
        pytest.fail('Protocol capability must be checked before candles or worker execution')
    monkeypatch.setattr(
        'nerya.skills.builtin.backtest.scripts.backtest_run.get_candles', forbidden)
    with pytest.raises(BacktestPreflightError) as failure:
        replay(tmp_path, preflight_only=True)
    blockers = failure.value.receipt['blockers']
    gap = next(row for row in blockers if row['code'] == 'protocol_replay_model_required')
    assert 'candles alone' in gap['message']
    assert market in gap['message']
    assert not list(root.glob('backtests/**/metrics.json'))


def test_protocol_preflight_error_keeps_structured_recovery_and_no_retry():
    from nerya.tools.native.strategy_runtime import _backtest_error
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall
    error = BacktestPreflightError({'blockers':[{
        'code':'protocol_replay_model_required',
        'message':'Protocol history and an execution model are required.'}]})
    result = _backtest_error(ToolCall(name='strategy_backtest',arguments={}), error,
        reason=error.reason,strategy_id='lp_fixture',proposal_id='prp_fixture')
    body = tool_json_data(result)
    assert body['reason'] == 'protocol_replay_model_required'
    assert body['backtest_status'] == 'blocked'
    assert body['next_required_action']['type'] == 'provide_protocol_replay'
    assert body['next_required_action']['preserve_candidate'] is True
    assert result.error.retryable is False
    from nerya.agent.tool_projection import project_tool_results
    projected = project_tool_results([result],
        render_tool_result=lambda result: {'type':'tool_result','content':result.text()},
        rendered_tool_result_text=lambda rendered: rendered.get('content'))
    assert projected.event_blocks[0]['recovery']['reason'] == 'protocol_replay_model_required'
    assert projected.event_blocks[0]['result'] is None


def test_wrong_native_engine_recovers_existing_saved_custom_replay(tmp_path):
    from nerya.tools.native.strategy_runtime import _backtest_error
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall
    root = tmp_path / 'strategies' / 'lp_fixture' / 'scripts'
    root.mkdir(parents=True)
    (root / 'custom_replay.py').write_text('raise RuntimeError("must not execute during discovery")')
    error = BacktestPreflightError({'blockers': [{'code': 'protocol_replay_model_required',
                                               'message': 'Native engine cannot model this protocol.'}]})
    result = _backtest_error(ToolCall(name='strategy_backtest', arguments={
        'strategy_id': 'lp_fixture', 'engine': 'native', 'allow_mock': False}), error,
        reason=error.reason, strategy_id='lp_fixture', proposal_id=None, workspace_root=tmp_path)
    recovery = tool_json_data(result)['next_required_action']
    assert recovery['type'] == 'select_existing_replay'
    assert recovery['arguments_patch'] == {'strategy_id': 'lp_fixture', 'engine': 'auto', 'allow_mock': False}
    assert recovery['preserve_requested_constraints'] is True
    assert recovery['automatic'] is False
