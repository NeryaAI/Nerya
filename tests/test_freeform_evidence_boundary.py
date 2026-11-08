"""Script-authored numbers cannot attest coverage or historical execution."""
import json

import pytest

from nerya.skills.builtin.backtest.scripts.freeform_run import _build_metrics, run_freeform_backtest
from test_freeform_sandbox import package, macos

pytestmark = pytest.mark.smoke


def test_authored_coverage_claim_is_not_host_verification(tmp_path):
    metrics = _build_metrics(payload={'coverage_ok': True, 'requested_window_complete': True,
        'performance_evidence': True, 'verdict': 'PASS'},
        equity_rows=[{'ts': 1, 'equity': 100}, {'ts': 2, 'equity': 101}], trade_rows=[],
        strategy_id='fixture', proposal_id=None, script_path=tmp_path / 'script.py')
    assert metrics['coverage_ok'] is None
    assert metrics['requested_window_complete'] is None
    assert metrics['performance_evidence'] is False
    assert metrics['verdict'] == 'RESEARCH'


@macos
def test_actual_freeform_result_and_saved_metrics_keep_verification_gap(tmp_path):
    package(tmp_path, "import json\nprint(json.dumps({'equity_curve': [100, 101], 'trades': [], 'coverage_ok': True, 'performance_evidence': True}))\n")
    result = run_freeform_backtest(strategy_id='sandbox_test', workspace=tmp_path)
    from pathlib import Path
    for data in [result, json.loads(Path(result['metrics_path']).read_text()),
                 json.loads(Path(result['result_path']).read_text())]:
        assert data['coverage_ok'] is None
        assert data['requested_window_complete'] is None
        assert data['performance_evidence'] is False
        assert 'unverified' in data['coverage_message']
    assert any('not verified' in value for value in result['limitations'] if isinstance(value, str))


@macos
def test_custom_research_delivers_partial_without_scope_or_full_history_gate(tmp_path):
    from test_task_delivery import _runtime, _command, _deps
    from nerya.tools.native import delivery, strategy_runtime
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall

    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    package(tmp_path, "import json\nprint(json.dumps({'equity_curve': [100, 101], 'trades': []}))\n")
    started = delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments={
        'skill_id': 'backtest', 'workflow_id': 'strategy_replay',
        'requested_scope': {'summary': 'Exploratory calculation; fixture data, not market performance'},
        'stages': [{'id': 'research', 'state': 'in_progress'}],
    }), deps=deps)
    assert not started.is_error, started.text()
    result = strategy_runtime.strategy_backtest_handler(ToolCall(name='strategy_backtest', arguments={
        'strategy_id': 'sandbox_test', 'engine': 'freeform',
    }), config=config)
    assert not result.is_error and result.semantic_success is True, result.text()
    data = tool_json_data(result)
    checks = {row['check_id']: row for row in data['verification']['checks']}
    assert checks['historical_data']['status'] == 'missing'
    assert checks['historical_data']['required'] is False
    assert data['performance_evidence'] is False
    assert data['requested_window_complete'] is None
    updated = delivery.delivery_update_handler(ToolCall(name='delivery_update', arguments={
        'expected_revision': data['delivery_revision'],
        'stages': [{'id': 'research', 'state': 'completed'}],
    }), deps=deps)
    assert not updated.is_error, updated.text()
    final = delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize', arguments={
        'expected_revision': tool_json_data(updated)['delivery_revision'],
    }), deps=deps)
    completion = tool_json_data(final)['completion']
    assert (completion['allowed'], completion['status'], completion['reasons']) == (True, 'partial', [])
