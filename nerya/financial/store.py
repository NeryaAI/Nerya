"""Atomic authorization usage; submitted or uncertain funds stay reserved."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal,localcontext
import json
import time
import uuid

from .contracts import FinancialError, amount, digest, normalize_policy, action_fingerprint, ALLOWANCE_ACTIONS
from ..core.redaction import redact_display_dict,redact_public_record
from ..db.sqlite import connect


def encode(value):return json.dumps(value,sort_keys=True,separators=(",",":"),default=str)


class FinancialStore:
    PUBLIC_IDENTITIES=frozenset({"security_revision","quote_hash","transaction_hash","hash","recipient","receiver","address","spender","router", "recipients","spenders","routers","assets","asset","to_asset","from_token","to_token",'order_id','order_ids','client_order_id','executor_id','intent_id','plan_id','execution_ref','destination_transaction_hash','route_hash','binding_fingerprint','component_id','component_binding','package_revision','revision','plugin_id','to','from','sender','pool','oracle','a_token','debt_token','block_hash','deployment_revision','position_key'})
    def __init__(self,config):self.config=config

    @contextmanager
    def transaction(self):
        con=connect(self.config.paths.db)
        try:
            with localcontext() as decimal_context:
                decimal_context.prec=160
                con.execute("BEGIN IMMEDIATE");yield con;con.commit()
        except BaseException:
            con.rollback();raise
        finally:con.close()

    @staticmethod
    def public(row, *, internal=False):
        data=dict(row)
        for key in list(data):
            if key.endswith("_json"):data[key[:-5]]=json.loads(data.pop(key))
        data.pop("fingerprint",None)
        return data if internal else redact_public_record(data,identity_fields=FinancialStore.PUBLIC_IDENTITIES)

    def create_grant(self,context,*,task_kind,task_id,security_revision,policy,expires_at=None,valid_from=None,subject_actor_id=None):
        context.require("write:config")
        if task_kind not in {"strategy_agent","strategy_script","scheduled_agent"} or not task_id or not security_revision:
            raise FinancialError("grant_task_binding_required",400)
        policy=normalize_policy(policy)
        from .resource_binding import resource_revisions,canonical_policy
        policy=canonical_policy(self.config,policy)
        policy['resource_revisions']=resource_revisions(self.config,policy)
        now=time.time();start=float(amount(valid_from if valid_from is not None else now,zero=True))
        end=float(amount(expires_at if expires_at is not None else now+86400))
        if end<=max(now,start):raise FinancialError("grant_expiry_required",400)
        if end>now+float(self.config.get('financial.max_grant_seconds',31536000)):
            raise FinancialError('grant_duration_exceeds_limit',400)
        gid="grant_"+uuid.uuid4().hex
        subject=subject_actor_id or context.actor_id
        family=digest({"actor":subject,"kind":task_kind,"task":task_id})
        with self.transaction() as con:
            con.execute("""INSERT INTO financial_grants(grant_id,family_id,actor_id,task_kind,task_id,security_revision,
                state,policy_json,valid_from,expires_at,created_at,updated_at,issued_by) VALUES (?,?,?,?,?,?,'draft',?,?,?,?,?,?)""",
                (gid,family,subject,task_kind,task_id,security_revision,encode(policy),start,end,now,now,context.actor_id))
        return self.get_grant(gid,context,operator=True)

    def get_grant(self,gid,context,*,operator=False):
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_grants WHERE grant_id=?",(gid,)).fetchone()
            if not row:raise FinancialError("grant_not_found",404)
            if row["actor_id"]!=context.actor_id and row["issued_by"]!=context.actor_id and not operator:raise FinancialError("grant_owner_mismatch",403)
            return self.public(row)

    def approve_grant(self,gid,context,*,expected_revision):
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_grants WHERE grant_id=?",(gid,)).fetchone()
            if not row:raise FinancialError("grant_not_found",404)
            if row["actor_id"]!=context.actor_id and row["issued_by"]!=context.actor_id and not ({"api:all","admin:ops"}&context.scopes):
                raise FinancialError("grant_owner_mismatch",403)
            policy=json.loads(row["policy_json"])
            from .resource_binding import resource_revisions
            if policy.get('resource_revisions')!=resource_revisions(self.config,policy):raise FinancialError('grant_resource_binding_changed')
            scope="approve:trade" if set(policy["actions"])<={"trade","swap"} else "approve:funds"
            context.require(scope)
            if set(policy['actions'])&{'trade','swap'}:context.require('approve:trade')
            if row["revision"]!=expected_revision:raise FinancialError("revision_conflict")
            if row["state"]!="draft":raise FinancialError("grant_not_draft")
            if row["expires_at"]<=time.time():raise FinancialError("grant_expired")
            con.execute("UPDATE financial_grants SET state='active',approved_by=?,revision=revision+1,updated_at=? WHERE grant_id=?",
                        (context.actor_id,time.time(),gid))
            row=con.execute("SELECT * FROM financial_grants WHERE grant_id=?",(gid,)).fetchone()
            return self.public(row)

    def revoke_grant(self,gid,context,*,expected_revision):
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_grants WHERE grant_id=?",(gid,)).fetchone()
            if not row:raise FinancialError("grant_not_found",404)
            if row["actor_id"]!=context.actor_id and row["issued_by"]!=context.actor_id and not ({"api:all","admin:ops"}&context.scopes):
                raise FinancialError("grant_owner_mismatch",403)
            policy=json.loads(row["policy_json"])
            context.require("approve:trade" if set(policy["actions"])<={"trade","swap"} else "approve:funds")
            if row["revision"]!=expected_revision:raise FinancialError("revision_conflict")
            con.execute("UPDATE financial_grants SET state='revoked',revision=revision+1,updated_at=? WHERE grant_id=?",(time.time(),gid))
        return self.get_grant(gid,context,operator=True)

    def list_grants(self,context,*,operator=False,task_id=None,task_kind=None):
        where,args=[],[]
        if not operator:where.append("(actor_id=? OR issued_by=?)");args.extend((context.actor_id,context.actor_id))
        if task_id:where.append("task_id=?");args.append(task_id)
        if task_kind:where.append('task_kind=?');args.append(task_kind)
        with self.transaction() as con:
            rows=con.execute("SELECT * FROM financial_grants"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY created_at DESC LIMIT 100",args).fetchall()
            result=[]
            for row in rows:
                item=self.public(row)
                usage=con.execute("SELECT * FROM financial_usage WHERE family_id=? AND state!='released'",(row['family_id'],)).fetchall()
                item['usage']={'total_usd':str(sum((amount(r['amount_usd'],zero=True) for r in usage if r['grant_id']==row['grant_id'] and r['kind'] not in ALLOWANCE_ACTIONS),Decimal(0))),
                    'rolling_24h_usd':str(sum((amount(r['amount_usd'],zero=True) for r in usage if r['created_at']>time.time()-86400 and r['kind'] not in ALLOWANCE_ACTIONS),Decimal(0))),
                    'allowance_risk_usd':str(sum((amount(r['amount_usd'],zero=True) for r in usage if r['grant_id']==row['grant_id'] and r['kind'] in ALLOWANCE_ACTIONS),Decimal(0))),
                    'reserved_usd':str(sum((amount(r['amount_usd'],zero=True) for r in usage if r['state']=='reserved' and r['grant_id']==row['grant_id'] and r['kind'] not in ALLOWANCE_ACTIONS),Decimal(0)))}
                result.append(item)
            return result

    def existing_action(self,context,key,fingerprint):
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_actions WHERE actor_id=? AND action_key=?",(context.actor_id,key)).fetchone()
            if row and row["fingerprint"]!=fingerprint:raise FinancialError("financial_idempotency_conflict")
            return self.public(row) if row else None

    def prepare_action(self,context,request,quote,*,action_key,parent_action_id=None):
        fp=action_fingerprint(context,request,parent_action_id)
        aid="fund_"+uuid.uuid4().hex;now=time.time()
        with self.transaction() as con:
            if parent_action_id:
                child=con.execute("""SELECT * FROM financial_actions WHERE parent_action_id=? AND actor_id=? AND request_json=?
                    AND kind IN ('contract_approval','permit2_approval') AND state NOT IN ('rejected','failed_before_submission') ORDER BY created_at DESC LIMIT 1""",
                    (parent_action_id,context.actor_id,encode(request))).fetchone()
                if child:return {**self.public(child),'duplicate':True}
            old=con.execute("SELECT * FROM financial_actions WHERE actor_id=? AND action_key=?",(context.actor_id,action_key)).fetchone()
            if old:
                if old["fingerprint"]!=fp:raise FinancialError("financial_idempotency_conflict")
                return self.public(old)
            con.execute("""INSERT INTO financial_actions(action_id,actor_id,run_id,parent_action_id,action_key,kind,
                request_json,fingerprint,context_json,quote_json,quote_hash,state,created_at,updated_at,strategy_run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?,?)""",
                (aid,context.actor_id,context.run_id,parent_action_id,action_key,request["kind"],encode(request),fp,
                 encode({**asdict(context),"scopes":sorted(context.scopes)}),encode(quote),digest(quote),now,now,context.strategy_run_id))
            steps=quote.get('steps') or []
            if request['kind']=='bridge_swap':steps=[*steps,{'kind':'source_submission','chain':request['chain']},
                {'kind':'bridge','to_chain':request['to_chain']},{'kind':'destination_credit','asset':request['to_asset'],'recipient':request['recipient'],'minimum_received_base':quote.get('minimum_received_base')}]
            for index,step in enumerate(steps):
                con.execute('INSERT INTO financial_steps(action_id,step_id,state,plan_json,updated_at) VALUES (?,?,?,?,?)',
                    (aid,str(index),'pending',encode(step),now))
            return self.public(con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone())

    def get_action(self,aid,context,*,operator=False,internal=False):
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
            if not row:raise FinancialError("financial_action_not_found",404)
            if row["actor_id"]!=context.actor_id and not operator:raise FinancialError("financial_owner_mismatch",403)
            item=self.public(row,internal=internal)
            item['steps']=[self.public(step,internal=internal) for step in con.execute('SELECT * FROM financial_steps WHERE action_id=? ORDER BY CAST(step_id AS INTEGER)',(aid,))]
            return item

    def mark_steps(self,aid,receipt):
        with self.transaction() as con:
            for row in con.execute('SELECT * FROM financial_steps WHERE action_id=?',(aid,)).fetchall():
                plan=json.loads(row['plan_json']);kind=plan.get('kind')
                state=None
                if kind=='source_submission':state='confirmed' if receipt.get('source_confirmed') else 'submitted' if receipt.get('submission') or receipt.get('transaction_hash') else None
                if kind=='bridge':state='confirmed' if receipt.get('destination_confirmed') else 'running' if receipt.get('source_confirmed') else None
                if kind=='destination_credit':state='confirmed' if receipt.get('destination_confirmed') else 'needs_recovery' if receipt.get('state')=='needs_recovery' else None
                if state and row['state']!='confirmed':con.execute('UPDATE financial_steps SET state=?,receipt_json=?,updated_at=? WHERE action_id=? AND step_id=?',
                    (state,encode(receipt),time.time(),aid,row['step_id']))

    def list_actions(self,context,*,operator=False,run_id=None,limit=50):
        where,args=[],[]
        if not operator:where.append("actor_id=?");args.append(context.actor_id)
        if run_id:where.append("run_id=?");args.append(run_id)
        with self.transaction() as con:
            rows=con.execute("SELECT * FROM financial_actions"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY created_at DESC LIMIT ?",(*args,max(1,min(int(limit),100)))).fetchall()
            return [self.public(r) for r in rows]

    @staticmethod
    def matches(policy,request,quote):
        if request["kind"] not in policy["actions"]:return False
        resources=policy["resources"]
        for field,key in (("account_id","accounts"),("wallet_id","wallets"),("asset","assets"),("market","markets"),
                          ("chain","chains"),("to_chain","chains"),("to_asset","assets"),("recipient","recipients"),("spender","spenders"),
                          ("component_id","components"),("protocol","protocols"),("position_id","positions")):
            if request.get(field) is not None and str(request[field]) not in resources.get(key,[]):return False
        for step in quote.get("steps",[]):
            if step.get("spender") and step["spender"] not in resources.get("spenders",[]):return False
            if step.get("router") and step["router"] not in resources.get("routers",[]):return False
        binding=quote.get("component_binding") or {}
        if binding.get("id", "").startswith("user:"):
            if binding["id"] not in resources.get("components",[]):return False
            if policy.get("resource_revisions",{}).get("component:"+binding["id"])!=binding.get("revision"):return False
        limits=policy["limits"]
        if amount(quote["risk_usd"],zero=True)>amount(limits["single_usd"]):return False
        if amount(quote["fee_usd"],zero=True)>amount(limits["fee_usd"],zero=True):return False
        if request.get("slippage_bps",0)>limits.get("slippage_bps",0):return False
        for asset,value in quote.get("asset_amounts",{}).items():
            if request["kind"]=="trade" and not limits.get("asset_amounts"):continue
            if amount(value,zero=True)>amount(limits.get("asset_amounts",{}).get(asset,0),zero=True):return False
        for asset,value in quote.get("exposure_asset_amounts",{}).items():
            if asset not in resources.get("assets",[]) or amount(value,zero=True)>amount(limits.get("asset_amounts",{}).get(asset,0),zero=True):return False
        if request["kind"]=="exchange_transfer":
            if any(request[field] not in resources.get("account_types",[]) for field in ("from_account","to_account")):return False
        if request['kind'] in ALLOWANCE_ACTIONS and amount(request['amount'],zero=True)>amount(limits['asset_amounts'].get(request['asset'],0),zero=True):return False
        if request.get('memo') and str(request['memo']) not in resources.get('memos',[]):return False
        if request['kind']=='trade':
            plan=request.get('plan') or {}
            if plan.get('action') not in limits.get('trade_actions',[]):return False
            if amount((plan.get('meta') or {}).get('leverage',1))>amount(limits.get('max_leverage',1)):return False
        return True

    def reserve(self,aid,context,*,quote_hash,approved=False):
        if context.plan_only:raise FinancialError("plan_mode_denies_financial_execution",403)
        now=time.time()
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
            if not row:raise FinancialError("financial_action_not_found",404)
            if row["actor_id"]!=context.actor_id:raise FinancialError("financial_owner_mismatch",403)
            if row["quote_hash"]!=quote_hash:raise FinancialError("financial_quote_changed")
            if row["state"] in {"reserved","submitting","submitted","confirming","confirmed","unconfirmed","needs_recovery"}:
                return {"duplicate":True,"action":self.public(row)}
            if row["state"] not in {"prepared","awaiting_approval"}:raise FinancialError("financial_action_not_executable")
            request=json.loads(row["request_json"]);quote=json.loads(row["quote_json"])
            if float(quote["expires_at"])<=now:raise FinancialError("financial_quote_expired")
            candidates=con.execute("""SELECT * FROM financial_grants WHERE actor_id=? AND task_kind=? AND task_id=?
                AND security_revision=? AND state='active' AND valid_from<=? AND expires_at>? ORDER BY created_at DESC""",
                (context.actor_id,context.task_kind,context.task_id,context.security_revision,now,now)).fetchall()
            from .resource_binding import resource_revisions
            def valid(g):
                policy=json.loads(g['policy_json'])
                return self.matches(policy,request,quote) and policy.get('resource_revisions')==resource_revisions(self.config,policy)
            grant=next((g for g in candidates if valid(g)),None)
            if approved:
                approval=con.execute("SELECT * FROM approvals WHERE id=?",(row["approval_id"],)).fetchone()
                frozen=json.loads(approval["payload"]) if approval else {}
                approved=bool(approval and approval["state"]=="approved" and approval["expires_at"]>now
                    and frozen.get("financial_action_id")==aid and frozen.get("quote_hash")==row["quote_hash"])
            if not grant and not approved:return {"needs_approval":True,"action":self.public(row)}
            value=amount(quote["risk_usd"],zero=True);fee=amount(quote["fee_usd"],zero=True)
            family=grant["family_id"] if grant else "manual:"+aid
            gid=grant["grant_id"] if grant else "approval:"+str(row["approval_id"] or "")
            if grant:
                policy=json.loads(grant["policy_json"]);limits=policy["limits"]
                usage=con.execute("SELECT * FROM financial_usage WHERE family_id=? AND state!='released'",(family,)).fetchall()
                metric=lambda u:(u['kind'] in ALLOWANCE_ACTIONS)==(request['kind'] in ALLOWANCE_ACTIONS)
                total=sum((amount(u["amount_usd"],zero=True) for u in usage if metric(u) and u['grant_id']==gid),Decimal(0))
                daily=sum((amount(u["amount_usd"],zero=True) for u in usage if metric(u) and u["created_at"]>now-86400),Decimal(0))
                fees=sum((amount(u["fee_usd"],zero=True) for u in usage if u['grant_id']==gid),Decimal(0))
                daily_fees=sum((amount(u['fee_usd'],zero=True) for u in usage if u['created_at']>now-86400),Decimal(0))
                if total+value>amount(limits["total_usd"]) or daily+value>amount(limits["rolling_24h_usd"]) or fees+fee>amount(limits["fee_usd"],zero=True) or daily_fees+fee>amount(limits.get('rolling_24h_fee_usd',limits['fee_usd']),zero=True):
                    return {"needs_approval":True,"reason":"grant_quota_exceeded","action":self.public(row)}
            resource="wallet:"+request["wallet_id"] if request.get("wallet_id") else request["account_id"]
            if request.get('wallet_id'):
                sender=quote.get('sender')
                if sender and request.get('chain')!='solana':sender=sender.lower()
                lock_key=('signer:'+sender if sender else resource)+':'+str(request.get('chain',''))
                lease=con.execute('SELECT action_id FROM financial_resource_leases WHERE resource_key=?',(lock_key,)).fetchone()
                if lease and lease['action_id']!=aid:raise FinancialError('wallet_financial_action_busy')
            if request.get('wallet_id') and quote.get('sender'):
                active=con.execute("""SELECT meta_json FROM capital_reservations WHERE state IN ('proposed','reserved')
                    AND (account_id=? OR json_extract(meta_json,'$.financial_sender')=?)
                    AND COALESCE(json_extract(meta_json,'$.chain'),?)=?""",(resource,sender,request.get('chain'),request.get('chain'))).fetchall()
            else:active=con.execute("SELECT meta_json FROM capital_reservations WHERE account_id=? AND state IN ('proposed','reserved')",(resource,)).fetchall()
            quantities={}
            for item in active:
                for asset,quantity in json.loads(item["meta_json"]).get("asset_amounts",{}).items():
                    quantities[asset]=quantities.get(asset,Decimal(0))+amount(quantity,zero=True)
            for asset,quantity in quote.get("asset_amounts",{}).items():
                available=quote.get("available_asset_amounts",{}).get(asset)
                if available is None or quantities.get(asset,Decimal(0))+amount(quantity,zero=True)>amount(available,zero=True):
                    raise FinancialError("insufficient_unreserved_asset_balance")
            account_limits=self.config.get("financial.account_limits",{}).get(resource)
            if request.get('wallet_id') and quote.get('sender'):
                from ..wallet.registry import list_configured_providers
                related=[]
                for binding in list_configured_providers(self.config.data):
                    address=str((binding.get('config') or {}).get('address') or '')
                    same=address==sender if request.get('chain')=='solana' else address.lower()==sender
                    ceiling=self.config.get('financial.account_limits',{}).get('wallet:'+binding['wallet_id'])
                    if same and ceiling:related.append(ceiling)
                if related:
                    account_limits={key:str(min(amount(value[key]) for value in related)) for key in ('single_usd','rolling_24h_usd')}
            if (context.run_id or context.strategy_run_id) and not account_limits:raise FinancialError('account_financial_limits_required',403)
            if account_limits:
                if request.get('wallet_id') and quote.get('sender'):
                    used=con.execute("""SELECT u.* FROM financial_usage u JOIN financial_actions f ON f.action_id=u.action_id
                        WHERE u.state!='released' AND u.created_at>? AND (u.account_id=? OR json_extract(f.quote_json,'$.sender')=?)""",
                        (now-86400,resource,quote['sender'])).fetchall()
                else:used=con.execute("SELECT * FROM financial_usage WHERE account_id=? AND state!='released' AND created_at>?",(resource,now-86400)).fetchall()
                daily=sum((amount(u["amount_usd"],zero=True) for u in used if (u['kind'] in ALLOWANCE_ACTIONS)==(request['kind'] in ALLOWANCE_ACTIONS)),Decimal(0))
                if value>amount(account_limits["single_usd"]) or daily+value>amount(account_limits["rolling_24h_usd"]):raise FinancialError("account_financial_limit_exceeded")
            candidate=(quote.get("budget_decision") or {}).get("candidate") if request["kind"]=="trade" else None
            reservation_meta={"financial_action_id":aid,"asset_amounts":quote.get("asset_amounts",{}),
                              'financial_sender':sender if request.get('wallet_id') else None,'chain':request.get('chain')}
            strategy_id=context.task_id or "manual_funds"
            if candidate:
                from ..trading.capital import check_owned_exit,CapitalReservation,_row_to_reservation
                from ..trading.instruments import position_bucket
                from ..core.errors import TradingError
                bucket=position_bucket(candidate.get("meta"));strategy_id=candidate["strategy_id"]
                if candidate["account_id"]!=resource or candidate["market"]!=request["market"]:
                    raise FinancialError("trade_plan_resource_mismatch",403)
                if candidate.get("reduce_only"):
                    try:check_owned_exit(con,account_id=resource,strategy_id=strategy_id,market=request["market"],
                        position_side=bucket,quantity=candidate.get("size_base") or 0)
                    except TradingError as exc:raise FinancialError(str(exc),403) from exc
                else:
                    reserved=con.execute("SELECT * FROM capital_reservations WHERE account_id=? AND state IN ('proposed','reserved')",(resource,)).fetchall()
                    blocked=sum((Decimal(str(_row_to_reservation(item).total_blocked_usd)) for item in reserved),Decimal(0))
                    available=amount(quote["budget_free_usd"],zero=True)
                    if available-blocked-amount(quote["spend_usd"],zero=True)-fee<amount(quote["minimum_free_usd"],zero=True):
                        raise FinancialError("budget_free_balance_floor_changed",403)
                reservation_meta.update(reduce_only=bool(candidate.get("reduce_only")),quantity_base=str(candidate.get("size_base") or 0),
                                        position_side=bucket,collateral_by_asset=candidate.get("required_collateral") or {})
            con.execute("INSERT INTO financial_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid,gid,family,resource,request["kind"],str(value),str(fee),encode(quote.get("asset_amounts",{})),"reserved",now,now))
            spend=amount(quote.get("spend_usd",quote["risk_usd"]),zero=True)
            collateral=amount(quote.get("collateral_usd",spend),zero=True)
            con.execute("""INSERT INTO capital_reservations(reservation_id,account_id,strategy_id,intent_id,market,side,
                notional_usd,estimated_fee_usd,estimated_margin_usd,state,created_at,updated_at,meta_json)
                VALUES (?,?,?,?,?,'buy',?,?,?,'reserved',?,?,?)""",
                ("financial:"+aid,resource,strategy_id,aid,request.get("market") or request.get("asset") or request["kind"],
                 float(value if candidate else spend),float(fee),float(collateral),now,now,encode(reservation_meta)))
            con.execute("UPDATE financial_actions SET state='reserved',grant_id=?,revision=revision+1,updated_at=? WHERE action_id=?",(grant["grant_id"] if grant else None,now,aid))
            if request.get('wallet_id'):
                con.execute('INSERT OR IGNORE INTO financial_resource_leases VALUES (?,?,?)',(lock_key,aid,now))
            return {"duplicate":False,"action":self.public(con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone())}

    def mark(self,aid,*,state,submission=None,receipt=None,error_code=None,expected_state=None):
        with self.transaction() as con:
            row=con.execute("SELECT * FROM financial_actions WHERE action_id=?",(aid,)).fetchone()
            if not row:raise FinancialError("financial_action_not_found",404)
            if expected_state and row["state"]!=expected_state:return False
            if row["state"]=="confirmed" and state!="confirmed":return False
            con.execute("""UPDATE financial_actions SET state=?,submission_json=?,receipt_json=?,error_code=?,
                revision=revision+1,updated_at=? WHERE action_id=?""",
                (state,encode(redact_public_record(submission,identity_fields=self.PUBLIC_IDENTITIES)) if submission is not None else row["submission_json"],
                 encode(redact_public_record(receipt,identity_fields=self.PUBLIC_IDENTITIES)) if receipt is not None else row["receipt_json"],error_code,time.time(),aid))
            if state in {"confirmed","failed_before_submission","rejected"}:
                target="consumed" if state=="confirmed" else "released"
                if receipt and 'settled_notional_usd' in receipt:
                    value=amount(receipt['settled_notional_usd'],zero=True)
                    fee=amount(receipt.get('settled_fee_usd',0),zero=True)
                    con.execute('UPDATE financial_usage SET amount_usd=?,fee_usd=? WHERE action_id=?',(str(value),str(fee),aid))
                    if fee>0 or value>0:target='consumed'
                con.execute("UPDATE financial_usage SET state=?,updated_at=? WHERE action_id=? AND state='reserved'",(target,time.time(),aid))
                con.execute("UPDATE capital_reservations SET state=?,updated_at=? WHERE reservation_id=? AND state='reserved'",('consumed' if state=='confirmed' else 'released',time.time(),"financial:"+aid))
                con.execute('DELETE FROM financial_resource_leases WHERE action_id=?',(aid,))
            elif receipt and receipt.get('source_confirmed'):
                # Source debit is already reflected in live balances. Keep
                # permission quota held while avoiding a second principal hold.
                con.execute("UPDATE capital_reservations SET state='consumed',updated_at=? WHERE reservation_id=? AND state='reserved'",(time.time(),'financial:'+aid))
            if row['parent_action_id']:
                con.execute("UPDATE financial_steps SET state=?,receipt_json=?,updated_at=? WHERE action_id=? AND json_extract(submission_json,'$.child_action_id')=?",
                    (state,encode(receipt or {}),time.time(),row['parent_action_id'],aid))
            return True
