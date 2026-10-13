"""Parent-only recovery of a never-submitted strategy action; no old lease reopen."""
from copy import deepcopy
import json
import time
import uuid

from nerya.core import jsonl
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.financial.store import FinancialStore
from nerya.financial.strategy_runtime import bind_run, finish_run, validate_context
from nerya.financial.task_binding import strategy_security_revision
from nerya.sdk.financial_api import FinancialAPI
from nerya.strategies.context import StrategyState
from nerya.strategies.runner import _strategy_tick_lock
from nerya.workspace.state_store import StateStore


def reset_unprepared(config, sid, *, issuer):
    """Explicit reset of a failed open intent proven never prepared; retain lineage."""
    root=config.paths.strategy(sid)
    with _strategy_tick_lock(root/'state/tick.lock') as acquired:
        if not acquired:raise FinancialError('operator_recovery_tick_running',409)
        state=StrategyState(StateStore(root/'state/state.json'))
        cycle=state.get('cycle');pending=(cycle or {}).get('pending') or {}
        if sid!='monad_perp_sma' or cycle.get('phase')!='idle' or pending.get('action_id') or pending.get('label')!='open' or cycle.get('receipts'):
            raise FinancialError('operator_reset_unprepared_scope_denied',403)
        rid=pending.get('producer_run_id');store=FinancialStore(config)
        with store.transaction() as con:
            row=con.execute('SELECT state FROM strategy_financial_runs WHERE run_id=?',(rid,)).fetchone()
            if not row or row['state']!='finished':raise FinancialError('operator_reset_original_run_not_finished',409)
            if con.execute('SELECT 1 FROM financial_actions WHERE strategy_run_id=?',(rid,)).fetchone():
                raise FinancialError('operator_reset_action_exists',403)
            # An earlier unknown/submitted opening for this strategy also blocks reset.
            if con.execute("SELECT 1 FROM financial_actions WHERE actor_id=? AND kind='trade' AND state NOT IN ('rejected','failed_before_submission')",('strategy:'+sid,)).fetchone():
                raise FinancialError('operator_reset_strategy_exposure_may_exist',403)
        record=root/'runs'/(rid+'.json')
        import json
        failed=json.loads(record.read_text())
        if failed.get('status')!='error' or 'fixed_base must be a finite number' not in failed.get('reason',''):
            raise FinancialError('operator_reset_failure_evidence_mismatch',403)
        updated=deepcopy(cycle)
        event={'kind':'strategy.operator_reset_unprepared','strategy_id':sid,'producer_run_id':rid,
            'operator':issuer.actor_id,'reason':failed['reason'],'old_pending':pending,
            'old_last_signal_ms':cycle.get('last_signal_ms'),'at':time.time(),'proof':'zero funds actions for producer and strategy exposure lane'}
        updated.setdefault('operator_recoveries',[]).append(event)
        updated.pop('pending');updated.pop('last_signal_ms',None)
        if not state.compare_and_set('cycle',expect=cycle,new_value=updated):raise FinancialError('operator_reset_cycle_changed',409)
        jsonl.append(config.paths.journal('strategy_operator_recovery'),event)
        return {'ok':True,'phase':updated['phase'],'producer_run_id':rid,'pending_cleared':True,
            'note':'No order or signal injected; next native tick reads current public candles again'}


def recover(config, sid, old_action_id, *, issuer, execute=False):
    root = config.paths.strategy(sid)
    with _strategy_tick_lock(root/'state'/'tick.lock') as acquired:
        if not acquired:
            raise FinancialError('operator_recovery_tick_running',409)
        state = StrategyState(StateStore(root/'state'/'state.json'))
        cycle = state.get('cycle')
        pending = (cycle or {}).get('pending') or {}
        previous_recovery = pending.get('recovery')
        resumed = previous_recovery if previous_recovery and previous_recovery.get('stage') == 'intent_checkpoint' else None
        if pending.get('action_id') != old_action_id:
            raise FinancialError('operator_recovery_pending_identity_mismatch',409)
        store = FinancialStore(config)
        old = store.get_action(old_action_id, issuer, internal=True, operator=True)
        context = old['context']
        if (old['actor_id'] != 'strategy:'+sid or context.get('task_kind') != 'strategy_script'
                or context.get('task_id') != sid or old['strategy_run_id'] != pending.get('producer_run_id')
                or old['request'] != pending.get('request') or old['request'].get('chain') != 'monad-testnet'):
            raise FinancialError('operator_recovery_original_owner_request_mismatch',403)
        if old['submission'] or old['state'] not in {'prepared','awaiting_approval','awaiting_prerequisite','failed_before_submission'}:
            raise FinancialError('operator_recovery_submitted_or_uncertain_denied',403)
        revision = strategy_security_revision(config,sid)
        with store.transaction() as con:
            producer = con.execute('SELECT state,lease_until FROM strategy_financial_runs WHERE run_id=?',(old['strategy_run_id'],)).fetchone()
            if producer and producer['state']=='running' and producer['lease_until']>time.time():
                raise FinancialError('operator_recovery_original_producer_running',409)
            if producer and producer['state']=='awaiting_approval' and producer['lease_until']>time.time():
                raise FinancialError('operator_recovery_original_approval_still_active',409)
            grants=con.execute("SELECT grant_id FROM financial_grants WHERE actor_id=? AND task_kind='strategy_script' AND task_id=? AND state='active' AND security_revision=? AND valid_from<=? AND expires_at>?",
                ('strategy:'+sid,sid,revision,time.time(),time.time())).fetchall()
            if not grants:
                raise FinancialError('operator_recovery_current_grant_required',403)
            children=con.execute('SELECT state,submission_json FROM financial_actions WHERE parent_action_id=?',(old_action_id,)).fetchall()
            if any(row['state'] not in {'rejected','failed_before_submission'} or json.loads(row['submission_json'] or '{}') for row in children):
                raise FinancialError('operator_recovery_prerequisite_reconciliation_required',409)
            steps=con.execute('SELECT state,submission_json FROM financial_steps WHERE action_id=?',(old_action_id,)).fetchall()
            if any(row['state'] in {'submitting','submitted','confirming','unconfirmed','confirmed','needs_recovery'} or json.loads(row['submission_json'] or '{}') for row in steps):
                raise FinancialError('operator_recovery_submission_step_present',403)
        rid='recover_'+uuid.uuid4().hex
        event={'kind':'strategy.operator_recovery','strategy_id':sid,'old_action_id':old_action_id,
            'old_producer_run_id':old['strategy_run_id'],'old_state':old['state'],'old_error_code':old.get('error_code'),
            'new_producer_run_id':rid,'operator':issuer.actor_id,'at':time.time(),
            'original_receipt':{key:old.get('receipt',{}).get(key) for key in ('reason','state')},'security_revision':revision}
        if previous_recovery and not resumed:
            if previous_recovery.get('new_action_id') != old_action_id:
                raise FinancialError('operator_recovery_replacement_lineage_mismatch',403)
            event['previous_recovery'] = deepcopy(previous_recovery)
        existing_action = None
        if resumed:
            if (resumed.get('old_action_id') != old_action_id or resumed.get('strategy_id') != sid
                    or resumed.get('security_revision') != revision or not resumed.get('new_producer_run_id')):
                raise FinancialError('operator_recovery_resume_binding_mismatch',403)
            prior_rid=resumed['new_producer_run_id']
            with store.transaction() as con:
                prior=con.execute('SELECT * FROM strategy_financial_runs WHERE run_id=?',(prior_rid,)).fetchone()
                prior_actions=con.execute('SELECT * FROM financial_actions WHERE strategy_run_id=?',(prior_rid,)).fetchall()
                active=bool(prior and prior['state']=='running' and prior['lease_until']>time.time()
                    and prior['actor_id']=='strategy:'+sid and prior['security_revision']==revision)
                if prior_actions:
                    if len(prior_actions)!=1 or not active:
                        raise FinancialError('operator_recovery_existing_action_requires_review',409)
                    row=prior_actions[0]
                    if (row['action_key']!=prior_rid+':'+pending['label'] or json.loads(row['request_json'])!=old['request']
                            or row['state']!='prepared' or json.loads(row['submission_json'] or '{}')):
                        raise FinancialError('operator_recovery_existing_action_requires_review',409)
                    existing_action=store.public(row)
            if active:
                rid=prior_rid
                event={**resumed,'kind':'strategy.operator_recovery_resume','at':time.time()}
            else:
                event.update(kind='strategy.operator_recovery_expired_empty_producer',previous_empty_producer=prior_rid)
        replacement=deepcopy(cycle)
        replacement['pending']['recovery']={**event,'new_producer_run_id':rid,'stage':'intent_checkpoint'}
        replacement.setdefault('operator_recoveries',[]).append(event)
        if not state.compare_and_set('cycle',expect=cycle,new_value=replacement):
            raise FinancialError('operator_recovery_cycle_changed',409)
        jsonl.append(config.paths.journal('strategy_operator_recovery'),event)
        # Original frozen context retires the plan only. Never bind/validate/reopen old run.
        if old['state']!='failed_before_submission':
            original = FinancialContext(**{**context,'scopes':frozenset(context.get('scopes',[]))})
            FinancialGateway(config).discard(original,old_action_id,expected_revision=old['revision'])
        bound,new_context=bind_run(config,strategy_id=sid,run_id=rid,mode='live',
            session_id=context.get('session_id'),max_run_seconds=600)
        def context_provider(write):
            if write:validate_context(bound,new_context,preparing=True)
            return new_context
        api=FinancialAPI(bound,context_provider=context_provider)
        action=existing_action or api.prepare(deepcopy(old['request']),client_request_id=rid+':'+pending['label'])
        prepared=deepcopy(replacement)
        prepared['pending'].update(action_id=action['action_id'],producer_run_id=rid,
            recovery={**event,'stage':'prepared','new_action_id':action['action_id']})
        if not state.compare_and_set('cycle',expect=replacement,new_value=prepared):
            raise FinancialError('operator_recovery_cycle_changed_before_execution',409)
        jsonl.append(config.paths.journal('strategy_operator_recovery'),{**event,'stage':'prepared','new_action_id':action['action_id']})
        if not execute:
            return {'ok':True,'state':'prepared','new_action_id':action['action_id'],'producer_run_id':rid,
                'quote_hash':action['quote_hash'],'lease_seconds':600,'note':'No execution; original new lease and quote expiry apply'}
        result=api.execute(action['action_id'],quote_hash=action['quote_hash'])
        current=api.get(action['action_id'])
        updated=deepcopy(prepared)
        updated['pending']['recovery']['stage']=current['state']
        if current['state']=='confirmed':
            updated['receipts'][pending['label']]={'action_id':current['action_id'],'receipt':current.get('receipt') or {}}
            updated['phase']=pending['next_phase']
            updated['confirmed_at_ms']=int(time.time()*1000)
            updated.pop('pending')
        if not state.compare_and_set('cycle',expect=prepared,new_value=updated):
            raise FinancialError('operator_recovery_cycle_changed_after_execution',409)
        finish_run(bound,rid)
        jsonl.append(config.paths.journal('strategy_operator_recovery'),{**event,'stage':current['state'],'new_action_id':action['action_id']})
        return {'ok':True,'state':current['state'],'new_action_id':action['action_id'],'producer_run_id':rid,
            'old_action_id':old_action_id,'action':current,'execution':result,'phase':updated['phase']}
