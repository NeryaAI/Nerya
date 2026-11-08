"""Regressions for wrong-resource acceptance (acmd_d9a9f990 / atmsg_b178).

Real draft/submit/delivery producers, isolated tmp_path storage. Replay receipts
are explicit offline fixtures; no historical run, live DB or network is used.
Candidate execution is an explicit fixture seam; scope producers are real.
"""
import json
from pathlib import Path
import socket

import pytest

from nerya.agent.tool_projection import project_tool_results
from nerya.core import yaml_io
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.strategies.tool_verification import attach_strategy_checks, source_binding
from nerya.task_delivery import read_delivery, update_delivery
from nerya.tools.native import delivery as native_delivery, strategy_runtime
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall, ToolResult
from test_task_delivery import _runtime, _deps, _persist_replay_outputs
from test_request_scope import FakeGateway, install_fake, output, fake_execution
from nerya.strategies import request_scope as scope

pytestmark = pytest.mark.smoke
SKILL = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author'
ORIGINAL = '帮我做一个 Jupiter SOL/USDC 趋势策略，回测最近30天，不启用交易。'


@pytest.fixture
def wrong_candidate(tmp_path, monkeypatch, fake_execution):
    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')
    def offline(*args, **kwargs):
        pytest.fail('NETWORK_DISABLED')
    monkeypatch.setattr(socket.socket, 'connect', offline)
    monkeypatch.setattr(socket, 'create_connection', offline)
    config, runtime = _runtime(tmp_path)
    command = runtime.submit({
        'command_id': 'scope-command', 'session_id': 'scope-session',
        '_auth_actor_id': 'operator', '_auth_scopes': ['execute:strategy'], 'request': {'payload': {'text': ORIGINAL}},
    }, start=False)['command']
    deps = _deps(config, command)
    config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model', 'allowed_tasks': ['agent.loop']}}
    config.data['agent']['native']['tier'] = 'medium'
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='running' WHERE command_id=?", (command['command_id'],))
    gateway = FakeGateway(lambda docs: output(docs[0], venue='Jupiter', base='SOL', quote='USDC', timeframe=None, window='30天'))
    install_fake(monkeypatch, gateway)
    fence = chr(96) * 3
    start_args = json.loads(SKILL.joinpath('SKILL.md').read_text().split(fence + 'json', 1)[1].split(fence, 1)[0])
    started = native_delivery.delivery_start_handler(
        ToolCall(name='delivery_start', arguments=start_args), deps=deps)
    assert not started.is_error, started.text()
    assert tool_json_data(started)['requested_scope']['summary'] == ORIGINAL
    sid = 'jupiter_sol_trend'
    files = {name: (SKILL / 'templates' / asset).read_text() for name, asset in (
        ('main.py', 'ema_cross.py.j2'), ('strategy_execution.py', 'strategy_execution.py.j2'),
        ('tests/test_contract.py', 'ema_cross_contract.py.j2'))}
    files['strategy.yml'] = yaml_io.dumps({
        'title': 'Jupiter SOL/USDC 趋势策略', 'markets': ['BINANCE:SOLUSDT'],
        'mode': 'paper', 'timeframe': '1h', 'backtest': {'window_days': 30},
    })
    # Existing candidate predates command-scope enforcement; standalone codegen
    # remains legal. Submit/finalize must still refuse its wrong identity.
    generated = StrategyCodeGenerator(config.paths).generate(StrategyGenerationRequest(
        strategy_id=sid, strategy_class='trend', markets=('BINANCE:SOLUSDT',),
        accounts=('paper_main',), create_tuning=False, files=files), initial_state='draft')
    assert generated.validation.ok, generated.validation.asdict()
    proposal = generated.proposal
    scope.ensure_contract(config)
    binding = source_binding(config, ToolCall(name='strategy_validate', arguments={'proposal_id': proposal.id}))
    attach_strategy_checks(ToolResult.from_json(tool_use_id='fixture-validation', name='strategy_validate',
        data=generated.validation.asdict()), before=binding, after=binding, kind='validation', config=config)
    root = proposal.path / 'after/strategies' / sid
    assert yaml_io.load(root / 'strategy.yml')['markets'] == ['BINANCE:SOLUSDT']
    return config, runtime, command, deps, proposal, sid, start_args


def test_submit_cannot_accept_wrong_market_without_verified_request_scope(wrong_candidate):
    config, _, _, _, proposal, _, _ = wrong_candidate
    result = strategy_runtime.strategy_submit_proposal_handler(ToolCall(
        name='strategy_submit_proposal', arguments={'proposal_id': proposal.id}), config=config)
    data = tool_json_data(result)
    assert result.is_error, {
        'problem': 'wrong venue and quote submitted without a verified request scope',
        'state': data.get('state'), 'validation_ok': data.get('validation', {}).get('ok'),
    }
    assert next(p for p in list_proposals(config.paths) if p.id == proposal.id).state == 'draft'
    assert result.error.recovery_hint['reason'] == 'request_scope_mismatch'
    assert {d['field'] for d in result.error.recovery_hint['differences']} >= {'venue', 'quote'}


def test_finalize_requires_scope_even_when_all_existing_checks_pass(wrong_candidate):
    config, runtime, command, deps, proposal, sid, start_args = wrong_candidate
    binding = source_binding(config, ToolCall(name='strategy_backtest', arguments={
        'strategy_id': sid, 'proposal_id': proposal.id}))
    replay = {
        'ok': True, 'backtest_status': 'completed', 'verdict': 'PASS',
        'requested_window_complete': True, 'performance_evidence': True,
        'run_receipt': 'backtests/scope-fixture/run.json',
        'replay': {'errors': 0, 'sdk_errors': 0, 'order_accounting_ok': True},
        'provenance': {'data_kind': 'historical', 'source_revision': binding['revision'],
            'source_changed_during_run': False, 'datasets': [{'market': 'BINANCE:SOLUSDT',
                'timeframe': '1h', 'rows': 770, 'first_ts': 1, 'last_ts': 2768401,
                'duplicate_timestamps': 0, 'out_of_order': False, 'sha256': 'd' * 64}]},
    }
    _persist_replay_outputs(config, proposal, sid, replay)
    result = attach_strategy_checks(ToolResult.from_json(tool_use_id='scope-replay',
        name='strategy_backtest', data=replay), before=binding, after=binding, kind='backtest', config=config)
    block = project_tool_results([result], render_tool_result=lambda _: {'content': []},
        rendered_tool_result_text=lambda _: '[fixture receipt]').event_blocks[0]
    runtime.store.record_event(command['command_id'], {**block, 'kind': 'tool.complete',
        'event_id': 'scope-replay', 'session_id': command['session_id'], 'turn_id': command['turn_id']})
    row = read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])
    checks = {c['check_id']: c for c in row['checks']}
    for name in ('source_binding', 'strategy_contract', 'behavioral_tests', 'historical_run',
                 'data_coverage', 'historical_data', 'replay_source', 'execution_integrity', 'report_output'):
        assert checks[name]['status'] == 'passed', checks[name]
    candidate = (proposal.path / 'after/strategies' / sid).relative_to(config.paths.root).as_posix()
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'],
        stages=[{'id': stage['id'], 'state': 'completed'} for stage in start_args['stages']],
        artifacts=[{'artifact_id': name, 'ref': ref} for name, ref in (
            ('candidate', candidate), ('report', replay['report_path']), ('run_receipt', replay['run_receipt']))])
    final = tool_json_data(native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=deps))
    assert final['requested_scope']['summary'] == ORIGINAL
    assert final['completion']['allowed'] is False, final['completion']
    assert 'check_failed:request_scope_match' in final['completion']['reasons']
