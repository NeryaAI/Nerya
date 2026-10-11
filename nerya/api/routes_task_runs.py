"""Chat-first task admission and ownership-checked run projections."""
from functools import wraps
import json,time

from ..agent.command_store import CommandError
from ..agent.task_runs import task_runs


def _identity(client):
    actor=getattr(client,"auth_actor_id",None)
    scopes=set(getattr(client,"auth_scopes",()))
    if not actor:raise CommandError("authenticated_actor_required",403)
    return actor,scopes,"api:all" in scopes or "admin:ops" in scopes


def _safe(fn):
    @wraps(fn)
    def wrapped(client,payload):
        try:return fn(client,payload or {})
        except CommandError as exc:return {"ok":False,"error":exc.code,"_status":exc.status,"retrying":False}
        except (ValueError,TypeError,KeyError):return {"ok":False,"error":"invalid_task_request","_status":400,"retrying":False}
    return wrapped


def routes():
    @_safe
    def descriptor(client,p):
        _identity(client)
        from ..sdk.task_execution import schedule_spec,strategy_spec
        from ..triggers.schedule import load_schedules
        from ..triggers.event import TriggerEvent
        from ..triggers.router import RouterResult
        if p.get('task_kind')=='scheduled_agent':
            entry=next((e for e in load_schedules(client.config.paths) if e.id==p.get('task_id') and e.session_kind in {'agent','script'}),None)
            if not entry:raise CommandError('task_not_found',404)
            spec=schedule_spec(client.config,entry);owner=entry.owner_actor_id
        elif p.get('task_kind')=='strategy_agent':
            event=TriggerEvent.new(event_id='task_descriptor',source='user_command',kind='strategy.inspect',target='skill:strategy.agent_task',strategy_id=str(p.get('task_id') or ''),payload={})
            spec=strategy_spec(client.config,event,RouterResult(event.event_id,'routed',event.target,None,event.strategy_id))
            owner=client.config.get('runtime.strategy_owner_actor_id','local:loopback')
        else:raise CommandError('invalid_task_kind',400)
        return {'ok':True,'task':{'task_kind':spec.task_kind,'task_id':spec.task_id,'title':spec.title,'session_id':spec.session_id,
            'source_revision':spec.source_revision,'security_revision':spec.snapshot['security_revision'],'owner_actor_id':owner,
            'effective_permissions':spec.snapshot.get('effective_permissions'),'budget':spec.snapshot.get('budget'),'mode':spec.snapshot.get('mode')}}
    @_safe
    def create(client,p,*,retry_of=None):
        actor,scopes,operator=_identity(client)
        kind=p.get("task_kind");required="execute:strategy" if kind=="strategy_agent" else "execute:automation"
        if required not in scopes and "api:all" not in scopes:raise CommandError("task_scope_denied",403)
        if not isinstance(p.get("client_request_id"),str) or not p["client_request_id"].strip():
            raise CommandError("client_request_id_required",400)
        from ..sdk.task_execution import schedule_spec,strategy_spec
        from ..triggers.schedule import load_schedules
        from ..triggers.event import TriggerEvent
        from ..triggers.router import RouterResult
        if kind=="scheduled_agent":
            entry=next((e for e in load_schedules(client.config.paths) if e.id==p.get("task_id") and e.session_kind in {'agent','script'}),None)
            if entry is None:raise CommandError("task_not_found",404)
            spec=schedule_spec(client.config,entry)
            trigger={"schedule_id":entry.id,"reason":"operator_manual_run"}
        elif kind=="strategy_agent":
            sid=str(p.get("task_id") or "")
            event=TriggerEvent.new(event_id="manual_"+p["client_request_id"],source="user_command",kind="strategy.manual",
                                   target="skill:strategy.agent_task",strategy_id=sid,payload={})
            route=RouterResult(event.event_id,"routed",event.target,None,sid)
            spec=strategy_spec(client.config,event,route)
            trigger=event.asdict()
        else:raise CommandError("invalid_task_kind",400)
        if p.get("expected_task_revision")!=spec.source_revision:raise CommandError("task_revision_conflict")
        receipt=task_runs(client.config).admit(spec,actor=actor,trigger=trigger,trigger_kind="manual",
            trigger_id=p["client_request_id"],client_request_id=p["client_request_id"],operator=operator,
            retry_of_run_id=retry_of)
        return {**receipt,"_status":202}

    @_safe
    def history(client,p):
        actor,scopes,operator=_identity(client)
        keys=("task_kind","task_id","session_id","client_request_id","cursor","limit","state","since","until")
        return task_runs(client.config).list(actor=actor,operator=operator,**{k:p[k] for k in keys if k in p})

    @_safe
    def get(client,p):
        actor,_,operator=_identity(client)
        return {"ok":True,"run":task_runs(client.config).get(p["run_id"],actor=actor,operator=operator)}

    @_safe
    def events(client,p):
        actor,_,operator=_identity(client);runs=task_runs(client.config)
        run=runs.get(p["run_id"],actor=actor,operator=operator)
        return runs.events(run["run_id"],after=p.get("after_seq",0),limit=p.get("limit",500))

    @_safe
    def control(client,p):
        actor,scopes,operator=_identity(client);runs=task_runs(client.config)
        run=runs.get(p["run_id"],actor=actor,operator=operator)
        required="execute:strategy" if run["task_kind"]=="strategy_agent" else "execute:automation"
        if required not in scopes and "api:all" not in scopes:raise CommandError("task_scope_denied",403)
        if not isinstance(p.get("client_request_id"),str) or not p["client_request_id"].strip():
            raise CommandError("client_request_id_required",400)
        action=p.get("action")
        if action not in {"stop","resume","reconcile","rerun"}:raise CommandError("invalid_run_control",400)
        from ..agent.task_runs import revision
        from ..agent.command_store import encode
        fingerprint=revision({key:p.get(key) for key in ('run_id','action','expected_revision','feedback','run_only','expected_task_revision')})
        request_id=p['client_request_id']
        with runs.store.transaction() as con:
            saved=con.execute('SELECT * FROM run_controls WHERE actor_id=? AND client_request_id=?',(actor,request_id)).fetchone()
            if saved and saved['fingerprint']!=fingerprint:raise CommandError('run_control_idempotency_conflict')
            if saved and saved['response_json']:return {**json.loads(saved['response_json']),'duplicate':True}
            if not saved:con.execute('INSERT INTO run_controls VALUES (?,?,?,?,?,?,NULL,?)',
                (actor,request_id,run['run_id'],action,fingerprint,run.get('command_id'),time.time()))
        def receipt(result):
            with runs.store.transaction() as con:con.execute('UPDATE run_controls SET response_json=? WHERE actor_id=? AND client_request_id=?',
                (encode(result),actor,request_id))
            return result
        if saved:
            if action=='resume':
                with runs.store.transaction() as con:existing=con.execute('SELECT * FROM agent_commands WHERE command_id=? AND actor_id=? AND session_id=?',('resume_'+request_id,actor,run['session_id'])).fetchone()
                if existing:return receipt({'ok':True,'duplicate':True,'command':runs.store.public(existing)})
            if action=='rerun':
                old=runs.list(actor=actor,operator=operator,client_request_id=request_id)['runs']
                if old and old[0]['run_id']!=run['run_id']:return receipt({'ok':True,'duplicate':True,'run_id':old[0]['run_id'],'session_id':old[0]['session_id']})
            if action=='stop' and run.get('command_id')==saved['command_id'] and run['execution_status'] in {'stopping','interrupted','removed'}:
                return receipt({'ok':True,'duplicate':True,'run':run})
        if action=="rerun":
            if p.get("expected_revision")!=run.get("command_revision",run["revision"]):raise CommandError("revision_conflict")
            return receipt(create.__wrapped__(client,{**p,"task_kind":run["task_kind"],"task_id":run["task_id"]},retry_of=run['run_id']))
        if not run.get("command_id"):raise CommandError("run_not_admitted")
        command=runs.store.snapshot(run["session_id"],run["command_id"])["command"]
        if p.get("expected_revision")!=command["revision"]:raise CommandError("revision_conflict")
        if action=="resume":
            request={"source":"interaction_continue","resume_turn_id":run["turn_id"],"strategy_id":run["snapshot"].get("strategy_id"),
                     "run_only":p.get('run_only') is True,
                     "continuation_feedback":str(p.get("feedback") or "Continue this run within its existing permissions.")}
            return receipt(runs.manager.submit({"command_id":"resume_"+p["client_request_id"],
                "session_id":run["session_id"],"command_type":"resume","_auth_actor_id":actor,"_auth_scopes":list(scopes),"request":request}))
        if action=='stop':return receipt({'ok':True,'run':runs.stop(run['run_id'])})
        if action=="reconcile":
            from ..financial.store import FinancialStore
            from ..financial.gateway import FinancialGateway
            from ..financial.contracts import FinancialContext
            context=FinancialContext(actor,frozenset(scopes))
            for fund in FinancialStore(client.config).list_actions(context,operator=operator,run_id=run["run_id"]):
                FinancialGateway(client.config).reconcile(context,fund["action_id"])
        if action=="stop" and command["state"]=="queued":action="remove"
        return receipt(runs.manager.control({"session_id":run["session_id"],"command_id":run["command_id"],
            "expected_revision":command["revision"],"action":action,"_auth_actor_id":actor,"_auth_scopes":list(scopes)}))

    return [("GET","/agent/tasks/descriptor",descriptor),("POST","/agent/runs",create),("GET","/agent/runs",history),
            ("GET","/agent/runs/{run_id}",get),("GET","/agent/runs/{run_id}/events",events),
            ("POST","/agent/runs/{run_id}/control",control)]
