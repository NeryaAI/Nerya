"""One funds mutation gateway. Transport uncertainty is never an automatic retry."""
from __future__ import annotations

import json
import time

from .contracts import FinancialError, FinancialContext, amount, digest, normalize_request, action_fingerprint
from .store import FinancialStore, encode


class FinancialGateway:
    def __init__(self,config,*,adapters=None):
        self.config=config;self.store=FinancialStore(config);self.adapters=adapters

    def adapter(self,request,*,quote=None,recovery=False):
        if self.adapters is not None:
            adapter=self.adapters.get(request["kind"])
            if adapter is None:raise FinancialError("unsupported_financial_capability",422)
            return adapter
        from .adapters import adapter_for
        return adapter_for(self.config,request,quote=quote,recovery=recovery)

    @staticmethod
    def _producer(context,action):
        if action.get("run_id")!=context.run_id:raise FinancialError("financial_run_binding_mismatch",403)
        if action.get("strategy_run_id")!=context.strategy_run_id:raise FinancialError("financial_strategy_run_binding_mismatch",403)

    def _strategy_resources(self,context,request):
        if context.task_kind not in {"strategy_agent","strategy_script"}:return
        from ..core import yaml_io
        from ..trading.accounts import get_account_profile
        manifest=yaml_io.load(self.config.paths.strategy(context.task_id)/"strategy.yml",default={}) or {}
        accounts=manifest.get("accounts") or [manifest.get("account_id")]
        markets=manifest.get("markets") or [manifest.get("market")]
        if not any(accounts):raise FinancialError("strategy_financial_resources_unavailable",403)
        if request.get("account_id") and request["account_id"] not in accounts:raise FinancialError("strategy_financial_account_mismatch",403)
        if request.get("wallet_id"):
            wallets={get_account_profile(self.config.paths,aid).wallet_id for aid in accounts if aid}
            if request["wallet_id"] not in wallets:raise FinancialError("strategy_financial_wallet_mismatch",403)
        if request.get("market") and request["market"] not in markets:raise FinancialError("strategy_financial_market_mismatch",403)
        if request["kind"]=="trade" and (request.get("plan") or {}).get("strategy_id")!=context.task_id:
            raise FinancialError("strategy_financial_owner_mismatch",403)

    def _quote(self,context,request):
        from dataclasses import asdict
        if self.adapters is None:
            import copy
            from ..core.config import Config
            data=copy.deepcopy(self.config.data)
            data.setdefault("runtime",{})["financial_context"]={**asdict(context),"scopes":sorted(context.scopes)}
            adapter=FinancialGateway(Config(paths=self.config.paths,data=data)).adapter(request)
        else:adapter=self.adapter(request)
        quote=adapter.quote(request)
        binding=getattr(adapter,"component_binding",None)
        if binding:quote["component_binding"]=binding
        component=getattr(adapter,"component",None)
        if component and set(component.spec.market_types)&{"option","lp","lending"}:
            if not quote.get("portfolio_effect"):raise FinancialError("financial_portfolio_effect_required",422)
            if "option" in component.spec.market_types:quote["options_risk_required"]=True
        return quote

    def account_state(self,context,raw):
        context.require("read:funds")
        request=normalize_request(raw)
        from .resource_binding import canonical_request
        request=canonical_request(self.config,request)
        self._strategy_resources(context,request)
        adapter=self.adapter(request)
        component=getattr(adapter,"component",None)
        if not component or not component.spec.account_state:raise FinancialError("component_account_state_unsupported",422)
        state=adapter.account_state(request)
        if not isinstance(state,dict) or state.get("health") not in {"ok","unknown","degraded","stale"} or not state.get("source") or state.get("as_of") is None:
            raise FinancialError("component_account_state_contract_incomplete",422)
        if state["health"]=="ok" and not 0<=time.time()-float(state["as_of"])<=float(self.config.get("financial.max_account_age_seconds",30)):
            state={**state,"health":"stale"}
        return {"component_binding":adapter.component_binding,**state}

    def _portfolio_risk(self,action):
        from ..trading.portfolio_risk import RiskMetrics,evaluate
        quote=action["quote"];mode=str(self.config.get("trading.risk_mode","normal"))
        if mode not in {"normal","halt_new_risk","reduce_only","freeze_all"}:raise FinancialError("invalid_trading_risk_mode",403)
        projection=quote.get("portfolio_effect")
        if not projection:
            if mode=="freeze_all":raise FinancialError("trading_risk_mode_blocks_action",403)
            if mode in {"halt_new_risk","reduce_only"} and action["kind"]!="trade":
                raise FinancialError("portfolio_effect_required_for_reduce_only",403)
            return
        if set(projection)!={"before","after","as_of","source"} or not projection["source"] or not 0<=time.time()-float(projection["as_of"])<=float(self.config.get("financial.max_account_age_seconds",30)):
            raise FinancialError("financial_portfolio_evidence_unavailable",422)
        try:
            if quote.get("options_risk_required") or quote.get("naked_options"):
                required={"equity_usd","gross_exposure_usd","initial_margin_usd","maintenance_margin_usd","stress_loss_usd","delta_usd","gamma_usd","vega_usd","theta_usd"}
                if any(required-set(projection[side]) for side in ("before","after")):
                    raise FinancialError("financial_option_risk_evidence_missing",422)
            before=RiskMetrics(**projection["before"]);after=RiskMetrics(**projection["after"])
            resource=action["request"].get("account_id") or "wallet:"+action["request"]["wallet_id"]
            limits=self.config.get("financial.portfolio_limits",{}).get(resource,{})
            reasons=evaluate(before,after,limits,mode=mode,requires_options_policy=bool(quote.get("options_risk_required") or quote.get("naked_options")))
        except (ValueError,TypeError) as exc:raise FinancialError("invalid_financial_portfolio_effect",422) from exc
        if reasons:raise FinancialError(reasons[0],403)

    @staticmethod
    def checked_quote(request,quote):
        for key in ('risk_usd','spend_usd','fee_usd'):quote[key]=format(amount(quote.get(key),zero=True),'f')
        if request.get('max_fee_usd') is not None and amount(quote['fee_usd'],zero=True)>amount(request['max_fee_usd'],zero=True):raise FinancialError('financial_fee_limit_exceeded')
        if not isinstance(quote.get('asset_amounts'),dict) or not isinstance(quote.get('available_asset_amounts'),dict):raise FinancialError('financial_quote_balance_missing',422)
        if float(amount(quote.get('expires_at')))<=time.time():raise FinancialError('financial_quote_expired')
        return quote

    def _fence(self,context,action):
        # Safety settings are mutable; accepted instructions and quotes are not.
        config=self.config
        if config.paths.config.exists():
            from ..core.config import load_config
            fresh=load_config(config.paths)
            if not fresh.get('financial.enabled',False) or fresh.kill_switch():raise FinancialError('financial_operations_disabled',403)
            if not fresh.live_trading_enabled():raise FinancialError('live_trading_disabled',403)
            self.config.data['financial']=fresh.data['financial']
            self.config.data['wallet']=fresh.data.get('wallet',{})
            self.config.data['trading']=fresh.data.get('trading',{})
        if not config.get('financial.enabled',False) or config.kill_switch():raise FinancialError('financial_operations_disabled',403)
        if not config.live_trading_enabled():raise FinancialError('live_trading_disabled',403)
        self._strategy_resources(context,action["request"])
        self._portfolio_risk(action)
        if context.command_id and not context.run_id:
            with self.store.transaction() as con:
                command=con.execute('SELECT state,actor_id FROM agent_commands WHERE command_id=?',(context.command_id,)).fetchone()
                if not command or command['actor_id']!=context.actor_id or command['state'] not in {'running','awaiting_approval'}:
                    raise FinancialError('financial_command_not_active',403)
        if self.adapters is None and action['quote'].get('binding_fingerprint'):
            from ..wallet.bindings import binding_fingerprint
            if binding_fingerprint(self.config,action['request'])!=action['quote']['binding_fingerprint']:
                raise FinancialError('financial_wallet_binding_changed',403)
        if context.task_kind == "strategy_script":
            from .strategy_runtime import validate_context
            validate_context(config, context)
        elif context.run_id:
            with self.store.transaction() as con:
                command=con.execute("""SELECT c.state,r.snapshot_json FROM agent_commands c JOIN agent_run_commands l
                    ON l.command_id=c.command_id JOIN agent_runs r ON r.run_id=l.run_id
                    WHERE c.command_id=? AND r.run_id=?""",(context.command_id,context.run_id)).fetchone()
                if not command or command['state'] not in {'running','awaiting_approval'}:raise FinancialError('financial_run_not_active',403)
                if command['state']=='running':
                    lease=con.execute('SELECT worker_owner,lease_until FROM agent_command_queues WHERE session_id=(SELECT session_id FROM agent_commands WHERE command_id=?)',(context.command_id,)).fetchone()
                    if not lease or not lease['worker_owner'] or lease['lease_until']<=time.time():raise FinancialError('financial_run_lease_lost',403)
                snapshot=json.loads(command['snapshot_json'])
                if snapshot.get('mode')=='paper':raise FinancialError('paper_run_denies_funds_execution',403)
                if snapshot.get('expires_at',float('inf'))<=time.time():raise FinancialError('financial_trigger_expired',403)
            from .task_binding import task_security_revision
            if task_security_revision(config,context.task_kind,context.task_id)!=context.security_revision:
                raise FinancialError('financial_task_security_revision_changed')

    def prepare(self,context,raw,*,action_key,parent_action_id=None):
        if not isinstance(action_key,str) or not action_key or len(action_key)>200:
            raise FinancialError("financial_action_key_required",400)
        request=normalize_request(raw)
        from .resource_binding import canonical_request
        request=canonical_request(self.config,request)
        self._strategy_resources(context,request)
        if parent_action_id:
            parent=self.store.get_action(parent_action_id,context,internal=True)
            self._producer(context,parent)
            if parent['state'] not in {'prepared','awaiting_prerequisite','needs_recovery'}:
                raise FinancialError('financial_prerequisite_parent_mismatch',403)
            if request not in self._prerequisite_requests(parent):raise FinancialError('financial_prerequisite_plan_mismatch',403)
            with self.store.transaction() as con:
                child=con.execute("SELECT * FROM financial_actions WHERE parent_action_id=? AND kind IN ('contract_approval','permit2_approval') AND request_json=? AND state NOT IN ('rejected','failed_before_submission') ORDER BY created_at DESC LIMIT 1",(parent_action_id,encode(request))).fetchone()
            if child:return {**self.store.public(child),'duplicate':True}
        existing=self.store.existing_action(context,action_key,action_fingerprint(context,request,parent_action_id))
        if existing:return existing
        quote=self._quote(context,request)
        if self.adapters is None and request.get('wallet_id'):
            from ..wallet.bindings import binding_fingerprint
            quote['binding_fingerprint']=binding_fingerprint(self.config,request)
        for key in ("risk_usd","spend_usd","fee_usd"):
            quote[key]=format(amount(quote.get(key),zero=True),"f")
        if request.get('max_fee_usd') is not None and amount(quote['fee_usd'],zero=True)>amount(request['max_fee_usd'],zero=True):
            raise FinancialError('financial_fee_limit_exceeded')
        if not isinstance(quote.get("asset_amounts"),dict) or not isinstance(quote.get("available_asset_amounts"),dict):
            raise FinancialError("financial_quote_balance_missing",422)
        if not float(amount(quote.get("expires_at")))>time.time():raise FinancialError("financial_quote_expired")
        result=self.store.prepare_action(context,request,quote,action_key=action_key,parent_action_id=parent_action_id)
        if parent_action_id:
            with self.store.transaction() as con:
                con.execute("UPDATE financial_steps SET submission_json=?,state='prepared',updated_at=? WHERE action_id=? AND step_id='0'",
                    (encode({'child_action_id':result['action_id']}),time.time(),parent_action_id))
        return result

    @staticmethod
    def _allowance_request(parent):
        request=parent['request'];step=parent['quote']['steps'][0]
        return normalize_request({'kind':'contract_approval','wallet_id':request['wallet_id'],'chain':request['chain'],
            'asset':request['asset'],'amount':request['amount'],'spender':step['spender']})

    def _prerequisite_requests(self,parent):
        prerequisites=parent["quote"].get("prerequisites")
        if prerequisites is None:
            return [self._allowance_request(parent)] if parent["kind"]=="bridge_swap" else []
        if not isinstance(prerequisites,list) or len(prerequisites)>8:raise FinancialError("invalid_financial_prerequisites",422)
        result=[]
        for raw in prerequisites:
            from .resource_binding import canonical_request
            request=canonical_request(self.config,normalize_request(raw))
            from .contracts import ALLOWANCE_ACTIONS
            if request["kind"] not in ALLOWANCE_ACTIONS or request.get("wallet_id")!=parent["request"].get("wallet_id") or request.get("chain")!=parent["request"].get("chain"):
                raise FinancialError("financial_prerequisite_resource_mismatch",403)
            result.append(request)
        return result

    def refresh(self,context,aid,*,expected_revision):
        action=self.store.get_action(aid,context,internal=True)
        self._producer(context,action)
        if action['revision']!=expected_revision:raise FinancialError('revision_conflict')
        if action['state'] not in {'prepared','awaiting_approval','awaiting_prerequisite','needs_recovery'} or action['submission']:
            raise FinancialError('submitted_financial_plan_is_immutable')
        if action['kind']=='bridge_swap' or action['quote'].get('prerequisites'):
            with self.store.transaction() as con:
                pending=con.execute("SELECT 1 FROM financial_actions WHERE parent_action_id=? AND state NOT IN ('confirmed','rejected','failed_before_submission') LIMIT 1",(aid,)).fetchone()
            if pending:raise FinancialError('financial_prerequisite_not_confirmed')
        # New quote is a new reviewable version. Previous approvals never carry
        # over to changed calldata, minimum output, gas or routes.
        quote=self.checked_quote(action['request'],self._quote(context,action['request']))
        adapter=self.adapter(action['request'],quote=quote)
        if self.adapters is None and action['request'].get('wallet_id'):
            from ..wallet.bindings import binding_fingerprint
            quote['binding_fingerprint']=binding_fingerprint(self.config,action['request'])
        adapter.validate(action['request'],quote)
        with self.store.transaction() as con:
            current=con.execute('SELECT * FROM financial_actions WHERE action_id=?',(aid,)).fetchone()
            if current['revision']!=expected_revision:raise FinancialError('revision_conflict')
            if current['approval_id']:con.execute("UPDATE approvals SET state='cancelled' WHERE id=? AND state='pending'",(current['approval_id'],))
            con.execute("INSERT INTO financial_steps(action_id,step_id,state,plan_json,updated_at) VALUES (?,?,'superseded_quote',?,?)",
                (aid,'quote:'+str(expected_revision),current['quote_json'],time.time()))
            from dataclasses import asdict
            con.execute("UPDATE financial_actions SET quote_json=?,quote_hash=?,context_json=?,state='prepared',approval_id=NULL,revision=revision+1,updated_at=? WHERE action_id=?",
                (encode(quote),digest(quote),encode({**asdict(context),'scopes':sorted(context.scopes)}),time.time(),aid))
        return self.store.get_action(aid,context)

    def discard(self,context,aid,*,expected_revision):
        action=self.store.get_action(aid,context,internal=True)
        self._producer(context,action)
        if action['revision']!=expected_revision:raise FinancialError('revision_conflict')
        if action['submission'] or action['state'] not in {'prepared','awaiting_approval','awaiting_prerequisite','needs_recovery'}:
            raise FinancialError('submitted_financial_action_cannot_be_discarded')
        with self.store.transaction() as con:
            pending=con.execute("SELECT 1 FROM financial_actions WHERE parent_action_id=? AND state IN ('submitting','submitted','confirming','unconfirmed','needs_recovery') LIMIT 1",(aid,)).fetchone()
            if pending:raise FinancialError('financial_prerequisite_receipt_pending')
            if action['approval_id']:con.execute("UPDATE approvals SET state='cancelled' WHERE id=? AND state='pending'",(action['approval_id'],))
        self.store.mark(aid,state='failed_before_submission',receipt={'reason':'operator_discarded_unsubmitted_plan'},expected_state=action['state'])
        self._publish(aid)
        return self.store.get_action(aid,context)

    def _approval(self,action,reason="explicit_financial_authorization_required"):
        from ..core import jsonl
        from ..db.repositories import ApprovalRepository
        aid="approval_"+action["action_id"]+'_'+action['quote_hash'][:16]
        kind="financial_action_trade" if action["kind"] in {"trade","swap"} else "financial_action"
        record={"approval_id":aid,"kind":kind,"state":"pending","actor_id":action["actor_id"],
            "financial_action_id":action["action_id"],"quote_hash":action["quote_hash"],
            "expires_at":min(time.time()+600,float(action["quote"]["expires_at"])),"reason":reason,
            "summary":{"request":action["request"],"risk_usd":action["quote"]["risk_usd"],"fee_usd":action["quote"]["fee_usd"]}}
        context=action["context"]
        record.update(session_id=context.get('session_id'),turn_id=context.get('turn_id'),run_id=action.get("run_id"))
        if action.get("run_id") and context.get("task_kind") != "strategy_script":
            from ..agent.task_runs import task_runs
            run=task_runs(self.config).get(action["run_id"])
            record.update(session_id=run["session_id"],turn_id=run.get("turn_id"))
        with self.store.transaction() as con:
            existing=con.execute("SELECT state FROM approvals WHERE id=?",(aid,)).fetchone()
            if not existing:
                ApprovalRepository(con).insert(id=aid,kind=kind,expires_s=max(1,record["expires_at"]-time.time()),payload=record)
            con.execute("UPDATE financial_actions SET state='awaiting_approval',approval_id=?,revision=revision+1,updated_at=? WHERE action_id=? AND state IN ('prepared','awaiting_approval')",(aid,time.time(),action["action_id"]))
        # Canonical record is in SQLite; legacy approval UI reads this projection.
        if not existing:jsonl.append(self.config.paths.approvals_pending,record)
        return {"status":"approval_required","approval_id":aid,"action_id":action["action_id"],"run_id":action.get("run_id"),"reason":reason}

    def execute(self,context,aid,*,quote_hash):
        if context.plan_only:raise FinancialError("plan_mode_denies_financial_execution",403)
        action=self.store.get_action(aid,context,internal=True)
        self._producer(context,action)
        if action["quote_hash"]!=quote_hash:raise FinancialError("financial_quote_changed")
        if action["state"] in {"submitting","submitted","confirming","confirmed","unconfirmed","needs_recovery"}:
            return {**self.store.get_action(aid,context),"duplicate":True}
        prior=action['context']
        if prior.get('command_id') and not context.command_id:raise FinancialError('financial_action_requires_original_command',403)
        if prior.get('session_id') and context.session_id!=prior['session_id']:raise FinancialError('financial_session_binding_mismatch',403)
        from dataclasses import asdict
        current_context={**asdict(context),'scopes':sorted(context.scopes)}
        with self.store.transaction() as con:
            con.execute('UPDATE financial_actions SET context_json=? WHERE action_id=? AND state IN (?,?,?)',
                (encode(current_context),aid,'prepared','awaiting_approval','awaiting_prerequisite'))
        action['context']=current_context
        self._fence(context,action)
        if context.run_id:
            from .task_binding import task_security_revision
            if task_security_revision(self.config,context.task_kind,context.task_id)!=context.security_revision:
                raise FinancialError("financial_task_security_revision_changed")
        try:self.adapter(action["request"],quote=action["quote"]).validate(action["request"],action["quote"])
        except FinancialError as exc:
            if (action['kind']=='bridge_swap' and exc.code=='finite_bridge_allowance_required') or exc.code=='finite_protocol_allowance_required':
                self.store.mark(aid,state='awaiting_prerequisite',receipt={'reason':exc.code})
                required=self._prerequisite_requests(action)
                return {'status':'prerequisite_required','action_id':aid,'parent_action_id':aid,'required_actions':required,
                    **({'required_action':required[0]} if len(required)==1 else {}),'next_step':'confirm_finite_allowance_then_refresh_quote'}
            raise
        reservation=self.store.reserve(aid,context,quote_hash=quote_hash,approved=bool(action.get("approval_id")))
        if reservation.get("needs_approval"):
            return self._approval(action,reservation.get("reason","explicit_financial_authorization_required"))
        if reservation["duplicate"]:return {**reservation["action"],"duplicate":True}
        try:self._fence(context,action)
        except FinancialError:
            self.store.mark(aid,state='failed_before_submission');raise
        # The final permission fence and submission marker share a transaction.
        fence_error=None
        with self.store.transaction() as con:
            row=con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
            if row["state"]!="reserved":return {**self.store.public(row),"duplicate":True}
            if row["grant_id"]:
                grant=con.execute("SELECT * FROM financial_grants WHERE grant_id=?",(row["grant_id"],)).fetchone()
                if not grant or grant["state"]!="active" or grant["expires_at"]<=time.time():
                    fence_error='financial_grant_revoked_or_expired'
            else:
                approval=con.execute('SELECT state,expires_at FROM approvals WHERE id=?',(row['approval_id'],)).fetchone()
                if not approval or approval['state']!='approved' or approval['expires_at']<=time.time():fence_error='financial_approval_no_longer_valid'
            if not fence_error:con.execute("UPDATE financial_actions SET state='submitting',revision=revision+1,updated_at=? WHERE action_id=?",(time.time(),aid))
        if fence_error:
            self.store.mark(aid,state='failed_before_submission');raise FinancialError(fence_error)
        def submitted(ref):
            # Owned signers supply identity before network broadcast.
            self.store.mark(aid,state="submitted",submission=ref)
        try:
            import copy
            from ..core.config import Config
            execution_data=copy.deepcopy(self.config.data)
            execution_data.setdefault("runtime",{})["financial_action_id"]=aid
            execution_config=Config(paths=self.config.paths,data=execution_data)
            executor=self.adapter(action["request"],quote=action["quote"]) if self.adapters is not None else FinancialGateway(execution_config).adapter(action["request"],quote=action["quote"])
            result=executor.execute(action["request"],action["quote"],submitted)
            state=result.get("state","unconfirmed")
            if state not in {"confirmed","submitted","confirming","unconfirmed","needs_recovery","failed_before_submission",'rejected'}:
                state="unconfirmed"
            self.store.mark(aid,state=state,receipt=result,submission=result.get("submission"))
            self.store.mark_steps(aid,result)
        except Exception as exc:
            # Even a transport exception can follow a successful submission.
            self.store.mark(aid,state="unconfirmed",error_code=getattr(exc,"code",None) or type(exc).__name__)
        self._publish(aid)
        return self.store.get_action(aid,context)

    def reconcile(self,context,aid):
        action=self.store.get_action(aid,context,internal=True,operator='api:all' in context.scopes or 'admin:ops' in context.scopes)
        if action["state"] not in {"submitted","confirming","unconfirmed","needs_recovery"}:return action
        try:
            result=self.adapter(action["request"],quote=action["quote"],recovery=True).status(action["request"],action["quote"],action["submission"])
        except FinancialError as exc:
            if exc.code.startswith("financial_component_") or exc.code.startswith("trading_component_") or exc.code.startswith("trading_plugin_"):
                self.store.mark(aid,state="needs_recovery",error_code=exc.code)
                return self.store.get_action(aid,context,operator='api:all' in context.scopes or 'admin:ops' in context.scopes)
            raise
        state=result.get("state","unconfirmed")
        if state not in {"confirmed","submitted","confirming","unconfirmed","needs_recovery",'rejected'}:state="unconfirmed"
        self.store.mark(aid,state=state,receipt=result)
        self.store.mark_steps(aid,result)
        self._publish(aid)
        return self.store.get_action(aid,context,operator='api:all' in context.scopes or 'admin:ops' in context.scopes)

    def _publish(self,aid):
        with self.store.transaction() as con:
            row=con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
            if not row["run_id"]:return
            linked=con.execute("""SELECT c.* FROM agent_commands c JOIN agent_run_commands l ON c.command_id=l.command_id
                WHERE l.run_id=? ORDER BY l.created_at DESC LIMIT 1""",(row["run_id"],)).fetchone()
            if not linked:return
            event={"kind":"financial.receipt","event_id":f"financial:{aid}:{row['revision']}",
                "session_id":linked["session_id"],"turn_id":linked["turn_id"],"run_id":row["run_id"],
                "action_id":aid,"state":row["state"],"ts":time.time()}
            con.execute("INSERT OR IGNORE INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)",
                        (linked["command_id"],event["event_id"],encode(event)))
            con.execute("UPDATE agent_runs SET revision=revision+1,updated_at=? WHERE run_id=?",(time.time(),row["run_id"]))
