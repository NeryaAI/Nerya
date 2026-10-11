"""Task adapters bridge trigger definitions to the existing command runtime."""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

from ..agent.command_store import CommandError
from ..agent.task_runs import TaskSpec, TaskRuns, revision, task_runs
from ..core.config import Config
from ..db.sqlite import connect


def schedule_spec(config, entry, *, now_ts=None, session_id=None):
    from ..triggers.scheduled_session import ScheduledSessionRunner
    now_ts=time.time() if now_ts is None else now_ts
    sid=session_id or ScheduledSessionRunner._session_ids_for(entry,now_ts)[0]
    raw=asdict(entry)
    from ..financial.task_binding import schedule_security_revision
    text=str(entry.payload.get("prompt") or entry.payload.get("text") or entry.payload.get("source_request") or entry.payload.get("message") or "").strip()
    if entry.session_kind=='script':text='Execute approved script '+str(entry.payload.get('script_id') or entry.target)
    if not text:
        raise CommandError("schedule_prompt_required",400)
    execution=dict(raw.get("execution") or {})
    scoped=config;mode='analysis'
    if entry.strategy_id:
        from ..strategies.package import load_package
        from ..strategies.agent_execution import execution_config
        package=load_package(config.paths,entry.strategy_id);mode=package.manifest.mode
        scoped=execution_config(config,package.manifest)
    request={"payload":{**entry.payload,"text":text},"kind":entry.kind}
    for name in ("model_provider","model_id","model_tier","permission_mode","work_mode","max_iterations","max_total_tool_calls","max_wall_seconds"):
        if name in execution:request[name]=execution[name]
    request.setdefault("max_wall_seconds",entry.session_ttl_seconds if entry.session_ttl_seconds is not None else 1800)
    from ..tools.capability_policy import normalise_tool_policy
    policy=normalise_tool_policy(scoped.get("agent.native.tool_policy"))
    task_policy=normalise_tool_policy(execution.get("tool_policy"))
    policy["allow_groups"].extend(task_policy["allow_groups"])
    policy["deny"]=list(dict.fromkeys([*policy["deny"],*task_policy["deny"]]))
    return TaskSpec("scheduled_agent",entry.id,sid,revision(raw),str(entry.payload.get("title") or entry.id),
        request,{"definition":raw,"effective_permissions":policy,"security_revision":schedule_security_revision(entry,config),
                 "mode":mode,"budget":{k:v for k,v in request.items() if k.startswith("max_")}},
        getattr(entry,"overlap_policy","skip"),entry.strategy_id)


def strategy_spec(config, event, route_result, *, prepared_task=None, prepared_inputs=None):
    from ..strategies.package import load_package
    from ..agent.session_profile import strategy_agent_session_id
    from ..strategies.agent_execution import execution_config
    from ..triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor
    from ..strategies.agent_task import StrategyAgentTask
    sid=route_result.strategy_id or event.strategy_id
    package=load_package(config.paths,str(sid or ""))
    scoped=execution_config(config,package.manifest)
    executor=StrategyAgentTaskExecutor(config)
    dummy=prepared_task if isinstance(prepared_task,StrategyAgentTask) else StrategyAgentTask(**prepared_task) if isinstance(prepared_task,dict) else StrategyAgentTask.dispatch(prompt="Prepare strategy event",session_key={})
    key=executor._resolve_session_key(package,event,dummy,event.event_id)
    session=strategy_agent_session_id(strategy_id=package.strategy_id,policy=package.manifest.agent_session.policy,session_key=key)
    execution=scoped.get("agent.native",{})
    request={"kind":"strategy.agent_task","payload":{"text":f"{package.manifest.title or package.strategy_id}: {event.kind}"}}
    for source,target in (("max_iterations","max_iterations"),("max_total_tool_calls","max_total_tool_calls"),("max_wall_seconds","max_wall_seconds"),("tier","model_tier")):
        if execution.get(source) is not None:request[target]=int(execution[source]) if source.startswith("max_") else execution[source]
    request["permission_mode"]=scoped.get("runtime.permission_mode","default")
    snapshot={"event":event.asdict(),"route":route_result.asdict(),"strategy_id":package.strategy_id,"effective_permissions":execution.get("tool_policy",{}),
              "profile":executor._profile_for(package),"prepared_inputs":prepared_inputs or {}}
    from ..financial.task_binding import strategy_security_revision
    snapshot.update(security_revision=strategy_security_revision(config,package.strategy_id),mode=package.manifest.mode,
                    budget={k:v for k,v in request.items() if k.startswith("max_")})
    if prepared_task is not None:snapshot["prepared_task"]=prepared_task.asdict() if hasattr(prepared_task,"asdict") else prepared_task
    if event.payload.get('expires_at') is not None:snapshot['expires_at']=float(event.payload['expires_at'])
    return TaskSpec("strategy_agent",package.strategy_id,session,package.content_hash,
                    package.manifest.title or package.strategy_id,request,snapshot,"coalesce",package.strategy_id)


def scoped_task_config(config, command_id):
    con=connect(config.paths.db)
    try:
        row=con.execute("""SELECT r.* FROM agent_runs r JOIN agent_run_commands l ON r.run_id=l.run_id
            WHERE l.command_id=?""",(command_id,)).fetchone()
        if not row:return config
        import json
        snapshot=json.loads(row["snapshot_json"])
        if snapshot.get("expires_at") is not None and snapshot["expires_at"]<=time.time():
            raise CommandError("task_trigger_expired")
        data=copy.deepcopy(config.data)
        from ..tools.capability_policy import normalise_tool_policy
        current=normalise_tool_policy(config.get("agent.native.tool_policy"))
        frozen=normalise_tool_policy(snapshot.get("effective_permissions"))
        current["allow_groups"].extend(group for group in frozen["allow_groups"] if group not in current["allow_groups"])
        current["deny"]=list(dict.fromkeys([*current["deny"],*frozen["deny"]]))
        data.setdefault("agent",{}).setdefault("native",{})["tool_policy"]=current
        previous=con.execute("""SELECT c.result_json FROM agent_commands c JOIN agent_run_commands l ON c.command_id=l.command_id
            WHERE l.run_id=? AND c.command_id!=? AND c.kind!='guide' AND c.result_json IS NOT NULL""",(row['run_id'],command_id)).fetchall()
        completed=[json.loads(item['result_json']) for item in previous]
        used={'max_wall_seconds':sum(float(item.get('execution_elapsed_ms') or 0)/1000 for item in completed),
            'max_iterations':sum(int(item.get('iterations') or 0) for item in completed),
            'max_total_tool_calls':sum(len(item.get('tool_trace') or []) for item in completed)}
        for key,limit in snapshot.get('budget',{}).items():
            if key not in used:continue
            if float(limit)>0:
                remaining=float(limit)-used[key]
                if remaining<=0:raise CommandError('task_run_budget_exhausted')
                data['agent']['native'][key]=remaining if key=='max_wall_seconds' else int(remaining)
            else:data['agent']['native'][key]=0
        if snapshot.get('work_mode')=='plan':data['agent']['native']['plan_only']=True
        data.setdefault("runtime",{})["task_run_id"]=row["run_id"]
        data["runtime"]["task_actor_id"]=row["actor_id"]
        data["runtime"]["task_command_id"]=command_id
        data["agent"]["native"]["attached_skills"]=snapshot.get("definition",{}).get("attached_skills") or snapshot.get("profile",{}).get("attached_skills")
        return Config(paths=config.paths,data=data)
    finally:
        con.close()


def current_task_policy(config):
    from ..core import yaml_io
    from ..tools.capability_policy import normalise_tool_policy
    raw=yaml_io.load(config.paths.config,default={}) if config.paths.config.exists() else config.data
    policy=normalise_tool_policy(((raw.get('agent') or {}).get('native') or {}).get('tool_policy'))
    rid=config.get('runtime.task_run_id')
    if not rid:return policy
    run=task_runs(config).get(rid)
    from ..financial.task_binding import task_security_revision
    if task_security_revision(config,run['task_kind'],run['task_id'])!=run['snapshot']['security_revision']:
        return {'deny':['*']}
    if run['snapshot'].get('expires_at',float('inf'))<=time.time():return {'deny':['*']}
    return policy


def execute_task_run(config,request):
    import json
    rid=request["_task_run_id"]
    runs=task_runs(config)
    run=runs.get(rid)
    if run['snapshot'].get('admission_expires_at',float('inf'))<=time.time():
        with runs.store.transaction() as con:
            con.execute("UPDATE agent_runs SET admission_status='skipped',reason='expired_queued_trigger',revision=revision+1,updated_at=? WHERE run_id=?",(time.time(),rid))
        return {'ok':True,'no_action':True,'stopped_reason':'completed','final_text':'Queued occurrence expired before execution.',
            'run_id':rid,'session_id':run['session_id'],'turn_id':request['turn_id']}
    from ..agent.streaming import get_default_bus
    def stage(name,status):
        get_default_bus().publish("task.stage",session_id=run["session_id"],turn_id=request["turn_id"],run_id=rid,
                                  stage=name,status=status)
    stage("prepare","running")
    cid=run["command_id"]
    effective=scoped_task_config(config,cid)
    native=effective.data.setdefault("agent",{}).setdefault("native",{})
    for name in ("max_iterations","max_total_tool_calls","max_wall_seconds"):
        if name in native:request[name]=native[name]
    token=request.get('_task_cancel_token')
    wall=float(native.get('max_wall_seconds') or 0)
    if token is not None and wall>0:token.deadline_s=min(token.deadline_s or float('inf'),time.time()+wall)
    fixed=run["snapshot"].get("accepted_model")
    if fixed:
        request={**request,"model_provider":fixed["provider"],"model_id":fixed["model"]}
    if run["task_kind"]=="strategy_agent":
        from ..triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor
        from ..triggers.event import TriggerEvent
        from ..triggers.router import RouterResult
        from ..strategies.package import load_package
        snapshot=run["snapshot"]
        if load_package(config.paths,run["task_id"]).content_hash!=run["source_revision"]:
            raise CommandError("strategy_version_changed")
        event=TriggerEvent(**snapshot["event"])
        route=RouterResult(**snapshot["route"])
        executor=StrategyAgentTaskExecutor(effective)
        from ..strategies.agent_task import StrategyAgentTask
        prepared=snapshot.get('prepared_task')
        if isinstance(prepared,dict):prepared=StrategyAgentTask(**prepared)
        response=executor.execute(event,route,expected_hash=run["source_revision"],
            prepared_task=prepared,prepared_inputs=snapshot.get("prepared_inputs"),
            cancel_token=request.get('_task_cancel_token'),
            execution_session_id=run["session_id"],execution_turn_id=request["turn_id"],execution_task_id=rid)
        result=dict(response.result)
        result.update(ok=response.status not in {"failed"},session_id=run["session_id"],turn_id=request["turn_id"],run_id=rid)
        if response.status=="skipped":result.update(no_action=True,stopped_reason="completed")
        if response.error:result.update(ok=False,error=response.error)
        if (response.error or {}).get('code')=='script_preparation_unconfirmed':result['execution_status']='unconfirmed'
        stage("execute","completed" if result.get("ok") else "failed")
        return checked_result(config,run,result)
    from ..triggers.schedule import ScheduleEntry
    from .scheduled_session_factory import default_kernel_factory
    from ..api.routes_agent import _inject_trusted_actor
    entry=ScheduleEntry(**run["snapshot"]["definition"])
    if entry.session_kind=='script':
        from ..triggers.scheduled_script import ScheduledScriptRunner
        from ..scripts.runner import run_script
        stage('prepare','completed');stage('execute','running')
        token=request.get('_task_cancel_token')
        if token is not None:token.raise_if_cancelled()
        outcome=ScheduledScriptRunner(effective,run_script).run_once(entry)
        if token is not None:token.raise_if_cancelled()
        stage('execute','completed' if outcome.ok else 'failed')
        return {'ok':outcome.ok,'stopped_reason':'completed' if outcome.ok else 'script_failed',
            'final_text':str((outcome.result or {}).get('summary') or '') if isinstance(outcome.result,dict) else '',
            'script_receipt':outcome.asdict(),'error':outcome.error,'run_id':rid,'session_id':run['session_id'],'turn_id':request['turn_id']}
    trigger={"id":run["trigger_id"],"event_id":run["trigger_id"],"source":"scheduled_session",
             "kind":entry.kind,"target":entry.target,"strategy_id":entry.strategy_id,
             "payload":{**entry.payload,"text":request["payload"]["text"],"schedule_id":entry.id,"run_id":rid}}
    trigger=_inject_trusted_actor(trigger,request)
    kernel=default_kernel_factory(effective)
    from ..tools.permissions import PermissionMode
    kernel.permission_mode=PermissionMode(request.get("permission_mode") or effective.get("runtime.permission_mode","default"))
    if fixed:
        kernel.model_provider=fixed["provider"];kernel.model_id=fixed["model"]
    stage("prepare","completed");stage("execute","running")
    turn=kernel.run_turn(trigger=trigger,strategy_id=entry.strategy_id,session_id=run["session_id"],
        turn_id=request["turn_id"],attached_skills=entry.attached_skills or None,cancel_token=request.get('_task_cancel_token'))
    result=turn.asdict() if hasattr(turn,"asdict") else dict(vars(turn))
    stage("execute","returned")
    return checked_result(config,run,{**result,"run_id":rid,"session_id":run["session_id"],"turn_id":request["turn_id"]})


def checked_result(config,run,result):
    if result.get('stopped_reason') in {'user_input_pending','approval_required','cancelled'} or result.get('awaiting_approval'):return result
    from ..strategies.agent_execution import task_output_error
    if task_output_error(str(result.get('final_text') or '')):
        return {**result,'ok':False,'error':'output_protocol_error','stopped_reason':'output_protocol_error'}
    required=run['snapshot'].get('definition',{}).get('execution',{}).get('required_files',[])
    evidence=[]
    import hashlib
    for entry in required:
        path=entry if isinstance(entry,str) else entry.get('path') if isinstance(entry,dict) else None
        if not isinstance(path,str) or not path:return {**result,'ok':False,'error':'invalid_required_output','stopped_reason':'required_output_missing'}
        target=(config.paths.root/path).resolve()
        if not target.is_relative_to(config.paths.root.resolve()) or not target.is_file():
            return {**result,'ok':False,'error':'required_output_missing','stopped_reason':'required_output_missing','missing_output':path}
        digest=hashlib.sha256()
        with target.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        if isinstance(entry,dict) and entry.get('sha256') and digest.hexdigest()!=entry['sha256']:
            return {**result,'ok':False,'error':'required_output_hash_mismatch','stopped_reason':'required_output_missing'}
        evidence.append({'kind':'file','path':target.relative_to(config.paths.root).as_posix(),'sha256':digest.hexdigest(),'size':target.stat().st_size,'verified':True})
    if evidence:return {**result,'output_evidence':evidence}
    return result


def complete_task_run(config,cid,result,state):
    from ..agent.command_runtime import runtime
    with runtime(config).store.transaction() as con:
        command=con.execute('SELECT state FROM agent_commands WHERE command_id=?',(cid,)).fetchone()
        if command:state=command['state']
    con=connect(config.paths.db)
    try:
        row=con.execute("SELECT run_id FROM agent_run_commands WHERE command_id=?",(cid,)).fetchone()
    finally:con.close()
    if not row:return
    runs=task_runs(config);run=runs.get(row[0])
    if run['admission_status']=='skipped':return
    if state in {'failed','interrupted'}:
        with runs.store.transaction() as con:
            queue=con.execute('SELECT * FROM agent_command_queues WHERE session_id=?',(run['session_id'],)).fetchone()
            uncertain=con.execute("SELECT 1 FROM financial_actions WHERE run_id=? AND state IN ('submitting','submitted','confirming','unconfirmed','needs_recovery','awaiting_approval')",(run['run_id'],)).fetchone()
            foreign=con.execute("SELECT 1 FROM agent_commands c WHERE c.session_id=? AND c.state='queued' AND NOT EXISTS(SELECT 1 FROM agent_run_commands l WHERE l.command_id=c.command_id)",(run['session_id'],)).fetchone()
            if queue and queue['pause_reason'] in {'failed','interrupted','stopping'} and not uncertain and not foreign and not runs.store._decision_pending(con,run['session_id']):
                con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?",(run['session_id'],))
    if state in {"succeeded","failed"} and run["task_kind"]=="scheduled_agent":
        from ..triggers.schedule import ScheduleEntry
        from ..triggers.schedule_clock import next_due
        definition=run['snapshot'].get('definition')
        with runs.store.transaction() as con:
            if definition:
                con.execute('INSERT OR IGNORE INTO task_schedule_state(task_id,source_revision,next_due,updated_at) VALUES (?,?,?,?)',
                    (run['task_id'],run['source_revision'],next_due(ScheduleEntry(**definition),time.time()),time.time()))
            con.execute("""UPDATE task_schedule_state SET consecutive_failures=
                CASE WHEN ?='failed' THEN consecutive_failures+1 ELSE 0 END,
                paused_reason=CASE WHEN ?='failed' AND consecutive_failures>=2 THEN 'consecutive_failures' ELSE paused_reason END,
                updated_at=? WHERE task_id=?""",(state,state,time.time(),run["task_id"]))
    definition=run["snapshot"].get("definition",{})
    if not definition.get("delivery_targets") or state!="succeeded":return
    # 执行先落库，通知独立发送；通知失败绝不重跑资金动作。
    from .run_effects import enqueue_delivery
    enqueue_delivery(config,run["run_id"],definition["delivery_targets"])
