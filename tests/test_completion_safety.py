"""Isolated contract regressions; all state lives in pytest's /tmp workspace."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from nerya.task_delivery import (
    DeliveryError, _completion, finalize_delivery, new_delivery, public_delivery,
    read_delivery, record_delivery_verification, start_delivery, update_delivery,
)
from nerya.tools.native import delivery as native_delivery
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall
from test_task_delivery import _command, _deps, _receipt, _runtime

pytestmark = pytest.mark.smoke


def draft(stages=None):
    return new_delivery(command_id='isolated-probe', skill_id='generic',
        workflow_id='generic_delivery', requested_scope={'summary': 'verified deliverable'},
        stages=stages if stages is not None else [{'id': 'deliver', 'state': 'completed'}])


def bound_artifact(artifact_id='report:one', revision='a'):
    return {'artifact_id': artifact_id, 'ref': artifact_id, 'revision': revision}


def evidence_delivery():
    row = draft([{'id': 'deliver', 'state': 'completed',
        'required_checks': ['coverage'], 'required_artifacts': ['report:one']}])
    row['checks'] = [{'check_id': 'coverage', 'status': 'passed', 'required': True,
        'producer_owned': True, 'artifact_id': 'report:one', 'revision': 'a'}]
    row['artifacts'] = [bound_artifact()]
    # Host fixture for the generic evaluator; native tests resolve real resources.
    row['workflow_contract'] = {'skill_id': 'generic', 'workflow_id': 'generic_delivery',
        'source': 'test:producer', 'revision': 'a',
        'required_checks': ['coverage'], 'required_artifacts': ['report:one']}
    return row


@pytest.mark.parametrize('status', ['missing', 'failed', 'pending', 'stale', 'unknown'])
def test_optional_producer_gap_is_partial_not_full_completion(status):
    row = evidence_delivery()
    row['checks'].append({**row['checks'][0], 'check_id': 'optional_scope',
                          'required': False, 'status': status})
    completion = _completion(row)
    assert completion['allowed'] is True
    assert completion['status'] == 'partial'
    assert completion['reasons'] == []
    row['checks'][-1]['status'] = 'passed'
    assert _completion(row)['status'] == 'complete'


def test_optional_producer_gap_survives_model_clearing_limitations_and_reopen(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    source = evidence_delivery()
    row = start_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        skill_id='generic', workflow_id='generic_delivery', requested_scope={'summary': 'deliver evidence'},
        stages=source['stages'], workflow_contract=source['workflow_contract'])
    required = _receipt('coverage', artifact_id='report:one')
    optional = _receipt('optional_scope', 'missing', artifact_id='report:one', required=False)
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], artifacts=source['artifacts'],
        producer_receipts=[required, optional])
    assert row['completion']['allowed'] and row['completion']['status'] == 'partial'
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], limitations=[])
    restored = read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])
    assert restored['completion']['status'] == 'partial'
    with pytest.raises(DeliveryError, match='delivery_check_owned_by_producer'):
        update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
            expected_revision=row['delivery_revision'], checks=[{'check_id': 'optional_scope', 'status': 'missing', 'required': False}])
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], producer_receipt=_receipt('optional_scope', artifact_id='report:one', required=False))
    assert row['completion']['status'] == 'complete'


@pytest.mark.parametrize('state', ['completed', 'skipped'])
def test_no_requirements_cannot_complete(state):
    row = draft([{'id': 'deliver', 'state': state}])
    assert not row['completion']['allowed']


@pytest.mark.parametrize('mutation', [
    'no_checks', 'no_artifact', 'unowned', 'truthy_owner', 'optional_only',
    'missing_check_revision', 'missing_check_artifact', 'unbound_artifact',
    'wrong_revision', 'empty_ref',
])
def test_requires_required_producer_evidence_bound_to_delivered_artifact(mutation):
    row = evidence_delivery()
    check, artifact = row['checks'][0], row['artifacts'][0]
    if mutation == 'no_checks':
        row['checks'] = []
    elif mutation == 'no_artifact':
        row['artifacts'] = []
    elif mutation == 'unowned':
        check.pop('producer_owned')
    elif mutation == 'truthy_owner':
        check['producer_owned'] = 'true'
    elif mutation == 'optional_only':
        check['required'] = False
    elif mutation == 'missing_check_revision':
        check.pop('revision')
    elif mutation == 'missing_check_artifact':
        check.pop('artifact_id')
    elif mutation == 'unbound_artifact':
        artifact['artifact_id'] = 'model-only'
    elif mutation == 'wrong_revision':
        artifact['revision'] = 'b'
    elif mutation == 'missing_artifact_revision':
        artifact.pop('revision')
    else:
        artifact['ref'] = ''
    assert not _completion(row)['allowed'], mutation


@pytest.mark.parametrize('status', ['failed', 'missing', 'pending', 'stale', 'unknown'])
def test_unresolved_required_check_blocks_unrelated_verified_artifact(status):
    row = evidence_delivery()
    row['checks'].append({**row['checks'][0], 'check_id': 'second', 'status': status})
    assert not _completion(row)['allowed']


@pytest.mark.parametrize('requirement', ['required_artifacts', 'required_checks'])
def test_skipping_stage_cannot_drop_declared_requirement(requirement):
    row = evidence_delivery()
    row['stages'].append({'id': 'unfinished', 'state': 'skipped', requirement: ['missing']})
    row['limitations'] = ['Declared limitation does not discharge required work']
    assert not _completion(row)['allowed']


@pytest.mark.parametrize('bad', ['unowned', 'failed', 'missing_identity'])
def test_stage_required_optional_receipt_cannot_hide_behind_same_check_id(bad):
    row = evidence_delivery()
    row['stages'][0]['required_checks'] = ['stage_check']
    invalid = {'check_id': 'stage_check', 'status': 'passed', 'required': False,
        'producer_owned': True, 'artifact_id': 'other', 'revision': 'a'}
    if bad == 'unowned':
        invalid.pop('producer_owned')
    elif bad == 'failed':
        invalid['status'] = 'failed'
    else:
        invalid.pop('revision')
    # Both source versions are retained; a later same-name pass cannot erase one.
    row['checks'] += [invalid, {**row['checks'][0], 'check_id': 'stage_check', 'required': False}]
    assert not _completion(row)['allowed']


@pytest.mark.parametrize('partial', [None, 'limitation', 'skipped'])
def test_verified_delivery_and_honest_partial_are_supported(partial):
    row = evidence_delivery()
    if partial == 'limitation':
        row['limitations'] = ['Future follow-up remains unverified']
    elif partial == 'skipped':
        row['stages'].append({'id': 'optional', 'state': 'skipped'})
    completion = _completion(row)
    assert completion['allowed']
    assert completion['status'] == ('partial' if partial else 'complete')


def test_all_skipped_cannot_be_delivered_even_with_evidence():
    row = evidence_delivery()
    row['stages'][0]['state'] = 'skipped'
    assert not _completion(row)['allowed']


def test_limitations_without_delivered_evidence_are_not_genuine_partial():
    row = draft()
    row['limitations'] = ['No evidence collected']
    assert _completion(row)['status'] == 'incomplete'


def test_verified_subset_is_partial_but_does_not_complete_missing_requirements():
    row = evidence_delivery()
    row['stages'].append({'id': 'followup', 'state': 'skipped', 'required_checks': ['missing']})
    row['limitations'] = ['Follow-up remains missing']
    completion = _completion(row)
    assert not completion['allowed'] and completion['status'] == 'partial'


def test_stage_declaration_cannot_promote_optional_producer_check():
    row = evidence_delivery()
    row['stages'][0]['required_checks'] = ['coverage']
    row['checks'][0]['required'] = False
    assert not _completion(row)['allowed']


def test_model_artifact_cannot_replace_producer_receipt_and_cas_survives(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    row = start_delivery(config.paths, command_id=command['command_id'],
        session_id=command['session_id'], skill_id='generic', workflow_id='generic_delivery',
        requested_scope={'summary': 'deliver'}, stages=evidence_delivery()['stages'])
    row = update_delivery(config.paths, command_id=command['command_id'],
        session_id=command['session_id'], expected_revision=row['delivery_revision'],
        artifacts=[bound_artifact()], limitations=['Future follow-up'])
    failed = native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=deps)
    assert failed.semantic_success is False
    row = record_delivery_verification(config.paths, command_id=command['command_id'], receipt=_receipt())
    assert not row['completion']['allowed'] and row['completion']['status'] == 'partial'
    assert row['workflow_contract_error'] == 'trusted_delivery_contract_missing'
    before = deepcopy(row)
    with pytest.raises(DeliveryError, match='delivery_revision_conflict'):
        finalize_delivery(config.paths, command_id=command['command_id'],
            session_id=command['session_id'], expected_revision=row['delivery_revision'] - 1,
            producer_receipts=[_receipt()])
    assert read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id']) == before
    row = finalize_delivery(config.paths, command_id=command['command_id'],
        session_id=command['session_id'], expected_revision=row['delivery_revision'],
        source_versions={'report:one': 'b'})
    assert not row['completion']['allowed']
    assert row['checks'][0]['status'] == 'stale'


def test_persisted_stage_skip_keeps_promises_and_can_recover(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    identity = dict(command_id=command['command_id'], session_id=command['session_id'])
    row = start_delivery(config.paths, **identity, skill_id='generic', workflow_id='generic_delivery',
        requested_scope={'summary': 'two verified results'}, stages=[
            evidence_delivery()['stages'][0],
            {'id': 'followup', 'required_checks': ['followup'], 'required_artifacts': ['report:two']},
        ])
    row = record_delivery_verification(config.paths, command_id=command['command_id'], receipt=_receipt())
    row = update_delivery(config.paths, **identity, expected_revision=row['delivery_revision'],
        artifacts=[bound_artifact()], stages=[{'id': 'followup', 'state': 'skipped',
            'required_checks': [], 'required_artifacts': []}], limitations=['Follow-up is missing'])
    assert row['stages'][1]['required_checks'] == ['followup']
    assert row['stages'][1]['required_artifacts'] == ['report:two']
    assert not row['completion']['allowed'] and row['completion']['status'] == 'partial'
    final = native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=_deps(config, command))
    assert final.semantic_success is False
    row = record_delivery_verification(config.paths, command_id=command['command_id'],
        receipt=_receipt(check_id='followup', artifact_id='report:two'))
    row = update_delivery(config.paths, **identity, expected_revision=row['delivery_revision'],
        artifacts=[bound_artifact('report:two')], stages=[{'id': 'followup', 'state': 'completed'}], limitations=[])
    assert not row['completion']['allowed']
    assert row['workflow_contract_error'] == 'trusted_delivery_contract_missing'


def test_old_reads_remain_lossless_but_reconciliation_rechecks_verdict(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    args = dict(skill_id='generic', workflow_id='generic_delivery',
        requested_scope={'summary': 'deliver'}, stages=[{'id': 'deliver', 'state': 'completed'}])
    legacy = start_delivery(config.paths, command_id=command['command_id'],
        session_id=command['session_id'], **args)
    legacy['completion'] = {'allowed': True, 'status': 'complete', 'reasons': [], 'computed_at': 1}
    with runtime.store.transaction() as con:
        con.execute('UPDATE agent_commands SET delivery_json=? WHERE command_id=?',
            (json.dumps(legacy), command['command_id']))
    assert read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id']) == legacy
    assert public_delivery(legacy) == legacy
    deps = _deps(config, command)
    result = native_delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments=args), deps=deps)
    row = tool_json_data(result)
    assert not row['completion']['allowed']
    assert row['delivery_revision'] == legacy['delivery_revision'] + 1
    again = native_delivery.delivery_start_handler(ToolCall(name='delivery_start', arguments=args), deps=deps)
    assert tool_json_data(again) == row


@pytest.mark.parametrize('missing', ['checks', 'artifacts', 'both'])
def test_undeclared_requirements_cannot_complete_even_with_producer_evidence(missing):
    row = evidence_delivery()
    row.pop('workflow_contract')
    if missing in {'checks', 'both'}:
        row['stages'][0]['required_checks'] = []
    if missing in {'artifacts', 'both'}:
        row['stages'][0]['required_artifacts'] = []
    assert not _completion(row)['allowed']


@pytest.mark.parametrize('case', ['optional_trivial', 'undeclared_check', 'unknown_status',
    'unrelated_artifact', 'unrelated_second_artifact', 'unattested_ref'])
def test_unrelated_or_unknown_evidence_cannot_grant(case):
    row = evidence_delivery()
    if case == 'optional_trivial':
        row['checks'][0].update(check_id='trivial', required=False)
    elif case == 'undeclared_check':
        row['checks'][0]['check_id'] = 'unknown_check'
    elif case == 'unknown_status':
        row['checks'][0]['status'] = 'unknown'
    elif case == 'unrelated_artifact':
        row['artifacts'][0] = bound_artifact('unrelated')
    elif case == 'unrelated_second_artifact':
        row['stages'][0]['required_artifacts'].append('unrelated')
        row['artifacts'].append(bound_artifact('unrelated'))
    else:
        row['artifacts'][0]['ref'] = 'model-invented/report.txt'
    assert not _completion(row)['allowed']


def test_producer_output_reference_supports_alias_without_forging_verification():
    row = evidence_delivery()
    row['checks'][0]['evidence_ref'] = 'outputs/verified-report.txt'
    row['artifacts'][0]['ref'] = 'outputs/verified-report.txt'
    assert _completion(row)['allowed']


def test_current_skill_contract_rejects_source_only_evidence_and_skipped_checks():
    import nerya.task_delivery as delivery_module
    skill = Path(delivery_module.__file__).parent / 'skills/builtin/strategy_author/SKILL.md'
    args = json.loads(skill.read_text().split('```json', 1)[1].split('```', 1)[0])
    row = new_delivery(command_id='isolated-skill-contract', **args)
    for stage in row['stages']:
        stage['state'] = 'completed'
    row['checks'] = [{**evidence_delivery()['checks'][0], 'check_id': 'source_binding'}]
    row['artifacts'] = [bound_artifact()]
    assert not _completion(row)['allowed']
    row['stages'][2]['state'] = row['stages'][3]['state'] = 'skipped'
    completion = _completion(row)
    assert not completion['allowed']
    assert 'check_missing:behavioral_tests' in completion['reasons']
    assert 'check_missing:historical_run' in completion['reasons']


def test_current_bridge_does_not_attest_arbitrary_report_path(tmp_path):
    from test_task_delivery import _completed_replay_before_delivery
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps, proposal, event, args = _completed_replay_before_delivery(config, runtime, command)
    args['stages'][0]['required_artifacts'] = ['report']
    row = tool_json_data(native_delivery.delivery_start_handler(
        ToolCall(name='delivery_start', arguments=args), deps=deps))
    row = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=row['delivery_revision'], stages=[{'id': 'deliver', 'state': 'completed'}],
        artifacts=[{'artifact_id': 'report', 'ref': 'backtests/20261022_040449/invented-report.md',
                    'revision': event['verification']['revision']}])
    result = native_delivery.delivery_finalize_handler(ToolCall(name='delivery_finalize',
        arguments={'expected_revision': row['delivery_revision']}), deps=deps)
    assert result.semantic_success is False
    assert 'artifact_unverified:report' in tool_json_data(result)['completion']['reasons']


def test_model_required_placeholder_cannot_promote_optional_receipt(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    identity = dict(command_id=command['command_id'], session_id=command['session_id'])
    row = start_delivery(config.paths, **identity, skill_id='generic', workflow_id='generic_delivery',
        requested_scope={'summary': 'deliver'}, stages=evidence_delivery()['stages'])
    row = update_delivery(config.paths, **identity, expected_revision=row['delivery_revision'],
        checks=[{'check_id': 'coverage', 'status': 'pending', 'required': True}], artifacts=[bound_artifact()])
    row = record_delivery_verification(config.paths, command_id=command['command_id'],
        receipt=_receipt(required=False))
    assert not row['checks'][0]['required']
    assert not row['completion']['allowed']
