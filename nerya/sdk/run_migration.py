"""Import only exact legacy identities. Importing never creates Commands."""
from datetime import datetime
import hashlib
import json
import time

from ..agent.task_runs import task_runs,revision
from ..financial.store import encode


def import_legacy_runs(config):
    counts={'imported':0,'already_imported':0,'unlinked':0,'invalid':0}
    runs=task_runs(config)
    for journal,kind in (('scheduled_session','scheduled_agent'),('strategy_agent_tasks','strategy_agent')):
        path=config.paths.journal(journal)
        if not path.exists():continue
        with path.open(encoding='utf-8') as stream:
            for index,line in enumerate(stream):
                if not line.strip():continue
                try:
                    old=json.loads(line);sid=old.get('session_id');tid=old.get('turn_id')
                    task=old.get('schedule_id') if kind=='scheduled_agent' else old.get('strategy_id')
                    if old.get('run_id') or str(old.get('task_id','')).startswith('run_'):continue
                    if not task or not sid or not tid:counts['unlinked']+=1;continue
                    identity=[journal,task,sid,tid]
                    key=hashlib.sha256(encode(identity).encode()).hexdigest();rid='legacy_'+key[:40]
                    ts=old.get('ts_epoch')
                    if ts is None:ts=datetime.fromisoformat(str(old.get('ts')).replace('Z','+00:00')).timestamp()
                    with runs.store.transaction() as con:
                        if con.execute('SELECT 1 FROM agent_run_imports WHERE source_key=?',(key,)).fetchone():
                            counts['already_imported']+=1;continue
                        command=con.execute('SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? ORDER BY created_at LIMIT 1',(sid,tid)).fetchone()
                        exact=con.execute("SELECT meta_json FROM agent_messages WHERE session_id=? AND turn_id=? AND role='assistant' AND deleted=0 ORDER BY ts DESC LIMIT 1",(sid,tid)).fetchone()
                        meta=json.loads(exact['meta_json']) if exact else {}
                        result={'status':'historical_unverified','final_text':str(old.get('final_text') or ''),
                            'legacy_session_id':sid,'legacy_turn_id':tid,'evidence_verified':bool(command)}
                        if meta.get('turn'):result['turn']=meta['turn']
                        con.execute("""INSERT INTO agent_runs(run_id,task_kind,task_id,actor_id,session_id,source_revision,
                            trigger_kind,trigger_id,dedupe_key,fingerprint,admission_status,reason,snapshot_json,trigger_json,
                            result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,'legacy',?,?,?,'legacy',?,?,?,?,?,?)""",
                            (rid,kind,task,command['actor_id'] if command else 'local:loopback',sid,
                             str(old.get('package_hash') or 'historical_unknown'),str(old.get('trigger_event_id') or old.get('task_id') or key),key,revision(old),
                             '' if command else 'historical_completion_unverified',encode({'title':task,'legacy':True}),encode({'journal':journal,'line':index+1}),encode(result),float(ts),time.time()))
                        if command:con.execute('INSERT INTO agent_run_commands VALUES (?,?,?)',(rid,command['command_id'],command['created_at']))
                        con.execute('INSERT INTO agent_run_imports VALUES (?,?)',(key,rid))
                        counts['imported']+=1
                except (ValueError,TypeError,KeyError):counts['invalid']+=1
    return counts
