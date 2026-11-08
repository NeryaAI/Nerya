"""Reusable LP configuration must be completed before producer acceptance."""
import json
import socket
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.tools.native.strategy_runtime import strategy_validate_handler
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall
from nerya.strategies.execution_contract import IncompleteStrategyError, require_implemented_candidate
from nerya.strategies.package import load_package_from_dir

pytestmark = pytest.mark.smoke


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('unexpected network')

    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    config = Config(WorkspacePaths(tmp_path), {})
    generated = StrategyCodeGenerator(config.paths).generate(StrategyGenerationRequest(
        strategy_id='lp_candidate', markets=('UNISWAP_V3:ETH/USDC',),
        accounts=('paper_main',), mode='paper', execution_mode='script',
        create_tuning=False, files={'strategy.yml': yaml_io.dumps({
            'id': 'lp_candidate', 'mode': 'paper', 'execution_mode': 'script',
            'markets': ['UNISWAP_V3:ETH/USDC'], 'accounts': ['paper_main'],
            'schedule': {'type': 'none', 'enabled': False},
            'params': {'lp_policy': {}, 'lp_source': {}, 'lp_execution': {}},
        })}), validate=False, initial_state='draft')
    root = generated.proposal.path / 'after/strategies/lp_candidate'
    return config, generated, root


def validate(candidate):
    config, generated, _ = candidate
    result = strategy_validate_handler(ToolCall(name='strategy_validate',
        arguments={'proposal_id': generated.proposal.id}), config=config)
    return result, tool_json_data(result)


def test_empty_reusable_lp_contract_cannot_claim_implementation(candidate):
    _, generated, root = candidate
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    result, report = validate(candidate)
    assert report['ok'] is False, report
    assert result.is_error and result.semantic_success is False
    assert any(b['code'] == 'strategy_implementation_incomplete' for b in report['blockers']), report
    assert report['structural']['status'] == 'failed'
    assert report['behavioral']['status'] == 'not_run'
    assert report['next_required_action']['preserve_candidate'] is True
    saved = json.loads((generated.proposal.path / 'validation_report.json').read_text())
    assert saved['ok'] is False
    assert saved['package_hash'] == report['package_hash']
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    checks = {row['check_id']: row['status'] for row in result.metadata['verification']['checks']}
    assert checks['strategy_contract'] == 'failed'
    assert checks['behavioral_tests'] == 'missing'
    assert checks['source_binding'] == 'passed'
    message = next(b['message'] for b in report['blockers']
                   if b['code'] == 'strategy_implementation_incomplete')
    assert 'Complete saved configuration' in message
    assert 'not historical coverage or live readiness' in message
    assert 'paper validation does not require live credentials' in message


def configured_params():
    # Saved synthetic contract only: no installed wallet, provider, history or secret.
    request = dict(kind='lp_add', component_id='builtin:uniswap', wallet_id='paper-wallet',
                   chain='base', protocol='uniswap_v3', parameters={'pool_id': 'reviewed-pool'})
    return dict(lp_policy=dict(max_age_seconds=60, persistence_seconds=10, cooldown_seconds=100,
                cost_multiple=2, enter_when_empty=True, exit_below_principal_usd=0, collect_min_fees_usd=1),
        lp_source=dict(source_id='declared-lp', wallet_id='paper-wallet', chain='base',
            protocol='uniswap_v3', pool_id='reviewed-pool', token_id=None, token0='wrapped-eth',
            token1='usdc', decimals=[18, 6], fee=500, tick_spacing=10,
            component_binding={'id': 'builtin:uniswap', 'revision': 'saved-revision', 'abi_version': 1},
            account_request=deepcopy(request)),
        lp_execution=dict(operation_key='reviewed-cycle', requests={'lp_add': request}))


def save_params(candidate, params):
    root = candidate[2]
    manifest = yaml_io.load(root / 'strategy.yml')
    manifest['params'] = params
    yaml_io.dump(root / 'strategy.yml', manifest)
    return load_package_from_dir(root)


@pytest.mark.parametrize('section', ['lp_policy', 'lp_source', 'lp_execution'])
@pytest.mark.parametrize('replacement', [{}, None, 'not-a-mapping'])
def test_each_explicit_mapping_is_required(candidate, section, replacement):
    params = configured_params()
    params[section] = replacement
    save_params(candidate, params)
    _, report = validate(candidate)
    assert report['ok'] is False
    assert report['behavioral']['status'] == 'not_run'
    assert any(b['code'] == 'strategy_implementation_incomplete' and section in b['message']
               for b in report['blockers'])


@pytest.mark.parametrize('section,key', [
    ('lp_policy', 'cooldown_seconds'), ('lp_source', 'token_id'),
    ('lp_source', 'account_request'), ('lp_source', 'component_binding'),
    ('lp_execution', 'operation_key'), ('lp_execution', 'requests'),
])
def test_missing_saved_contract_fields_fail_before_behavior(candidate, section, key):
    params = configured_params()
    del params[section][key]
    package = save_params(candidate, params)
    with pytest.raises(IncompleteStrategyError):
        require_implemented_candidate(package)
    _, report = validate(candidate)
    assert any(b['code'] == 'strategy_implementation_incomplete' for b in report['blockers'])
    assert report['behavioral']['status'] == 'not_run'


@pytest.mark.parametrize('plan_only', [False, True])
def test_saved_configuration_passes_without_runtime_readiness(candidate, plan_only):
    params = configured_params()
    if plan_only:
        request = params['lp_execution'].pop('requests')['lp_add']
        params['lp_execution']['rebalance_plan'] = {'exit': {**request, 'kind': 'lp_remove'},
                                                  'entry': request}
    package = save_params(candidate, params)
    require_implemented_candidate(package)
    result, report = validate(candidate)
    assert report['ok'] is True, report
    assert not result.is_error and result.semantic_success is True
    assert report['behavioral']['status'] == 'passed'
    assert report['behavioral']['summary']['passed'] >= 10


@pytest.mark.parametrize('edit', ['comment', 'format', 'authored', 'other-template'])
def test_exact_ast_identity_preserves_authored_overrides(candidate, edit):
    root = candidate[2]
    path = root / 'main.py'
    source = path.read_text()
    if edit == 'comment':
        source = '# completed according to prose only\n' + source
    elif edit == 'format':
        import ast
        source = ast.unparse(ast.parse(source))
    elif edit == 'authored':
        source = source.replace('def run(ctx):', 'def run(ctx):\n    return ctx.result.hold(reason="authored LP observation")')
    else:
        source = (Path(__file__).parents[1] /
            'nerya/skills/builtin/strategy_author/templates/aave_rate_strategy.py.j2').read_text()
    path.write_text(source)
    package = load_package_from_dir(root)
    if edit in {'comment', 'format'}:
        with pytest.raises(IncompleteStrategyError):
            require_implemented_candidate(package)
    else:
        require_implemented_candidate(package)
