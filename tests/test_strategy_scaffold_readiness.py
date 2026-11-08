"""An unchanged disabled scaffold is not an implemented strategy."""
import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.tools.native.strategy_runtime import (strategy_validate_handler,
    strategy_submit_proposal_handler, strategy_backtest_handler)
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke

@pytest.fixture
def candidate(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('unexpected network'))
    config = Config(WorkspacePaths(tmp_path), {})
    gen = StrategyCodeGenerator(config.paths).generate(StrategyGenerationRequest(
        strategy_id='yield_fixture', markets=('AAVE_V3:ETH-USDC',),
        accounts=('paper_main',), mode='paper', execution_mode='script',
        create_tuning=False), validate=False, initial_state='draft')
    root = gen.proposal.path / 'after/strategies/yield_fixture'
    return config, gen, root

def invoke(handler, candidate, **arguments):
    config, gen, _ = candidate
    return handler(ToolCall(name=handler.__name__.removesuffix('_handler'),
        arguments={'proposal_id':gen.proposal.id, **arguments}), config=config)

@pytest.mark.parametrize('comment_only', [False, True])
def test_current_scaffold_validation_cannot_be_used_as_implementation(candidate, comment_only):
    config, gen, root = candidate
    if comment_only:
        path = root/'main.py'
        path.write_text('# changed description, not implementation\n'+path.read_text())
    result = invoke(strategy_validate_handler, candidate)
    report = tool_json_data(result)
    assert result.is_error and result.semantic_success is False
    assert not report['ok']
    assert any(b['code']=='strategy_implementation_incomplete' for b in report['blockers'])
    assert report['behavioral']['status']=='not_run'
    assert (gen.proposal.path/'validation_report.json').exists()

def test_scaffold_submission_keeps_original_draft(candidate):
    config, gen, _ = candidate
    result = invoke(strategy_submit_proposal_handler, candidate)
    assert result.is_error
    assert any(b['code']=='strategy_implementation_incomplete'
               for b in result.error.recovery_hint['validation']['blockers'])
    assert next(p for p in list_proposals(config.paths) if p.id==gen.proposal.id).state=='draft'

@pytest.mark.parametrize('engine', ['native','freeform'])
def test_scaffold_replay_stops_before_data_or_candidate_execution(candidate, engine, monkeypatch):
    from nerya.skills.builtin.backtest.scripts import replay_inputs, backtest_run
    monkeypatch.setattr(replay_inputs, 'prepare_inputs', lambda *a, **k: pytest.fail('data preparation'))
    monkeypatch.setattr(backtest_run, 'create_run_dir', lambda *a, **k: pytest.fail('replay started'))
    result = invoke(strategy_backtest_handler, candidate, engine=engine, allow_mock=False)
    data = tool_json_data(result)
    assert result.is_error and result.semantic_success is False
    assert data['reason']=='strategy_implementation_incomplete'
    assert data['backtest_status']=='blocked'
    assert data['next_required_action']['type']=='implement_candidate'
    assert data['next_required_action']['preserve_candidate'] is True
    assert 'main.py' in data['next_required_action']['files']
    assert not (candidate[2]/'backtests').exists()

def test_source_edits_are_not_inferred_to_be_completed_implementation(candidate):
    from nerya.strategies.execution_contract import incomplete_scaffold
    _, _, root = candidate
    source=(root/'main.py').read_text()
    assert incomplete_scaffold(source)=='templates/aave_strategy.py.j2'
    assert incomplete_scaffold(source.replace('data_ready=False', 'data_ready=ctx.config.params.get("ready", False)')) is None
    assert incomplete_scaffold('def run(ctx):\n    return ctx.result.hold(reason="observation only")\n') is None


def test_scaffold_rejection_leaves_receipts_failed_or_missing(candidate):
    result = invoke(strategy_validate_handler, candidate)
    checks = {row['check_id']: row for row in result.metadata['verification']['checks']}
    assert checks['strategy_contract']['status'] == 'failed'
    assert checks['behavioral_tests']['status'] == 'missing'
    # A current source binding remains true; it never substitutes for behavior.
    assert checks['source_binding']['status'] == 'passed'


def test_incomplete_guard_does_not_reclassify_unrelated_freeform(candidate):
    from nerya.skills.builtin.backtest.scripts.backtest_run import _load_target_package
    from nerya.core import yaml_io
    config, gen, root = candidate
    manifest = yaml_io.load(root/'strategy.yml')
    manifest['entrypoint'] = 'observation.py:run'
    yaml_io.dump(root/'strategy.yml', manifest)
    (root/'observation.py').write_text('def run(ctx):\n    return ctx.result.hold(reason="authored observation")\n')
    package = _load_target_package(config.paths, None, gen.proposal.id)
    assert package.manifest.entrypoint_module == 'observation.py'
