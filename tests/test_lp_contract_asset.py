"""Installed LP test asset runs against real SDKs in a disposable candidate."""
from pathlib import Path
import json
import sys

import pytest

from nerya.core import yaml_io
from nerya.strategies.candidate_validation import run_candidate_validation
from nerya.strategies.execution_contract import IncompleteStrategyError, require_implemented_candidate
from nerya.strategies.package import load_package_from_dir
from nerya.strategies.validator import _validate_loaded

pytestmark = pytest.mark.smoke
TEMPLATES = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / 'candidate'
    (root / 'tests').mkdir(parents=True)
    names = {'main.py': 'lp_range_strategy', 'risk_rules.py': 'risk_rules',
             'lp_range_policy.py': 'lp_range_policy', 'lp_rebalance.py': 'lp_rebalance',
             'tests/test_contract.py': 'lp_range_contract'}
    for target, template in names.items():
        (root / target).write_text((TEMPLATES / (template + '.py.j2')).read_text())
    yaml_io.dump(root / 'strategy.yml', dict(id='synthetic_lp_contract', mode='paper',
        execution_mode='script', markets=['UNISWAP_V3:ETH/USDC'], accounts=['synthetic-only'],
        schedule={'type': 'none', 'enabled': False},
        params={'lp_source': {}, 'lp_policy': {}, 'lp_execution': {}}))
    return root


def test_asset_does_not_complete_an_unconfigured_manifest(candidate):
    asset = (candidate / 'tests/test_contract.py').read_text()
    assert asset.startswith('"""LP contract behavior')
    package = load_package_from_dir(candidate)
    with pytest.raises(IncompleteStrategyError, match='Complete saved configuration'):
        require_implemented_candidate(package)
    report = _validate_loaded(package, smoke_test=True)
    assert report.structural['status'] == 'failed'
    assert report.behavioral['status'] == 'not_run'
    assert any(issue.code == 'strategy_implementation_incomplete' for issue in report.blockers)


def configure_synthetic_manifest(candidate):
    manifest = yaml_io.load(candidate / 'strategy.yml')
    request = dict(kind='lp_add', component_id='synthetic-component', wallet_id='synthetic-wallet',
        chain='synthetic-chain', protocol='synthetic-protocol', parameters={'pool_id': 'synthetic-pool'})
    manifest['params'] = dict(
        lp_source=dict(source_id='synthetic-lp', wallet_id=request['wallet_id'], chain=request['chain'],
            protocol=request['protocol'], pool_id='synthetic-pool', token_id=None,
            token0='synthetic-token-a', token1='synthetic-token-b', decimals=[18, 6], fee=500,
            tick_spacing=10, component_binding=dict(id='synthetic-component',
                revision='synthetic-revision', abi_version=1), account_request=request),
        lp_policy=dict(max_age_seconds=60, persistence_seconds=10, cooldown_seconds=100,
            cost_multiple=2, enter_when_empty=True, exit_below_principal_usd=5, collect_min_fees_usd=1),
        lp_execution=dict(operation_key='synthetic-cycle', requests={'lp_add': request}))
    yaml_io.dump(candidate / 'strategy.yml', manifest)


def test_asset_passes_actual_static_validation(candidate):
    configure_synthetic_manifest(candidate)
    require_implemented_candidate(load_package_from_dir(candidate))
    report = _validate_loaded(load_package_from_dir(candidate), smoke_test=False, run_tests=False)
    assert report.structural['status'] == 'passed', json.dumps([issue.asdict() for issue in report.blockers])


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual macOS candidate sandbox required')
@pytest.mark.parametrize('configured', [False, True])
def test_asset_runs_in_actual_isolation_without_changing_candidate(candidate, configured):
    if configured:
        configure_synthetic_manifest(candidate)
    before = {p.relative_to(candidate): p.read_bytes() for p in candidate.rglob('*') if p.is_file()}
    package = load_package_from_dir(candidate)
    if configured:
        validation = _validate_loaded(package, smoke_test=True)
        assert validation.ok, validation.asdict()
        behavior = validation.behavioral
    else:
        report = run_candidate_validation(package, smoke_test=True)
        behavior = report['behavioral']
        assert report['smoke_issues'] == [], behavior
    assert behavior['status'] == 'passed', behavior
    assert behavior['summary']['collected'] == behavior['summary']['passed'] == 17, behavior
    assert behavior['summary']['skipped'] == 0, behavior
    assert behavior['safety']['os_isolated'] is True
    assert behavior['safety']['network'] == 'denied'
    assert before == {p.relative_to(candidate): p.read_bytes() for p in candidate.rglob('*') if p.is_file()}
    # Fixture tests never alter manifest configuration, paper mode or readiness.
    assert yaml_io.load(candidate / 'strategy.yml')['mode'] == 'paper'
    if not configured:
        with pytest.raises(IncompleteStrategyError):
            require_implemented_candidate(load_package_from_dir(candidate))


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual macOS candidate sandbox required')
def test_asset_detects_import_only_hold_regression(candidate):
    # Mutate only this disposable copy: an import-only smoke would miss this.
    path = candidate / 'main.py'
    path.write_text(path.read_text().replace('def run(ctx):',
        'def run(ctx):\n    return ctx.result.hold(reason="broken lifecycle")'))
    behavior = run_candidate_validation(load_package_from_dir(candidate), smoke_test=True)['behavioral']
    assert behavior['status'] == 'failed', behavior
    assert behavior['summary']['failed'] > 0, behavior
