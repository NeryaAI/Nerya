"""Offline guidance extraction and synthetic delivery-contract checks; no live run."""
import json
from pathlib import Path
import re

import pytest

from nerya.task_delivery import (
    DELIVERY_UPDATE_SCHEMA, DeliveryError, _completion, new_delivery,
    validate_delivery_input,
)

pytestmark = pytest.mark.smoke
BUILTIN = Path(__file__).resolve().parents[1] / 'nerya/skills/builtin'


def test_delivery_starts_before_producers_on_both_entry_paths():
    backtest = ' '.join((BUILTIN / 'backtest/SKILL.md').read_text().split())
    rate = ' '.join((BUILTIN / 'strategy_author/references/rate-policy.md').read_text().split())
    assert 'current command' in backtest
    assert 'call `delivery_start` before validation/replay producers' in backtest
    assert 'workflow_id:strategy_authoring before producers' in rate
    assert backtest.index('delivery_start') < backtest.index('## Normal path')
    assert rate.index('delivery_start') < rate.index('1. Declare thresholds')
    assert 'partial-delivery workflow' in backtest
    assert 'Skill(skill="strategy_author", file="references/rate-policy.md")' in backtest


def test_cross_skill_guidance_uses_explicit_loadable_asset_calls():
    from test_dex_exact_scope_routing import read_skill

    for path in (
        'backtest/SKILL.md', 'backtest/references/replay-capabilities.md',
        'strategy_author/SKILL.md', 'strategy_author/references/lp-sampled-research.md',
    ):
        text = (BUILTIN / path).read_text()
        # A skill-prefixed path copied into file resolves to <skill>/<skill>/...
        assert not re.search(r'\b(?:backtest|strategy_author|research)/references/', text), path
        calls = re.findall(r'Skill\(skill="([^"]+)", file="([^"]+)"\)', text)
        assert calls, path
        for skill, file in calls:
            if '<' in file:  # Documentation placeholders are not concrete asset calls.
                continue
            assert not file.startswith(f'{skill}/'), (path, skill, file)
            assert read_skill(skill=skill, file=file)


def test_completed_gap_workflow_reads_preserves_stops_then_finalizes_once():
    text = (BUILTIN / 'strategy_author/references/rate-policy.md').read_text()
    workflow = ' '.join(text.split('## Completed rate replay with incomplete coverage', 1)[1]
                        .split('## New candidate:', 1)[0].split())
    steps = [workflow.index(action) for action in (
        '1. Read the existing persisted', '2. Stop producer retries',
        '3. Register the returned', '4. After report readback',
    )]
    assert steps == sorted(steps)
    for instruction in (
        '30 decisions and 1 simulated action', 'coverage_ok:false',
        'Daily APY observations', 'leading/trailing gaps',
        "producer's missing data_coverage check",
        'do not repeat strategy_validate or strategy_backtest on the same source/window',
        'Resume replay only after new producer data',
        'never send checks with status:passed or remove required checks',
        'report predates delivery_start', 'rather than rerunning producers',
        'call delivery_finalize once', 'completion.allowed:false',
        'partial/incomplete status and reasons', 'actual decision/action counts',
        'same pool and missing UTC intervals', 'provenance and cadence',
        'full request remains outstanding',
    ):
        assert instruction in workflow


@pytest.mark.parametrize('skill,workflow', [
    ('backtest', 'strategy_replay'), ('strategy_author', 'strategy_authoring'),
])
def test_missing_coverage_stays_partial_and_disallowed_despite_saved_artifacts(skill, workflow):
    # Synthetic projection with every other required check satisfied: this tests
    # the strict floor, without executing a producer or touching a workspace DB.
    contract = json.loads((BUILTIN / skill / 'references/delivery-contract.json').read_text())[workflow]
    contract = {**contract, 'skill_id': skill, 'workflow_id': workflow}
    assert 'data_coverage' in contract['required_checks']
    refs = {artifact: f'fixture:{artifact}' for artifact in contract['required_artifacts']}
    row = new_delivery(command_id='fixture', skill_id=skill, workflow_id=workflow,
        requested_scope={'summary': 'Full requested rate window'},
        stages=[{'id': 'deliver', 'state': 'completed'}], workflow_contract=contract)
    row['artifacts'] = [{'artifact_id': artifact, 'ref': ref, 'revision': 'fixture-v1'}
                        for artifact, ref in refs.items()]
    row['checks'] = [
        {'check_id': check, 'status': 'missing' if check == 'data_coverage' else 'passed',
         'producer_owned': True, 'required': True, 'revision': 'fixture-v1',
         'artifact_id': refs['report']}
        for check in contract['required_checks']
    ]
    # Bind every artifact to an otherwise passing synthetic producer check.
    for artifact, check in [('run_receipt', 'historical_run'), ('candidate', 'strategy_contract')]:
        if artifact in refs:
            next(item for item in row['checks'] if item['check_id'] == check)['artifact_id'] = refs[artifact]
    row['limitations'] = ['Daily APY leading/trailing gaps; need same-pool boundary observations']
    completion = _completion(row)
    assert completion['allowed'] is False
    assert completion['status'] == 'partial'
    assert completion['reasons'] == ['check_missing:data_coverage']
    # Merely removing the explanation cannot waive the producer check either.
    row['limitations'] = []
    assert _completion(row)['allowed'] is False
    with pytest.raises(DeliveryError, match='invalid_delivery_input'):
        validate_delivery_input({'expected_revision': 1, 'checks': [
            {'check_id': 'data_coverage', 'status': 'passed'},
        ]}, DELIVERY_UPDATE_SCHEMA)
    # Positive control: only new producer evidence can satisfy this last gap.
    next(item for item in row['checks'] if item['check_id'] == 'data_coverage')['status'] = 'passed'
    assert _completion(row)['allowed'] is True
