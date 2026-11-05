"""CEX-shaped production DB records; every write stays under tmp_path.

The incident IDs and 14-check/9-artifact shape come from the reported case,
not a live DB export. Only the host fixture supplies producer authority.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from nerya.agent.assistant_coordination import delivery_projection, execution_context
from nerya.agent.assistant_receipts import _projection
from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore
from nerya.agent.command_store import CommandStore
from nerya.core.paths import WorkspacePaths
from nerya.task_delivery import start_delivery, update_delivery
from nerya.tools.verification import verification_receipt

pytestmark = pytest.mark.smoke

CID = 'acmd_49618f585a9b9c005fe10317'
SID = 'taskchat_49618f585a9b9c005fe10317'
LIMITATIONS = ['Historical replay only; no live execution.',
               'Unspecified periods use workflow defaults.']
ANSWER = 'All work is complete, including live trading.'


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No network or model execution is allowed')

    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setattr('nerya.llm.gateway.LLMGateway.call_messages', forbidden)
    paths = WorkspacePaths(tmp_path)
    assistants = AssistantStore(paths)
    parent = assistants.home('main')['session_id']
    assistants.bind(SID, 'main')
    work = AssistantWork(paths)
    store = CommandStore(paths, 'partial-projection-test')
    store.accept(cid=CID, sid=SID, actor='owner', kind='send',
        request={'payload': {'text': 'Minimal CEX strategy and historical replay'}},
        context={'publication_language': 'zh'}, turn_id='turn-cex')
    with store.transaction() as con:
        con.execute('INSERT INTO assistant_work VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('awork_cex', 'main', parent, SID, 'CEX replay', 'strategy',
             'fixture', CID, '{}', 'owner', 1))
        con.execute('UPDATE agent_commands SET state=?,result_json=?,created_at=10 WHERE command_id=?',
            ('succeeded', json.dumps({'final_text': ANSWER,
                                     'verifier_outcome': {'trusted': True}}), CID))
    checks = [f'producer_check_{i}' for i in range(14)]
    artifacts = [f'output_{i}' for i in range(9)]
    delivery = start_delivery(paths, command_id=CID, session_id=SID,
        skill_id='tasks', workflow_id='report',
        requested_scope={'summary': 'CEX strategy and historical replay',
                         'limitations': LIMITATIONS},
        stages=[{'id': 'deliver', 'required_checks': checks,
                 'required_artifacts': artifacts}],
        workflow_contract={'skill_id': 'tasks', 'workflow_id': 'report',
            'source': 'test:host_readback', 'revision': 'a' * 64,
            'required_checks': checks, 'required_artifacts': artifacts})
    receipt = verification_receipt(artifact_id='report:cex', revision='b' * 64,
        scope='Host fixture checks of historical outputs only',
        checks=[{'check_id': check, 'status': 'passed',
                 'evidence_ref': f'reports/output_{i % 9}.json'}
                for i, check in enumerate(checks)])
    delivery = update_delivery(paths, command_id=CID, session_id=SID,
        expected_revision=delivery['delivery_revision'],
        stages=[{'id': 'deliver', 'state': 'completed'}],
        artifacts=[{'artifact_id': artifact, 'ref': f'reports/{artifact}.json',
                    'revision': 'b' * 64} for artifact in artifacts],
        producer_receipt=receipt)
    assert delivery['completion']['allowed'] is True
    assert delivery['completion']['status'] == 'partial'
    assert delivery['completion']['reasons'] == []
    return SimpleNamespace(paths=paths, store=store, work=work, parent=parent, delivery=delivery)


def project(fixture, *, command_id=CID):
    with fixture.store.transaction() as con:
        work = con.execute('SELECT * FROM assistant_work').fetchone()
        command = con.execute('SELECT * FROM agent_commands WHERE command_id=?',
                              (command_id,)).fetchone()
        before = con.total_changes
        result = delivery_projection(con, work, command)
        assert con.total_changes == before
        return result


def save(fixture, delivery, *, command_id=CID):
    with fixture.store.transaction() as con:
        con.execute('UPDATE agent_commands SET delivery_json=? WHERE command_id=?',
                    (None if delivery is None else json.dumps(delivery), command_id))


def surfaces(fixture):
    fixture.work.sync(fixture.parent)
    with fixture.store.transaction() as con:
        work = con.execute('SELECT * FROM assistant_work').fetchone()
        command = con.execute('SELECT * FROM agent_commands WHERE command_id=?', (CID,)).fetchone()
        receipt = _projection(con, work, command, include_result=True)
        stored = json.loads(con.execute(
            'SELECT result_json FROM assistant_results WHERE command_id=?', (CID,)).fetchone()[0])
        message = con.execute('SELECT content,meta_json FROM agent_messages WHERE message_id=?',
                              ('assistant_result:' + CID,)).fetchone()
    assert message['content'] == stored['final_text'] == ANSWER
    assert stored['reported_result']['authority'] == 'untrusted_model_output'
    return [project(fixture), fixture.work.tasks(sid=fixture.parent)[0],
            fixture.work.read_task(fixture.parent, 'awork_cex', 'owner'),
            fixture.work.results('main')[0],
            json.loads(fixture.work.context(fixture.parent))['tasks'][0],
            receipt, stored, json.loads(message['meta_json'])]


def test_allowed_partial_reaches_parent_without_claiming_task_completion(fixture):
    assert len(fixture.delivery['checks']) == 14
    assert all(row['producer_owned'] and row['status'] == 'passed'
               for row in fixture.delivery['checks'])
    assert len(fixture.delivery['artifacts']) == 9
    for row in surfaces(fixture):
        assert row['delivery_completion']['allowed'] is True
        assert row['delivery_completion']['status'] == 'partial'
        assert row['delivery_completion']['reasons'] == []
        assert row['limitations'] == LIMITATIONS
        assert row['task_complete'] is False
        assert row['unresolved_deliveries'] == []
        assert row['publication']['verification_status'] == 'unverified'
        assert row['publication']['language'] == 'zh'
    with fixture.store.transaction() as con:
        assert json.loads(con.execute('SELECT delivery_json FROM agent_commands WHERE command_id=?',
                                      (CID,)).fetchone()[0]) == fixture.delivery


@pytest.mark.parametrize('prior_kind', ['blocked', 'allowed_partial', 'failed_partial'])
def test_earlier_unresolved_real_command_still_blocks_aggregate(fixture, prior_kind):
    prior = deepcopy(fixture.delivery)
    prior.update(command_id='acmd_prior', delivery_id='delivery_prior')
    if prior_kind == 'blocked':
        prior['completion'] = {'allowed': False, 'status': 'partial',
                               'reasons': ['check_pending:coverage']}
    fixture.store.accept(cid='acmd_prior', sid=SID, actor='owner', kind='send',
        request={'payload': {'text': 'Prior unfinished request'}}, context={}, turn_id='turn-prior')
    with fixture.store.transaction() as con:
        con.execute('UPDATE agent_commands SET state=?,created_at=1 WHERE command_id=?',
                    ('failed' if prior_kind == 'failed_partial' else 'succeeded', 'acmd_prior'))
    save(fixture, prior, command_id='acmd_prior')
    for row in surfaces(fixture):
        assert row['delivery_completion']['allowed'] is False
        assert row['task_complete'] is False
        refs = row['unresolved_deliveries']
        assert [ref['command_id'] for ref in refs] == ['acmd_prior']
        assert refs[0]['delivery_id'] == 'delivery_prior'
        if prior_kind == 'blocked':
            assert 'check_pending:coverage' in row['delivery_completion']['reasons']


@pytest.mark.parametrize('invalid', [
    'missing', 'malformed', 'foreign_delivery', 'protocol', 'revision',
    'denied', 'unknown_status', 'reasons', 'no_producer', 'no_contract',
    'stale_contract', 'missing_check', 'missing_artifact', 'unverified_artifact',
    'failed_check', 'stale_check', 'pending_check', 'missing_check_status',
    'bad_checks_shape', 'bad_stages_shape',
])
def test_partial_cannot_gain_permission_from_invalid_or_untrusted_records(fixture, invalid):
    delivery = deepcopy(fixture.delivery)
    if invalid == 'missing':
        delivery = None
    elif invalid == 'malformed':
        delivery = 'invalid JSON record'
    elif invalid == 'foreign_delivery':
        delivery['command_id'] = 'acmd_foreign'
    elif invalid == 'protocol':
        delivery['protocol'] = 'untrusted'
    elif invalid == 'revision':
        delivery['delivery_revision'] = '2'
    elif invalid == 'denied':
        delivery['completion']['allowed'] = False
    elif invalid == 'unknown_status':
        delivery['completion']['status'] = 'unconfirmed'
    elif invalid == 'reasons':
        delivery['completion']['reasons'] = ['producer_evidence_missing']
    elif invalid == 'no_producer':
        for check in delivery['checks']:
            check.pop('producer_owned')
    elif invalid == 'no_contract':
        delivery.pop('workflow_contract')
    elif invalid == 'stale_contract':
        delivery['workflow_contract_error'] = 'delivery_contract_stale'
    elif invalid == 'missing_check':
        delivery['checks'].pop()
    elif invalid == 'missing_artifact':
        delivery['artifacts'].pop()
    elif invalid == 'unverified_artifact':
        delivery['artifacts'][0]['revision'] = 'c' * 64
    elif invalid.endswith('_check') or invalid == 'missing_check_status':
        delivery['checks'][0]['status'] = invalid.split('_')[0]
    elif invalid == 'bad_checks_shape':
        delivery['checks'] = 1
    elif invalid == 'bad_stages_shape':
        delivery['stages'] = [None]
    save(fixture, delivery)
    row = project(fixture)
    assert row['delivery_completion']['allowed'] is False
    assert row['task_complete'] is False


@pytest.mark.parametrize('state', ['queued', 'running', 'unconfirmed', 'failed', 'blocked', 'interrupted'])
def test_execution_must_be_confirmed_succeeded_even_with_valid_partial(fixture, state):
    with fixture.store.transaction() as con:
        con.execute('UPDATE agent_commands SET state=? WHERE command_id=?', (state, CID))
    row = project(fixture)
    assert row['delivery_completion']['allowed'] is False
    assert row['task_complete'] is False
    assert row['unresolved_deliveries'][0]['command_id'] == CID


@pytest.mark.parametrize('foreign', ['actor_id', 'session_id'])
def test_partial_cannot_borrow_a_foreign_command(fixture, foreign):
    with fixture.store.transaction() as con:
        con.execute(f'UPDATE agent_commands SET {foreign}=? WHERE command_id=?', ('foreign', CID))
    row = project(fixture)
    assert row['delivery_completion'] == {
        'allowed': False, 'status': 'invalid', 'reasons': ['command_binding_mismatch']}


def test_missing_command_cannot_borrow_saved_partial(fixture):
    row = project(fixture, command_id='absent')
    assert row['delivery_completion']['allowed'] is False
    assert row['task_complete'] is False


@pytest.mark.parametrize('partial_reason', ['optional_gap', 'skipped_stage'])
def test_producer_partial_without_text_limitations_stays_partial(fixture, partial_reason):
    delivery = deepcopy(fixture.delivery)
    delivery['limitations'] = []
    if partial_reason == 'optional_gap':
        delivery['checks'].append({
            'check_id': 'optional_live_execution', 'status': 'missing',
            'required': False, 'producer_owned': True,
            'artifact_id': 'report:cex', 'revision': 'b' * 64})
    else:
        delivery['stages'].append({'id': 'optional_live_execution', 'state': 'skipped'})
    save(fixture, delivery)
    row = project(fixture)
    assert row['delivery_completion'] == {'allowed': True, 'status': 'partial', 'reasons': []}
    assert row['task_complete'] is False
    assert row['unresolved_deliveries'] == []


def test_existing_complete_behavior_and_continuation_remain_strict(fixture):
    delivery = update_delivery(fixture.paths, command_id=CID, session_id=SID,
        expected_revision=fixture.delivery['delivery_revision'], limitations=[])
    assert delivery['completion']['status'] == 'complete'
    for row in surfaces(fixture):
        assert row['task_complete'] is True
        assert row['delivery_completion']['allowed'] is True
        assert row['delivery_completion']['status'] == 'complete'
        assert row['unresolved_deliveries'] == []
        assert row['publication']['verification_status'] == 'verified'
    save(fixture, fixture.delivery)
    fixture.store.accept(cid='acmd_followup', sid=SID, actor='owner', kind='send',
        request={'payload': {'text': 'Continue the remaining scope'}}, context={}, turn_id='turn-next')
    with fixture.store.transaction() as con:
        con.execute('UPDATE agent_commands SET created_at=20 WHERE command_id=?', ('acmd_followup',))
    continuation = json.loads(execution_context(fixture.paths, SID, 'acmd_followup'))
    assert continuation['unfinished_workflows'][0]['command_id'] == CID
    assert project(fixture, command_id='acmd_followup')['delivery_completion']['allowed'] is False
