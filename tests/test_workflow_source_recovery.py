"""Malformed manifests remain inspectable; repairs only create reviewed proposals."""
import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.strategies.workflow_graph import WorkflowError, package_revision, read_manifest
from nerya.strategies.workflow_service import source_files, view_workflow, workflow_index, propose_workflow


@pytest.fixture
def broken(tmp_path):
    paths = WorkspacePaths(tmp_path)
    files = StrategyCodeGenerator(paths).generate(StrategyGenerationRequest(strategy_id='lp_recovery',
        title='LP recovery', prompt='Offline recovery fixture', markets=('BINANCE:ETHUSDT',),
        accounts=('paper_main',)), validate=False, create_proposal_record=False).files
    proposal = create_proposal(paths, kind='strategy_package_proposal', summary='Fixture', rationale='Fixture',
        test_plan='Offline only', rollback='Discard fixture', target='strategies/lp_recovery', initial_state='draft',
        extra_files={f'after/strategies/lp_recovery/{name}': value for name, value in files.items()})
    out = {'strategy_id': 'lp_recovery', 'proposal_id': proposal.id}
    root = paths.proposals / out['proposal_id'] / 'after/strategies/lp_recovery'
    # Same parser failure as prp_5ebaf6980964: an unquoted Evidence: in a scalar.
    original = (root / 'strategy.yml').read_text()
    malformed = original + '\nreview_plan:\n  focus: inspect Evidence: source receipts\n'
    (root / 'strategy.yml').write_text(malformed)
    return paths, out, root, malformed


def test_invalid_yaml_view_keeps_exact_source_diagnostic_and_revision(broken):
    paths, out, root, malformed = broken
    files, source = source_files(paths, out['strategy_id'], out['proposal_id'])
    with pytest.raises(WorkflowError) as failure:
        read_manifest(files)
    view = view_workflow(paths, out['strategy_id'], out['proposal_id'])
    assert view['ok'] is True
    assert view['recovery'] == {'file': 'strategy.yml', 'content': malformed, 'diagnostic': str(failure.value)}
    assert 'line ' in view['recovery']['diagnostic']
    assert view['source'] == {**source, 'file_count': len(files)}
    assert view['revision'] == package_revision(files)
    assert view['can_edit'] is True
    assert view['manifest'] == {} and view['strategy']['nodes'] == []
    assert (root / 'strategy.yml').read_text() == malformed
    row, = workflow_index(paths)['workflows']
    assert row['state'] == 'invalid' and row['error'] == str(failure.value)


@pytest.mark.parametrize('invalid_manifest', [False, True])
def test_view_file_count_tracks_current_source_without_writes(broken, invalid_manifest):
    paths, out, root, malformed = broken
    if not invalid_manifest:
        (root / 'strategy.yml').write_text(malformed.split('\nreview_plan:')[0] + '\n')
    files, _ = source_files(paths, **out)
    assert view_workflow(paths, **out)['source']['file_count'] == len(files)
    (root / 'strategy_execution.py').write_text('raise RuntimeError("must not execute")\n')
    (root / 'tests').mkdir(exist_ok=True)
    (root / 'tests/test_contract.py').write_text('raise RuntimeError("must not execute")\n')
    before = {p.relative_to(paths.root): p.read_bytes() for p in paths.root.rglob('*') if p.is_file()}
    view = view_workflow(paths, **out)
    assert view['source']['file_count'] == len(files) + 2
    assert ('recovery' in view) == invalid_manifest
    assert {p.relative_to(paths.root): p.read_bytes() for p in paths.root.rglob('*') if p.is_file()} == before


def repair_payload(broken):
    paths, out, root, malformed = broken
    return {**out, 'base_revision': view_workflow(paths, **out)['revision'],
            'manifest_source': malformed.split('\nreview_plan:')[0] + '\n'}


def test_repair_creates_new_proposal_without_changing_original_or_executing(broken, monkeypatch):
    from nerya.strategies import candidate_validation
    monkeypatch.setattr(candidate_validation, 'run_candidate_validation',
                        lambda *a, **kw: pytest.fail('source repair executed candidate'))
    paths, out, root, malformed = broken
    saved = propose_workflow(paths, repair_payload(broken))
    assert saved['ok'], saved
    assert saved['proposal_id'] != out['proposal_id']
    assert saved['state'] == 'pending_review'
    assert 'recovery' not in saved['workflow']
    assert (root / 'strategy.yml').read_text() == malformed
    assert not (paths.strategies / out['strategy_id']).exists()


@pytest.mark.parametrize('mutation', ['invalid', 'identity', 'mixed', 'stale'])
def test_failed_repair_keeps_original_and_creates_no_proposal(broken, mutation):
    paths, out, root, malformed = broken
    payload = repair_payload(broken)
    before = set(paths.proposals.iterdir())
    if mutation == 'invalid': payload['manifest_source'] = 'id: [broken'
    if mutation == 'identity': payload['manifest_source'] = payload['manifest_source'].replace('lp_recovery', 'other_id')
    if mutation == 'mixed': payload['changes'] = [{'node_id': 'main', 'content': 'pass'}]
    if mutation == 'stale': payload['base_revision'] = 'obsolete'
    with pytest.raises(WorkflowError):
        propose_workflow(paths, payload)
    assert (root / 'strategy.yml').read_text() == malformed
    assert set(paths.proposals.iterdir()) == before
