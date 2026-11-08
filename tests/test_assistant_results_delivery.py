"""Result history uses command-bound host delivery, never authored success claims.

Reduced fixture from acceptance-20261025.WreD5w/runtime-before-fixes.db,
opened with mode=ro&immutable=1. Summary is an excerpt; checks retain their
recorded statuses. All writes below target pytest's isolated temporary DB.
"""
import json
from pathlib import Path

import pytest

import test_assistant_receipts as assistant_receipts_fixtures

env = assistant_receipts_fixtures.env


def test_aave_result_exposes_command_delivery_without_rewriting_report(env):
    fixture = json.loads((Path(__file__).parent / 'fixtures/assistant_result_aave_incomplete.json').read_text())
    cid, sid = fixture['command_id'], fixture['task_session_id']
    env.manager.store.accept(cid=cid, sid=sid, actor='owner', kind='send',
        request={'payload': {'text': 'Aave report'}}, context={}, turn_id='turn-aave')
    with env.manager.store.transaction() as con:
        con.execute('INSERT INTO assistant_work VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (fixture['task_id'], 'main', env.sid, sid, fixture['title'], 'strategy',
             'fixture', cid, '{}', 'owner', 1))
        con.execute('UPDATE agent_commands SET state=?,result_json=?,delivery_json=? WHERE command_id=?',
            (fixture['state'], json.dumps({'reply_text': fixture['summary']}), json.dumps(fixture['delivery']), cid))
    row, = env.work.results('main')
    assert row['state'] == 'succeeded'
    assert row['delivery'] == fixture['delivery']
    assert row['task_complete'] is False
    assert row['delivery_completion']['allowed'] is False
    assert row['unresolved_deliveries'][0]['command_id'] == cid
    assert row['summary'] == fixture['summary']
    assert row['reported_result']['text'] == fixture['summary']


@pytest.mark.parametrize('first_complete', [False, True])
def test_result_delivery_is_scoped_through_its_command_not_later_success_or_failure(env, first_complete):
    fixture = json.loads((Path(__file__).parent / 'fixtures/assistant_result_aave_incomplete.json').read_text())
    sid, tid = fixture['task_session_id'], fixture['task_id']
    first_id, later_id = 'acmd_first', 'acmd_later'
    with env.manager.store.transaction() as con:
        con.execute('INSERT INTO assistant_work VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (tid, 'main', env.sid, sid, fixture['title'], 'strategy', 'fixture', first_id, '{}', 'owner', 1))
    for index, complete in enumerate([first_complete, not first_complete]):
        cid = [first_id, later_id][index]
        env.manager.store.accept(cid=cid, sid=sid, actor='owner', kind='send',
            request={'payload': {'text': cid}}, context={}, turn_id='turn-' + cid)
        delivery = {**fixture['delivery'], 'command_id': cid, 'delivery_id': 'delivery-' + cid}
        if complete:
            delivery['completion'] = {'allowed': True, 'status': 'complete', 'reasons': []}
            delivery['checks'] = []
        with env.manager.store.transaction() as con:
            con.execute('UPDATE agent_commands SET state=?,result_json=?,delivery_json=?,created_at=? WHERE command_id=?',
                ('succeeded', json.dumps({'reply_text': cid + ' authored report'}), json.dumps(delivery), index + 1, cid))
        rows = {row['command_id']: row for row in env.work.results('main')}
        if index == 0:
            first = rows[first_id]
    assert rows[first_id] == first
    assert first['task_complete'] is first_complete
    assert first['delivery']['completion']['allowed'] is first_complete
    assert rows[later_id]['delivery']['completion']['allowed'] is not first_complete
    assert rows[later_id]['task_complete'] is False
    assert rows[later_id]['unresolved_deliveries'][0]['command_id'] == (later_id if first_complete else first_id)
