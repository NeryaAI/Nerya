"""Producer-owned recovery hints only; no replay, runtime, credentials or DB."""
from copy import deepcopy
import json

import pytest

from nerya.skills.builtin.backtest.scripts.mock_ctx import BacktestUnsupportedSurfaceError
from nerya.tools.native.strategy_runtime import _backtest_error
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('target', [{'proposal_id': 'prp_fixture'}, {'strategy_id': 'fixture'}])
def test_lp_cadence_gap_exposes_only_counts_and_saved_target_recovery(target):
    from nerya.skills.builtin.backtest.scripts.lp_state_requirements import LPDataIncomplete

    counts = {'required_state_count': 8641, 'matched_state_count': 5,
              'missing_state_count': 8636, 'conflicting_state_count': 0,
              'interval_count': 8640, 'verified_interval_count': 1,
              'missing_interval_count': 3, 'pending_state_interval_count': 8636}
    error = LPDataIncomplete({'source_revision': 'PRIVATE_REVISION', **counts})
    error.receipt.update(provider='PRIVATE_PROVIDER', body={'raw': 'PRIVATE_BODY'},
                         next_download_args={'proposal_id': 'WRONG_CANDIDATE'})
    before = deepcopy(error.receipt)
    result = _backtest_error(ToolCall(name='strategy_backtest', arguments=target), error,
        reason='backtest_invalid_input', strategy_id=target.get('strategy_id'),
        proposal_id=target.get('proposal_id'))
    body = result.content[0].data
    assert result.is_error and result.error.retryable is False
    assert body['reason'] == 'lp_cadence_data_incomplete'
    assert body['backtest_status'] == 'blocked' and body['ok'] is False
    assert body['diagnostics'] == result.error.recovery_hint['diagnostics'] == counts
    assert result.error.detail['diagnostics'] == counts
    assert '状态' in body['message'] and '区间' in body['message']
    assert len(body['message']) < 100 and 'lp_cadence' not in body['message']
    action = body['next_required_action']
    assert action['tool'] == 'historical_data'
    assert action['arguments'] == {'action': 'inspect', 'dataset_kind': 'lp_state_requirements', **target}
    assert action['skill'] == 'backtest' and action['file'] == 'references/history-data.md'
    assert action['preserve_candidate'] and action['preserve_requested_constraints']
    assert action['preserve_schedule'] and action['automatic'] is False
    assert 'next_download_args' in action['message'] and 'ready' in action['message']
    assert 'Do not repeat the unchanged replay' in action['message']
    assert 'conditional_replay' not in action
    public = result.text() + result.error.message
    assert all(secret not in public for secret in ('PRIVATE_', 'WRONG_CANDIDATE', 'nerya.lp_cadence.v1'))
    assert error.receipt == before
    # Text or a copied receipt alone cannot impersonate the producer exception.
    fake = ValueError('lp_cadence_data_incomplete')
    fake.receipt = before
    untrusted = _backtest_error(ToolCall(name='strategy_backtest'), fake,
        reason='backtest_invalid_input', strategy_id=None, proposal_id=None)
    assert untrusted.content[0].data['reason'] == 'backtest_invalid_input'
    assert 'required_state_count' not in untrusted.content[0].data['diagnostics']


def test_worker_protection_value_error_retains_bounded_diagnostic_in_all_projections():
    from nerya.skills.builtin.backtest.scripts.process_replay import ReplayWorkerError

    message = ('ctx.trading.open_position.protection is unsupported in OHLCV backtests: '
               'invalid stop_loss percentage for long')
    error = ReplayWorkerError({'type': 'BacktestUnsupportedSurfaceError', 'message': message,
        'surface': 'trading.open_position.protection', 'details': {'body': 'PRIVATE_BODY'}})
    result = _backtest_error(ToolCall(name='strategy_backtest'), error, reason=error.reason,
                             strategy_id='fixture', proposal_id='prp_fixture')
    diagnostics = result.content[0].data['diagnostics']
    assert diagnostics == {'type': 'BacktestUnsupportedSurfaceError', 'message': message,
                           'surface': 'trading.open_position.protection'}
    assert result.error.recovery_hint['diagnostics'] == diagnostics
    assert result.error.detail['diagnostics'] == diagnostics
    assert 'invalid stop_loss percentage for long' in result.error.message
    assert 'PRIVATE_BODY' not in result.text() + result.error.message
    assert 'risk controls' in result.content[0].data['next_required_action']['message']
    error.receipt['message'] = 'x' * 2000
    bounded = _backtest_error(ToolCall(name='strategy_backtest'), error, reason=error.reason,
                              strategy_id='fixture', proposal_id='prp_fixture')
    assert len(bounded.error.recovery_hint['diagnostics']['message']) <= 1000


def test_failed_replay_projects_recovery_into_error_text_and_json(tmp_path):
    error = RuntimeError('fixture failure')
    for key, filename in [('run_receipt', 'run.json'), ('failure_path', 'failure.json')]:
        path = tmp_path / filename
        path.write_text('{}')
        setattr(error, key, str(path))
    error.receipt = {'failure_stage': 'replaying', 'request_count': 1,
                     'body': 'PRIVATE_PROVIDER_BODY'}
    result = _backtest_error(ToolCall(name='strategy_backtest'), error,
        reason='runtime_version_changed', strategy_id='fixture',
        proposal_id='prp_fixture', workspace_root=tmp_path)

    body = json.loads(result.text())
    detail = result.error.detail
    assert detail['reason'] == body['reason'] == 'runtime_version_changed'
    assert detail['next_required_action'] == body['next_required_action']
    assert detail['retryable'] is False and result.error.retryable is False
    assert detail['retry_instruction'] == 'Stop; do not repeat the unchanged replay.'
    assert detail['artifacts'] == {'run_receipt': 'run.json', 'failure_path': 'failure.json'}
    assert detail['diagnostics'] == {'failure_stage': 'replaying', 'request_count': 1}
    assert json.loads(result.error.message.split('\n', 1)[1]) == detail
    assert 'PRIVATE_PROVIDER_BODY' not in result.error.message
    assert str(tmp_path) not in result.error.message


def test_subagents_denial_routes_to_skill_and_conditional_same_candidate_replay():
    args = {'proposal_id': 'prp_fixture', 'engine': 'native', 'allow_mock': False,
            'settings': {'start_utc': '2026-09-01', 'end_utc': '2026-10-01', 'fee_bps': 8}}
    before = deepcopy(args)
    result = _backtest_error(ToolCall(name='strategy_backtest', arguments=args),
        BacktestUnsupportedSurfaceError('subagents'), reason='backtest_sdk_unsupported',
        strategy_id='fixture_strategy', proposal_id='prp_fixture')
    body = result.content[0].data
    action = body['next_required_action']
    assert action['tool'] == 'Skill'
    assert action['arguments'] == {'skill': 'backtest', 'file': 'references/replay-capabilities.md'}
    replay = action['conditional_replay']
    assert replay['tool'] == 'strategy_backtest'
    assert replay['arguments_patch'] == {'proposal_id': 'prp_fixture', 'settings': {'agent_replay': True}}
    assert replay['merge_settings'] is True
    assert replay['requires_operator_authorization'] is True and replay['automatic'] is False
    assert 'Do not replace' in action['message'] and 'stub' in action['message']
    assert 'producer' in action['message'] and 'unchanged' in action['message']
    assert result.error.recovery_hint['next_required_action'] == action
    assert result.error.recovery_hint['tool'] == 'Skill'
    assert result.is_error and body['ok'] is False and body['backtest_status'] == 'blocked'
    assert body['proposal_id'] == 'prp_fixture' and args == before
    assert 'BacktestUnsupportedSurfaceError' not in result.error.message
    assert '历史回放不支持 Agent 确认面' in result.error.message


def test_installed_strategy_target_is_preserved_without_inventing_proposal():
    result = _backtest_error(ToolCall(name='strategy_backtest'),
        BacktestUnsupportedSurfaceError('subagents'), reason='backtest_sdk_unsupported',
        strategy_id='installed_fixture', proposal_id=None)
    patch = result.content[0].data['next_required_action']['conditional_replay']['arguments_patch']
    assert patch == {'strategy_id': 'installed_fixture', 'settings': {'agent_replay': True}}


def test_missing_target_returns_reference_only_not_invented_replay_request():
    result = _backtest_error(ToolCall(name='strategy_backtest'),
        BacktestUnsupportedSurfaceError('subagents'), reason='backtest_sdk_unsupported',
        strategy_id=None, proposal_id=None)
    action = result.content[0].data['next_required_action']
    assert action['tool'] == 'Skill' and 'conditional_replay' not in action


@pytest.mark.parametrize('reason,surface', [
    ('backtest_sdk_unsupported', 'news'), ('backtest_sdk_unsupported', 'llm'),
    ('backtest_sdk_unsupported', 'subagents.run'), ('backtest_execution_failed', 'subagents'),
    ('runtime_version_changed', None), ('backtest_data_incomplete', None),
])
def test_other_errors_keep_existing_route_but_expose_it_in_tool_error(reason, surface, tmp_path):
    error = RuntimeError('fixture failure')
    error.surface = surface
    run = tmp_path / 'backtests' / 'run.json'
    run.parent.mkdir()
    run.write_text('{}')
    error.run_receipt = str(run)
    error.receipt = {'failure_stage': 'transport', 'request_count': 124,
                     'body': 'PRIVATE_PROVIDER_BODY'}
    result = _backtest_error(ToolCall(name='strategy_backtest'), error, reason=reason,
                             strategy_id='fixture', proposal_id='prp_fixture', workspace_root=tmp_path)
    action = result.content[0].data['next_required_action']
    assert 'conditional_replay' not in action and action.get('tool') != 'Skill'
    assert result.error.recovery_hint['next_required_action'] == action
    assert result.error.recovery_hint['action'] == action['type']
    assert result.error.recovery_hint['artifacts'] == {'run_receipt': 'backtests/run.json'}
    assert result.error.recovery_hint['diagnostics'] == {'failure_stage': 'transport', 'request_count': 124}
