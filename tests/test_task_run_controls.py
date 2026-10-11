from types import SimpleNamespace
import json

import pytest

from test_task_run_admission import runs,admit
from nerya.agent.command_runtime import runtime
from nerya.agent.task_runs import TaskRuns
from nerya.api.routes_task_runs import routes

pytestmark=pytest.mark.smoke


def fixture(tmp_path):
    config=runs(tmp_path).config;manager=runtime(config);manager.accepted_model=lambda _:None
    store=TaskRuns(manager);run=admit(store)
    client=SimpleNamespace(config=config,auth_actor_id='operator',auth_scopes=['execute:automation'])
    control=next(fn for method,path,fn in routes() if path=='/agent/runs/{run_id}/control')
    request={'run_id':run['run_id'],'action':'stop','expected_revision':run['run']['command_revision'],'client_request_id':'stop-original'}
    return store,client,control,request


def test_control_duplicate_and_different_payload_conflict(tmp_path):
    store,client,control,request=fixture(tmp_path)
    first=control(client,request);assert first['ok']
    second=control(client,request);assert second['duplicate']
    assert store.get(request['run_id'])['execution_status']=='removed'
    assert control(client,{**request,'action':'resume'})['error']=='run_control_idempotency_conflict'


def test_lost_control_ack_recovers_already_applied_effect(tmp_path):
    store,client,control,request=fixture(tmp_path)
    control(client,request)
    with store.store.transaction() as con:con.execute('UPDATE run_controls SET response_json=NULL')
    recovered=control(client,request)
    assert recovered['ok'] and recovered['duplicate']
    assert len(store.store.snapshot('session-daily')['commands'])==1


def test_control_scope_and_revision_are_server_owned(tmp_path):
    store,client,control,request=fixture(tmp_path)
    client.auth_scopes=['read:sessions']
    assert control(client,{**request,'_auth_scopes':['api:all']})['error']=='task_scope_denied'
    client.auth_scopes=['execute:automation']
    assert control(client,{**request,'expected_revision':999})['error']=='revision_conflict'
    assert store.get(request['run_id'])['execution_status']=='queued'
