"""Real admission/publication with isolated DB and disabled worker/network."""
import json
import pytest
import test_assistant_receipts as receipt_tests
from test_assistant_receipts import request, create, followup, verify
from nerya.task_delivery import start_delivery, update_delivery, DeliveryError

recovery_env = receipt_tests.env

@pytest.fixture
def recovery(recovery_env):
    env = recovery_env
    old = create(env, *request(env, language='zh'))
    sid, cid = old['task_session_id'], old['command_id']
    row = start_delivery(env.config.paths, command_id=cid, session_id=sid,
        skill_id='generic', workflow_id='report', requested_scope={'summary': 'Report'},
        stages=[{'id': 'deliver', 'state': 'completed', 'required_checks': ['reported']}])
    update_delivery(env.config.paths, command_id=cid, session_id=sid,
        expected_revision=row['delivery_revision'], checks=[{'check_id': 'reported', 'status': 'pending'}])
    env.manager.store.claim(sid, 'old-worker')
    assert env.manager.store.finish(cid, sid, 'old-worker', 'succeeded', {'final_text': 'Original'})
    before = env.manager.store.snapshot(sid, cid)['command']
    text = '继续交付，不重复执行。'
    cfg, contract = request(env, 'task_follow_up', target=old['task_id'],
        cid='followup-parent', text=text, language='zh')
    sent = followup(env, cfg, contract, text)
    return env, old, before, cfg, contract, sent, text

def test_admission_reference_and_retry_are_frozen(recovery):
    env, old, before, cfg, contract, sent, text = recovery
    new = env.manager.store.snapshot(old['task_session_id'], sent['command_id'])['command']
    ref = new['context']['unresolved_deliveries'][0]
    assert ref['command_id'] == old['command_id']
    assert ref['delivery_revision'] == before['delivery']['delivery_revision']
    assert ref['command_revision'] == before['revision']
    assert 'check_pending:reported' in ref['reasons']
    assert 'checks' not in ref and new['delivery'] is None
    assert new['turn_id'] != before['turn_id']
    with env.manager.store.transaction() as con:
        saved = json.loads(con.execute('SELECT request_json FROM agent_commands WHERE command_id=?',
            (sent['command_id'],)).fetchone()[0])
    assert 'resume_turn_id' not in saved
    update_delivery(cfg.paths, command_id=old['command_id'], session_id=old['task_session_id'],
        expected_revision=before['delivery']['delivery_revision'], limitations=['changed'])
    assert followup(env, cfg, contract, text)['duplicate']
    assert env.manager.store.snapshot(old['task_session_id'], sent['command_id'])['command']['context'] == new['context']
    with pytest.raises(DeliveryError, match='revision_conflict'):
        update_delivery(cfg.paths, command_id=old['command_id'], session_id=old['task_session_id'],
            expected_revision=before['delivery']['delivery_revision'], limitations=[])

def test_task_read_and_resume_expose_command_bound_delivery_without_inheriting(recovery):
    from nerya.agent.assistant_coordination import execution_context

    env, old, before, cfg, contract, sent, _ = recovery
    sid = old['task_session_id']
    read = env.work.read_task(env.sid, old['task_id'], 'owner')
    assert read['command']['command_id'] == sent['command_id']
    assert read['command']['delivery'] is None
    historical = read['delivery_history'][0]
    assert historical['command_id'] == old['command_id']
    assert historical['revision'] == before['revision']
    assert historical['delivery'] == before['delivery']
    assert historical['delivery']['checks'][0]['status'] == 'pending'
    original_context = json.loads(execution_context(cfg.paths, sid, old['command_id']))
    model_delivery = original_context['command']['delivery']
    for key in ('delivery_id', 'delivery_revision', 'command_id'):
        assert model_delivery[key] == before['delivery'][key]
    assert model_delivery['requested_scope'] == {key: before['delivery']['requested_scope'][key]
        for key in ('summary', 'scope_hash', 'authority', 'limitations_are_authorization')}
    for key in ('allowed', 'status', 'reasons'):
        assert model_delivery['completion'][key] == before['delivery']['completion'][key]
    assert 'limitations' not in model_delivery
    assert 'reason' not in model_delivery['recovery']
    assert original_context['previous_command'] is None
    direct = env.work.read_task(env.sid, old['task_id'], 'owner', command_id=old['command_id'])
    assert direct['command']['delivery'] == before['delivery']
    assert direct['result_command_id'] == old['command_id']
    assert direct['content'] == 'Original'
    from nerya.tools.native.tasks import assistant_task_collaboration_handler
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall
    result = assistant_task_collaboration_handler(
        ToolCall(id='read-old', turn_id='read-turn', name='assistant_task_read',
                 arguments={'task_id': old['task_id'], 'command_id': old['command_id']}),
        config=cfg, session_id=env.sid, actor_id='owner', operation='read')
    assert tool_json_data(result)['command']['delivery'] == before['delivery']
    from nerya.agent.assistants import AssistantError
    with pytest.raises(AssistantError, match='assistant_task_command_not_found'):
        env.work.read_task(env.sid, old['task_id'], 'owner', command_id=contract['binding']['command_id'])

    context = json.loads(execution_context(cfg.paths, sid, sent['command_id']))
    assert context['delivery_history'][0]['delivery'] == model_delivery
    assert context['delivery_history'][0]['command_id'] == historical['command_id']
    assert context['command']['delivery'] is None
    cfg, contract = request(env, 'task_read', target=old['task_id'], cid='read-parent')
    published = verify(cfg, contract, sent)
    assert published['delivery_history'] == read['delivery_history']
    assert published['command']['command_id'] == sent['command_id']
    assert env.manager.store.snapshot(sid, old['command_id'])['command'] == before
    assert env.manager.store.snapshot(sid, sent['command_id'])['command']['delivery'] is None

def test_publishers_keep_blocker_and_full_untrusted_reply(recovery):
    env, old, before, cfg, contract, sent, _ = recovery
    answer = '任务已完整交付。' + '完整原文' * 1300
    env.manager.store.claim(old['task_session_id'], 'new-worker')
    assert env.manager.store.finish(sent['command_id'], old['task_session_id'], 'new-worker',
        'succeeded', {'final_text': answer})
    env.work.sync(env.sid)
    rows = [env.work.tasks(sid=env.sid)[0], env.work.read_task(env.sid, old['task_id'], 'owner'),
            json.loads(env.work.context(env.sid))['tasks'][0]]
    cfg, contract = request(env, 'task_read', target=old['task_id'], cid='read-parent', language='zh')
    rows.append(verify(cfg, contract, sent))
    with env.manager.store.transaction() as con:
        msg = con.execute('SELECT content,meta_json FROM agent_messages WHERE message_id=?',
            ('assistant_result:' + sent['command_id'],)).fetchone()
        stored = json.loads(con.execute('SELECT result_json FROM assistant_results WHERE command_id=?',
            (sent['command_id'],)).fetchone()[0])
    meta = json.loads(msg['meta_json'])
    for row in rows + [meta, stored]:
        assert row['task_complete'] is False
        assert row['delivery_completion']['allowed'] is False
        assert row['unresolved_deliveries'][0]['command_id'] == old['command_id']
        assert row['publication']['verification_status'] == 'unverified'
        assert row['publication']['language'] == 'zh'
    assert msg['content'] == answer and stored['final_text'] == answer
    assert meta['reported_result']['text'] == answer
    assert meta['reported_result']['authority'] == 'untrusted_model_output'
    assert env.manager.store.snapshot(old['task_session_id'], old['command_id'])['command'] == before
