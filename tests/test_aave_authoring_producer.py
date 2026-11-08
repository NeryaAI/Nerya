"""Offline regression from acmd_ba52eb34aa83d090ae5385af.

Snapshot: three validate envelopes succeeded while all reports failed;
zero submit/skill_run/run_command/backtest calls. No replay_results.json.
Tests use tmp_path, never the snapshot or the live candidate.
"""
from pathlib import Path
import socket
import runpy
from types import SimpleNamespace

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.strategies.validator import validate_proposal_files
from nerya.strategies import ResultBuilder
from nerya.tools.native.strategy_runtime import strategy_draft_proposal_handler, strategy_validate_handler
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke
ASSETS = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('NETWORK_DISABLED')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def generate(tmp_path, *, files=None, markets=('ETH:AaveV3:USDC',)):
    return StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        StrategyGenerationRequest(
            strategy_id='yield_fixture', strategy_class='scalping', mode='paper',
            markets=markets, accounts=('fixture_paper',), create_tuning=False,
            files=files or {},
        ), validate=False, initial_state='draft',
    )


def test_failed_validation_is_a_repairable_tool_error_with_saved_receipt(tmp_path):
    gen = generate(tmp_path, files={
        'main.py': '# @nerya.version 1\ndef run(ctx):\n    return ctx.result.hold(reason="fixture")\n',
        'replay.py': 'from nerya.strategies.research import entrypoint\n',
    })
    result = strategy_validate_handler(ToolCall(name='strategy_validate', arguments={
        'proposal_id': gen.proposal.id,
    }), config=Config(paths=WorkspacePaths(tmp_path), data={}))
    report = result.content[0].data
    assert report['ok'] is False
    assert {'script_documentation_invalid', 'research_sdk_outside_replay'} <= {
        row['code'] for row in report['blockers']}
    assert report['behavioral']['status'] == 'not_run'
    assert (gen.proposal.path / 'validation_report.json').is_file()
    assert result.is_error, 'failed validation must not have a successful tool envelope'
    assert result.semantic_success is False
    assert result.error.kind == ToolErrorKind.EXECUTION_ERROR
    hint = result.error.recovery_hint
    assert hint['action'] == 'repair_candidate_and_validate'
    assert hint['proposal_id'] == gen.proposal.id
    assert hint['blockers'] == report['blockers']
    assert hint['tool_name'] == 'strategy_validate'
    assert hint['preserve_candidate'] is True
    assert report['next_required_action'] == hint
    assert any(c['check_id'] == 'strategy_contract' and c['status'] == 'failed'
               for c in report['verification']['checks'])


@pytest.mark.parametrize('method', ['open_position', 'close_position'])
def test_aave_spot_execution_is_a_structural_blocker(tmp_path, method):
    source = f'def run(ctx):\n    return ctx.trading.{method}(market=ctx.config.markets[0], side="long")\n'
    gen = generate(tmp_path, files={'main.py': source})
    report = validate_proposal_files(strategy_id='yield_fixture', files=gen.files, smoke_test=False)
    blockers = [b for b in report.blockers if b.code == 'execution_family_mismatch']
    assert blockers, report.asdict()
    assert 'templates/aave_supply.py.j2' in blockers[0].message
    assert 'ctx.financial' in blockers[0].message
    assert report.structural['status'] == 'failed'


def test_aave_scaffold_copies_actual_helper_independent_of_prose(tmp_path):
    gen = generate(tmp_path)
    assert gen.files.get('aave_supply.py') == (ASSETS / 'aave_supply.py.j2').read_text()
    assert 'aave_supply_tick' in gen.files['main.py']
    assert 'ctx.trading.' not in gen.files['main.py']
    report = validate_proposal_files(strategy_id='yield_fixture', files=gen.files, smoke_test=False)
    assert not report.ok
    assert [b.code for b in report.blockers] == ['strategy_implementation_incomplete']


def test_draft_producer_returns_protocol_guidance_at_new_candidate_seam(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    result = strategy_draft_proposal_handler(ToolCall(name='strategy_draft_proposal', arguments={
        'strategy_id': 'protocol_fixture', 'markets': ['ETH:AaveV3:USDC'],
        'accounts': ['fixture_paper'], 'create_tuning': False,
    }), config=config)
    assert not result.is_error, result.error
    data = result.content[0].data
    assert data['state'] == 'draft'
    assert data['implementation_status'] == 'scaffold'
    assert 'aave_supply.py' in data['files']
    assert 'aave_supply.py.j2' in ' '.join(data['next_steps'])
    assert 'GENERIC momentum' not in ' '.join(data['next_steps'])
    assert 'data_ready/decision gates are false' in ' '.join(data['next_steps'])


@pytest.mark.parametrize('mode', ['paper', 'backtest'])
def test_generated_entrypoint_uses_copied_helper_without_simulated_fills(tmp_path, monkeypatch, mode):
    gen = generate(tmp_path)
    root = gen.proposal.path / 'after/strategies/yield_fixture'
    monkeypatch.syspath_prepend(str(root))
    # Execute the generated entrypoint with the actual helper and ResultBuilder.
    # No financial/trading facade exists: any accidental call fails the test.
    module = runpy.run_path(str(root / 'main.py'))
    ctx = SimpleNamespace(mode=mode, result=ResultBuilder(), config=SimpleNamespace(params={}))
    result = module['run'](ctx)
    assert result.status.value == 'hold'
    assert result.metadata == {'financial_execution_allowed': False, 'funds_sent': False}


def test_spot_market_is_not_reclassified_by_name_or_description(tmp_path):
    gen = generate(tmp_path, markets=('BINANCE:AAVE/USDT',))
    assert 'aave_supply.py' not in gen.files
    assert 'ctx.trading.open_position' in gen.files['main.py']
    report = validate_proposal_files(strategy_id='yield_fixture', files=gen.files, smoke_test=False)
    assert report.ok, report.asdict()
