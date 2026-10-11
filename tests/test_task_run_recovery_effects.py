import json
import time
from types import SimpleNamespace

import pytest

from test_task_run_admission import runs,admit,spec
from nerya.sdk.run_effects import enqueue_delivery,delivery_tick,financial_tick
from nerya.sdk.run_migration import import_legacy_runs
from nerya.agent.command_runtime import runtime
from nerya.agent.task_runs import TaskRuns
from nerya.core import jsonl

pytestmark=pytest.mark.smoke


def test_thousand_runs_page_without_duplicates_or_large_payload(tmp_path):
    store=runs(tmp_path)
    for i in range(1000):admit(store,str(i))
    expected={r['run_id'] for r in store.list(limit=100)['runs']}
    page=store.list(limit=50);seen=[];cursor=page['next_cursor']
    seen.extend(r['run_id'] for r in page['runs'])
    newest=admit(store,'later')['run_id']
    while cursor:
        page=store.list(limit=50,cursor=cursor);seen.extend(r['run_id'] for r in page['runs']);cursor=page['next_cursor']
    assert len(seen)==len(set(seen))==1000 and newest not in seen
    assert expected.issubset(seen)
    assert all('turn' not in r['result'] and 'definition' not in r['snapshot'] for r in page['runs'])


def test_events_cover_continuations_and_global_cursor(tmp_path):
    store=runs(tmp_path);run=admit(store)
    store.store.record_event(run['command_id'],{'event_id':'one','kind':'task.stage','session_id':run['session_id'],'turn_id':run['turn_id']})
    claimed=store.store.claim(run['session_id'],'worker')
    store.store.finish(claimed['command_id'],run['session_id'],'worker','interrupted',{})
    later=store.manager.submit({'command_id':'resume-test','session_id':run['session_id'],'command_type':'resume','_auth_actor_id':'operator',
        'request':{'payload':{'text':'Continue'},'resume_turn_id':run['turn_id'],'continuation_feedback':'Continue with existing inputs'}},start=False)
    store.store.record_event(later['command']['command_id'],{'event_id':'two','kind':'financial.receipt','session_id':run['session_id'],'turn_id':run['turn_id']})
    first=store.events(run['run_id'],limit=1)
    second=store.events(run['run_id'],after=first['next_seq'])
    assert first['has_more'] and second['events'][-1]['event_id']=='two'


def test_skipped_filter_and_legacy_import_are_exact_and_idempotent(tmp_path):
    store=runs(tmp_path);a=admit(store);admit(store,'next','coalesce')
    assert store.list(state='skipped')['runs'][0]['run_id']==a['run_id']
    row={'schedule_id':'old','session_id':'old-session','turn_id':'old-turn','execution_status':'completed',
        'ts_epoch':time.time(),'final_text':'Historical analysis'}
    jsonl.append(store.config.paths.journal('scheduled_session'),row)
    assert import_legacy_runs(store.config)['imported']==1
    assert import_legacy_runs(store.config)['already_imported']==1
    legacy=TaskRuns(runtime(store.config)).list(task_id='old')['runs'][0]
    assert legacy['execution_status']=='historical_unverified' and not legacy.get('command_id')
    assert store.store.snapshot('old-session')['commands']==[]


def test_restart_only_kicks_never_started_system_tasks(tmp_path,monkeypatch):
    store=runs(tmp_path);admit(store)
    recovered=type(store.manager)(store.config,lambda *_:{},epoch='new-process');kicked=[]
    monkeypatch.setattr(recovered,'kick',kicked.append)
    recovered.recover_tasks();assert kicked==['session-daily']
    snapshot=recovered.store.snapshot('session-daily');recovered.store.control('session-daily','pause',revision=snapshot['queue']['revision'])
    kicked.clear();third=type(store.manager)(store.config,lambda *_:{},epoch='third-process');monkeypatch.setattr(third,'kick',kicked.append)
    third.recover_tasks();assert kicked==[]


def test_delivery_retries_only_delivery_and_preserves_successful_targets(tmp_path):
    store=TaskRuns(runtime(runs(tmp_path).config));run=admit(store)
    with store.store.transaction() as con:
        con.execute('UPDATE agent_runs SET snapshot_json=? WHERE run_id=?',(json.dumps({'definition':{'id':'daily','target':'main','strategy_id':None,'session_kind':'agent'},'title':'Daily'}),run['run_id']))
    enqueue_delivery(store.config,run['run_id'],[{'kind':'webhook','url':'https://example.invalid/a'},{'kind':'webhook','url':'https://example.invalid/b'}])
    calls=[]
    def send(c,e,r):
        target=e.delivery_targets[0];calls.append(target['url']);return [{'ok':not target['url'].endswith('/b') or len(calls)>2}]
    assert delivery_tick(store.config,deliver=send,now=time.time())
    assert delivery_tick(store.config,deliver=send,now=time.time())
    assert delivery_tick(store.config,deliver=send,now=time.time()+100)
    assert calls==['https://example.invalid/a','https://example.invalid/b','https://example.invalid/b']
    assert store.get(run['run_id'])['delivery_status']=='delivered'
    assert len(store.store.snapshot('session-daily')['commands'])==1


def test_delivery_unknown_does_not_replay_after_restart(tmp_path):
    store=TaskRuns(runtime(runs(tmp_path).config));run=admit(store)
    enqueue_delivery(store.config,run['run_id'],[{'kind':'webhook','url':'https://example.invalid'}])
    with store.store.transaction() as con:con.execute("UPDATE run_deliveries SET state='sending',lease_until=0")
    assert not delivery_tick(store.config,deliver=lambda *_:pytest.fail('unknown delivery replayed'))


def test_followup_retains_run_model_and_scope_without_guide_state_takeover(tmp_path):
    store=runs(tmp_path);first=admit(store)
    command=store.store.claim('session-daily','worker')
    store.store.finish(command['command_id'],'session-daily','worker','succeeded',{'stopped_reason':'completed'})
    follow=store.manager.submit({'command_id':'follow-up','session_id':'session-daily','_auth_actor_id':'operator',
        'request':{'payload':{'text':'Explain this result'},'model_provider':'different','model_id':'different'}},start=False)['command']
    linked=store.get(first['run_id']);assert linked['command_id']==follow['command_id']
    assert follow['context']['accepted_model']=={'provider':'fake','model':'fake-model'}
    with store.store.transaction() as con:
        saved=json.loads(con.execute('SELECT request_json FROM agent_commands WHERE command_id=?',('follow-up',)).fetchone()[0])
    assert saved['model_id']=='fake-model' and saved['permission_mode']==linked['snapshot']['permission_mode']


def test_failed_task_releases_system_hold_until_third_failure_pauses_plan(tmp_path):
    from nerya.triggers.schedule import ScheduleEntry,save_schedules
    from nerya.sdk.task_execution import schedule_spec,complete_task_run
    store=TaskRuns(runtime(runs(tmp_path).config));entry=ScheduleEntry(id='daily',kind='agent.task',session_kind='agent',every_seconds=60,payload={'prompt':'Read data'})
    save_schedules(store.config.paths,[entry]);task=schedule_spec(store.config,entry)
    for index in range(3):
        receipt=store.admit(task,actor='operator',trigger={},trigger_id=str(index),start=False)
        row=store.store.claim(task.session_id,'worker');assert row
        store.store.finish(row['command_id'],task.session_id,'worker','failed',{'ok':False})
        complete_task_run(store.config,row['command_id'],{'ok':False},'failed')
        assert not store.store.snapshot(task.session_id)['queue']['paused']
    with store.store.transaction() as con:clock=con.execute('SELECT * FROM task_schedule_state WHERE task_id=?',('daily',)).fetchone()
    assert clock['consecutive_failures']==3 and clock['paused_reason']=='consecutive_failures'


def test_required_files_need_real_workspace_evidence(tmp_path):
    from nerya.sdk.task_execution import checked_result
    cfg=runs(tmp_path).config;run={'snapshot':{'definition':{'execution':{'required_files':['report.txt']}}}}
    result={'stopped_reason':'completed','final_text':'The report is ready.'}
    assert checked_result(cfg,run,result)['error']=='required_output_missing'
    (tmp_path/'report.txt').write_text('verified report')
    observed=checked_result(cfg,run,result)
    assert observed['output_evidence'][0]['verified'] and observed['output_evidence'][0]['size']==15
    run['snapshot']['definition']['execution']['required_files']=['../foreign.txt']
    assert checked_result(cfg,run,result)['error']=='required_output_missing'


def test_continuation_cannot_reset_run_budget(tmp_path):
    from dataclasses import replace
    from nerya.sdk.task_execution import scoped_task_config
    store=runs(tmp_path);task=replace(spec(),request={'payload':{'text':'Work with a bounded budget'},'max_wall_seconds':10,'max_iterations':3,'max_total_tool_calls':4})
    first=store.admit(task,actor='operator',trigger={},trigger_id='bounded',start=False)
    command=store.store.claim(task.session_id,'worker')
    store.store.finish(command['command_id'],task.session_id,'worker','succeeded',{'stopped_reason':'completed','execution_elapsed_ms':4000,'iterations':1,'tool_trace':[{}]})
    follow=store.manager.submit({'command_id':'bounded-follow','session_id':task.session_id,'_auth_actor_id':'operator',
        'request':{'payload':{'text':'Continue'},'max_wall_seconds':999,'max_iterations':999,'max_total_tool_calls':999}},start=False)['command']
    config=scoped_task_config(store.config,follow['command_id'])
    assert config.get('agent.native.max_wall_seconds')==6 and config.get('agent.native.max_iterations')==2 and config.get('agent.native.max_total_tool_calls')==3


def test_scoped_task_config_does_not_poison_runtime_singleton(tmp_path):
    from nerya.sdk.task_execution import scoped_task_config
    cfg=runs(tmp_path).config;manager=runtime(cfg);store=TaskRuns(manager);first=admit(store)
    scoped=scoped_task_config(cfg,first['command_id'])
    assert scoped.get('runtime.task_run_id')==first['run_id']
    assert runtime(scoped).config is cfg
