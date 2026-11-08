"""Offline executable reference check; not producer or live acceptance."""
import json
from pathlib import Path
import socket
import sys
from types import ModuleType

from jsonschema import validate
import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.skills.builtin.backtest.scripts.replay_inputs import read_manifest
from nerya.tools.native.strategy_runtime import STRATEGY_DRAFT_PROPOSAL_SCHEMA
from test_defillama_data_api import PID

pytestmark = pytest.mark.smoke
REFERENCE = (Path(__file__).resolve().parents[1]
             / 'nerya/skills/builtin/strategy_author/references/rate-policy.md')


def test_documented_compact_bundle(tmp_path, monkeypatch):
    def offline(*args, **kwargs):
        pytest.fail('Network is outside this offline reference contract')

    monkeypatch.setattr(socket.socket, 'connect', offline)
    monkeypatch.setattr(socket, 'create_connection', offline)
    text = REFERENCE.read_text()
    fence = chr(96) * 3
    example = text.split(fence + 'python', 1)[1].split(fence, 1)[0]
    replacements = dict(requested_canonical_market='AAVE_V3:ethereum-USDC',
        existing_paper_account='fixture_paper', discovered_pool_id=PID,
        catalog_project='aave-v3', catalog_chain='Ethereum', catalog_symbol='USDC',
        requested_start_utc='2040-01-01T00:00:00Z', requested_end_utc='2040-01-31T00:00:00Z')
    for key, value in replacements.items():
        example = example.replace('<' + key + '>', value)
    calls = []
    exec(compile(example, str(REFERENCE), 'exec'),
         {'strategy_draft_proposal': lambda **args: calls.append(args)})
    assert len(calls) == 1
    args = calls[0]
    validate(args, STRATEGY_DRAFT_PROPOSAL_SCHEMA)
    assert set(args['files']) == {'strategy.yml', 'replay_inputs.json', 'tests/test_contract.py'}
    assert '<' not in json.dumps(args)
    result = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        StrategyGenerationRequest(**args), validate=False, initial_state='draft')
    templates = REFERENCE.parents[1] / 'templates'
    for output, template in [('main.py', 'aave_rate_strategy'),
                             ('aave_rate_policy.py', 'aave_rate_policy'),
                             ('aave_supply.py', 'aave_supply')]:
        assert result.files[output] == (templates / (template + '.py.j2')).read_text()
    candidate = result.proposal.path / 'after/strategies/rate_candidate'
    assert read_manifest(candidate) == json.loads(args['files']['replay_inputs.json'])
    policy = ModuleType('aave_rate_policy')
    monkeypatch.setitem(sys.modules, 'aave_rate_policy', policy)
    path = candidate / 'aave_rate_policy.py'
    exec(compile(path.read_text(), str(path), 'exec'), policy.__dict__)
    path = candidate / 'tests/test_contract.py'
    namespace = {'__file__': str(path)}
    exec(compile(path.read_text(), str(path), 'exec'), namespace)
    namespace['test_entry_exit_and_cash_limits']()
    namespace['test_missing_and_stale_rates']()
    words = ' '.join(text.split())
    assert 'BEFORE the generic incremental scaffold' in words
    assert 'For an EXISTING candidate, keep the same proposal_id and staged root' in words
    assert 'Omit allow_outside_conversation' in words
    assert 'performance_evidence:false' in words and 'financial_execution_allowed:false' in words
