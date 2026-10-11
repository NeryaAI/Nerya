"""Durable result delivery and read-only funds reconciliation, without a model."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from types import SimpleNamespace

from ..agent.task_runs import task_runs
from ..financial.store import FinancialStore,encode

log=logging.getLogger(__name__)


def enqueue_delivery(config,run_id,targets):
    store=FinancialStore(config)
    with store.transaction() as con:
        for index,target in enumerate(targets):
            identity=hashlib.sha256(encode([run_id,index,target]).encode()).hexdigest()
            con.execute("INSERT OR IGNORE INTO run_deliveries(delivery_id,run_id,target_json,updated_at) VALUES (?,?,?,?)",
                (identity,run_id,encode(target),time.time()))
    task_runs(config).update(run_id,delivery={"status":"pending"})


def delivery_tick(config,*,deliver=None,now=None):
    now=time.time() if now is None else now
    store=FinancialStore(config)
    with store.transaction() as con:
        con.execute("UPDATE run_deliveries SET state='unconfirmed',updated_at=? WHERE state='sending' AND lease_until<?",(now,now))
        row=con.execute("""SELECT * FROM run_deliveries WHERE state IN ('pending','failed')
            AND attempts<3 AND next_attempt<=? ORDER BY updated_at LIMIT 1""",(now,)).fetchone()
        if not row:return False
        con.execute("UPDATE run_deliveries SET state='sending',attempts=attempts+1,lease_until=?,updated_at=? WHERE delivery_id=?",
            (now+120,now,row['delivery_id']))
    run=task_runs(config).get(row['run_id'])
    target=json.loads(row['target_json'])
    target={**target,'delivery_id':row['delivery_id']}
    entry=SimpleNamespace(**{**run['snapshot']['definition'],'delivery_targets':[target]})
    result={**run['result'].get('turn',{}),'run_id':run['run_id'],'delivery_id':row['delivery_id']}
    if deliver is None:
        from ..messaging.scheduled_delivery import deliver_scheduled_session
        deliver=deliver_scheduled_session
    try:
        outcomes=deliver(config,entry,result)
        receipt=outcomes[0] if outcomes else {'ok':False,'error':'empty_delivery_receipt','uncertain':True}
    except Exception as exc:receipt={'ok':False,'error':type(exc).__name__,'uncertain':True}
    state='delivered' if receipt.get('ok') else 'unconfirmed' if receipt.get('uncertain') else 'failed'
    with store.transaction() as con:
        con.execute("UPDATE run_deliveries SET state=?,receipt_json=?,next_attempt=?,updated_at=? WHERE delivery_id=? AND state='sending'",
            (state,encode(receipt),now+min(3600,30*2**row['attempts']),time.time(),row['delivery_id']))
        rows=con.execute("SELECT delivery_id,state,attempts,receipt_json FROM run_deliveries WHERE run_id=?",(run['run_id'],)).fetchall()
    states={item['state'] for item in rows}
    status='delivered' if states=={'delivered'} else 'unconfirmed' if 'unconfirmed' in states else 'pending' if states & {'pending','sending'} else 'failed'
    task_runs(config).update(run['run_id'],delivery={'status':status,'targets':[
        {'delivery_id':r['delivery_id'],'status':r['state'],'attempts':r['attempts'],'receipt':json.loads(r['receipt_json'])} for r in rows]})
    return True


def financial_tick(config,*,gateway=None):
    from ..financial.contracts import FinancialContext
    from ..financial.gateway import FinancialGateway
    store=FinancialStore(config)
    # Crashes inside a submission window are ambiguous, never re-executed.
    with store.transaction() as con:
        con.execute("UPDATE financial_actions SET state='unconfirmed',revision=revision+1 WHERE state='submitting' AND updated_at<?",(time.time()-120,))
        rows=con.execute("""SELECT action_id,context_json FROM financial_actions
            WHERE state IN ('submitted','confirming','unconfirmed','needs_recovery') ORDER BY updated_at LIMIT 20""").fetchall()
        expired=con.execute("SELECT a.id FROM approvals a JOIN financial_actions f ON f.approval_id=a.id WHERE a.state='pending' AND a.expires_at<=?",(time.time(),)).fetchall()
    from ..approval_service import ApprovalService
    service=ApprovalService(config)
    for pending in expired:
        record=service.move(pending['id'],state='expired')
        if record:service.publish_resolution(pending['id'],state='expired',record=record)
    gateway=gateway or FinancialGateway(config)
    for row in rows:
        values=json.loads(row['context_json']);values['scopes']=frozenset(values.get('scopes',[]))
        try:gateway.reconcile(FinancialContext(**values),row['action_id'])
        except Exception:log.warning('funds receipt lookup deferred: %s',row['action_id'])
    ready=[]
    with store.transaction() as con:
        queues=con.execute("SELECT session_id FROM agent_command_queues WHERE paused=1 AND pause_reason='funds_pending'").fetchall()
        for queue in queues:
            pending=con.execute("""SELECT 1 FROM financial_actions f JOIN agent_runs r ON f.run_id=r.run_id
                WHERE r.session_id=? AND f.state IN ('awaiting_approval','awaiting_prerequisite','submitting','submitted','confirming','unconfirmed','needs_recovery') LIMIT 1""",(queue['session_id'],)).fetchone()
            if not pending:
                con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=? AND pause_reason='funds_pending'",(queue['session_id'],))
                ready.append(queue['session_id'])
    if ready:
        from ..agent.command_runtime import runtime
        for sid in ready:runtime(config).kick(sid)
    return len(rows)


def start(config):
    stop=threading.Event()
    def work():
        from ..agent.command_runtime import runtime
        from .run_migration import import_legacy_runs
        try:import_legacy_runs(config)
        except Exception:log.exception('legacy run import failed; no historical work replayed')
        while not stop.is_set():
            try:
                runtime(config).recover_tasks()
                financial_tick(config)
                delivery_tick(config)
            except Exception:log.exception('run effects maintenance failed')
            stop.wait(15)
    thread=threading.Thread(target=work,name='nerya-run-effects',daemon=True);thread.start()
    return stop
