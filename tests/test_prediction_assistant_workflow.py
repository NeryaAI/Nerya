import json

import pytest

from nerya.core import yaml_io
from nerya.tools.native.bootstrap import _wrap_strategy_run_history, _wrap_strategy_run_tick, build_native_tool_deps
from nerya.tools.types import ToolCall
from test_strategy_agent_task_chain import _config, _FakeKernel, _write_agent_task_strategy

pytestmark = pytest.mark.smoke


def test_chat_runs_agent_once_then_reads_its_receipt(tmp_path, monkeypatch):
    from nerya.agent.command_runtime import CommandRuntime

    cfg = _config(tmp_path)
    _write_agent_task_strategy(cfg)
    path = cfg.paths.strategy('macd_agent') / 'strategy.yml'
    manifest = yaml_io.load(path)
    manifest.update(agent_task={'enabled': True}, markets=['POLYMARKET:demo#No'])
    yaml_io.dump(path, manifest)
    deps = build_native_tool_deps(workspace_root=tmp_path, paths=cfg.paths, config=cfg, skill_roots=[])
    deps.active_actor_id = 'practice-author'
    kicks = []
    monkeypatch.setattr(CommandRuntime, 'kick', lambda self, session: kicks.append(session))
    result = _wrap_strategy_run_tick(deps)(ToolCall(name='strategy_run_tick', arguments={
        'strategy_id': 'macd_agent', 'operator': 'not-the-owner'}))
    assert not result.is_error, result.error
    receipt = result.content[0].data
    assert receipt.get('admission_only') is True, receipt.get('reason')
    assert receipt['execution_status'] == 'queued' and len(kicks) == 1
    call = ToolCall(name='strategy_run_history', arguments={'strategy_id': 'macd_agent', 'run_id': receipt['run_id']})
    history = _wrap_strategy_run_history(deps)(call)
    assert history.content[0].data['run']['execution_status'] == 'queued'
    assert len(kicks) == 1
    from nerya.agent.command_runtime import runtime
    from nerya.triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor
    kernel = _FakeKernel()
    monkeypatch.setattr(StrategyAgentTaskExecutor, '_kernel', lambda *args: kernel)
    runtime(cfg)._drain(receipt['session_id'])
    terminal = _wrap_strategy_run_history(deps)(call).content[0].data['run']
    assert terminal['execution_status'] == 'succeeded', terminal
    assert terminal['business_status'] == 'reported'
    assert len(kernel.calls) == len(kicks) == 1
    latest = ToolCall(name='strategy_run_history', arguments={'strategy_id': 'macd_agent', 'include_agent_runs': True})
    assert _wrap_strategy_run_history(deps)(latest).content[0].data['agent_runs'] == [terminal]
    deps.active_actor_id = 'not-the-owner'
    assert _wrap_strategy_run_history(deps)(call).is_error
    assert _wrap_strategy_run_history(deps)(latest).content[0].data['agent_runs'] == []


@pytest.mark.parametrize('denial', ['kill_switch', 'live', 'mode_override'])
def test_denied_agent_tick_never_starts_worker(tmp_path, monkeypatch, denial):
    from nerya.sdk import task_dispatch
    from nerya.strategies.state import StrategyKillSwitch

    cfg = _config(tmp_path)
    _write_agent_task_strategy(cfg)
    path = cfg.paths.strategy('macd_agent') / 'strategy.yml'
    manifest = yaml_io.load(path)
    manifest.update(agent_task={'enabled': True}, mode='live' if denial == 'live' else 'paper')
    yaml_io.dump(path, manifest)
    if denial == 'kill_switch':
        StrategyKillSwitch(cfg.paths, 'macd_agent').assert_(reason='operator stopped')
    deps = build_native_tool_deps(workspace_root=tmp_path, paths=cfg.paths, config=cfg, skill_roots=[])
    monkeypatch.setattr(task_dispatch, 'admit_strategy', lambda *a, **kw: pytest.fail('blocked task admitted'))
    result = _wrap_strategy_run_tick(deps)(ToolCall(name='strategy_run_tick', arguments={
        'strategy_id': 'macd_agent', 'mode_override': 'live' if denial == 'mode_override' else 'paper'}))
    assert result.is_error


def test_edited_candidate_submits_current_review_bundle(tmp_path):
    from nerya.evolution.candidate_bundle import verify_candidate_bundle
    from test_strategy_draft_submit_lane import _cfg, _draft_new, _submit

    paths, cfg = _cfg(tmp_path)
    draft = _draft_new(cfg, markets=['POLYMARKET:demo#No']).content[0].data
    main = paths.root / draft['proposal_paths']['main_path']
    main.write_text(main.read_text() + '\n')
    result = _submit(cfg, draft['proposal_id'])
    assert not result.is_error, result.error
    root = paths.proposals / draft['proposal_id']
    bundle = json.loads((root / 'candidate_bundle.json').read_text())
    assert verify_candidate_bundle(paths.root, root, bundle)['ok']
    main.write_text(main.read_text() + '\n')
    assert not verify_candidate_bundle(paths.root, root, bundle)['ok']


def test_compacted_inspection_preserves_decision_inputs(monkeypatch):
    from nerya.llm.tool_compaction import compact_tool_result
    from nerya.skills.builtin.adapter.scripts import inspect_prediction
    from test_polymarket_connector import FakePolymarketHttp, PolymarketConnector

    connector = PolymarketConnector(transport=FakePolymarketHttp())
    monkeypatch.setattr(inspect_prediction, 'build_connector', lambda *args, **kwargs: connector)
    result = inspect_prediction.run(market='POLYMARKET:event-slug#No')
    envelope = {'stdout': json.dumps(result), 'stdout_json': result, 'exit_code': 0}
    observed = compact_tool_result('script_run', envelope, size_threshold=0).kept['stdout_json']
    notes = observed['notes']
    assert notes['quantity_unit'] == result['book']['quantity_unit'] == 'shares'
    for key in ('bid', 'ask', 'ts_ms', 'min_order_size', 'size_step', 'tick_size'):
        assert notes[key] == result['book'][key]
    assert notes['asset_id'] == result['metadata']['asset_id'] and notes['outcome'] == 'No'
    assert notes['accepting_orders'] == result['metadata']['accepting_orders']
