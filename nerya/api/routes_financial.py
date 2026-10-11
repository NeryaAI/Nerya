"""Operator financial controls. All identities come from dispatcher auth."""
from functools import wraps

from ..financial.contracts import FinancialContext,FinancialError
from ..financial.store import FinancialStore
from ..financial.gateway import FinancialGateway


def _context(client):
    actor=getattr(client,"auth_actor_id",None)
    if not actor:raise FinancialError("financial_actor_required",403)
    return FinancialContext(actor,frozenset(getattr(client,"auth_scopes",())))


def _operator(context):return "api:all" in context.scopes or "admin:ops" in context.scopes


def _safe(fn):
    @wraps(fn)
    def wrap(client,payload):
        try:return fn(client,payload or {})
        except FinancialError as exc:return {"ok":False,"error":exc.code,"_status":exc.status,"retrying":False}
        except (KeyError,TypeError,ValueError):return {"ok":False,"error":"invalid_financial_request","_status":400,"retrying":False}
    return wrap


def routes():
    @_safe
    def readiness(client, p):
        _context(client).require('read:funds')
        from ..financial.readiness import execution_readiness
        return {'ok':True, **execution_readiness(client.config)}

    @_safe
    def list_grants(client,p):
        ctx=_context(client)
        return {"ok":True,"grants":FinancialStore(client.config).list_grants(ctx,operator=_operator(ctx),task_id=p.get("task_id"),task_kind=p.get('task_kind'))}

    @_safe
    def create_grant(client,p):
        ctx=_context(client)
        from ..financial.task_binding import task_security_revision
        security=task_security_revision(client.config,p["task_kind"],p["task_id"])
        if p.get("expected_security_revision")!=security:raise FinancialError("financial_task_security_revision_changed")
        owner=ctx.actor_id
        if p['task_kind']=='scheduled_agent':
            from ..triggers.schedule import load_schedules
            entry=next((e for e in load_schedules(client.config.paths) if e.id==p['task_id']),None)
            if not entry:raise FinancialError('task_not_found',404)
            owner=entry.owner_actor_id
        else:owner=client.config.get('runtime.strategy_owner_actor_id','local:loopback')
        if owner!=ctx.actor_id and not _operator(ctx):raise FinancialError('grant_task_owner_mismatch',403)
        grant=FinancialStore(client.config).create_grant(ctx,task_kind=p["task_kind"],task_id=p["task_id"],
            security_revision=security,policy=p["policy"],expires_at=p.get("expires_at"),valid_from=p.get("valid_from"),
            subject_actor_id="strategy:"+p["task_id"] if p["task_kind"]=="strategy_script" else owner)
        return {"ok":True,"grant":grant,"_status":201}

    @_safe
    def approve_grant(client,p):
        ctx=_context(client);store=FinancialStore(client.config)
        grant=store.get_grant(p["grant_id"],ctx,operator=_operator(ctx))
        from ..financial.task_binding import task_security_revision
        if grant["security_revision"]!=task_security_revision(client.config,grant["task_kind"],grant["task_id"]):
            raise FinancialError("financial_task_security_revision_changed")
        return {"ok":True,"grant":store.approve_grant(grant["grant_id"],ctx,expected_revision=p["expected_revision"])}

    @_safe
    def revoke_grant(client,p):
        ctx=_context(client)
        return {"ok":True,"grant":FinancialStore(client.config).revoke_grant(p["grant_id"],ctx,expected_revision=p["expected_revision"])}

    @_safe
    def list_actions(client,p):
        ctx=_context(client)
        return {"ok":True,"actions":FinancialStore(client.config).list_actions(ctx,operator=_operator(ctx),run_id=p.get("run_id"),limit=p.get("limit",50))}

    @_safe
    def action(client,p):
        ctx=_context(client)
        return {"ok":True,"action":FinancialStore(client.config).get_action(p["action_id"],ctx,operator=_operator(ctx))}

    @_safe
    def prepare(client,p):
        ctx=_context(client);ctx.require("read:funds")
        return {"ok":True,"action":FinancialGateway(client.config).prepare(ctx,p["request"],action_key=p["client_request_id"],parent_action_id=p.get('parent_action_id'))}

    @_safe
    def execute(client,p):
        ctx=_context(client);store=FinancialStore(client.config)
        existing=store.get_action(p["action_id"],ctx)
        ctx.require("approve:trade" if existing["kind"] in {"trade","swap"} else "approve:funds")
        # A UI request cannot borrow the task identity from an arbitrary body.
        if existing.get("run_id") or existing.get("strategy_run_id") or existing['context'].get('command_id'):raise FinancialError("task_financial_action_requires_original_approval")
        return {"ok":True,"action":FinancialGateway(client.config).execute(ctx,p["action_id"],quote_hash=p["quote_hash"]),"_status":202}

    @_safe
    def reconcile(client,p):
        ctx=_context(client);ctx.require("read:funds")
        return {"ok":True,"action":FinancialGateway(client.config).reconcile(ctx,p["action_id"])}

    @_safe
    def refresh(client,p):
        ctx=_context(client);ctx.require('read:funds')
        return {'ok':True,'action':FinancialGateway(client.config).refresh(ctx,p['action_id'],expected_revision=p['expected_revision'])}

    @_safe
    def capabilities(client,p):
        from ..financial.adapters import financial_capabilities
        return {"ok":True,"enabled":bool(client.config.get("financial.enabled",False)),"capabilities":financial_capabilities(client.config)}

    @_safe
    def components(client,p):
        ctx=_context(client);ctx.require("read:funds")
        from ..trading.components import trading_components
        return {"ok":True,**trading_components(client.config).describe()}

    @_safe
    def component_state(client,p):
        return {"ok":True,"account_state":FinancialGateway(client.config).account_state(_context(client),p["request"])}

    @_safe
    def reload_components(client,p):
        ctx=_context(client);ctx.require("write:config")
        if client.config.get("runtime.task_run_id") or client.config.get("runtime.strategy_financial_run_id"):
            raise FinancialError("background_task_cannot_reload_components",403)
        from ..trading.components import trading_components
        return {"ok":True,**trading_components(client.config).reload(client.config)}

    def resource_config(client,p):
        from ..financial.contracts import digest
        resource=str(p.get('resource_id') or '')
        wallet=resource.startswith('wallet:')
        permissions=client.config.get('financial.wallet_permissions' if wallet else 'financial.account_permissions',{}).get(resource[7:] if wallet else resource,{})
        limits=client.config.get('financial.account_limits',{}).get(resource,{})
        data={'permissions':permissions,'limits':limits}
        return {'ok':True,'resource':resource,**data,'revision':digest(data)}

    @_safe
    def rebalance_prepare(client, p):
        from ..financial.rebalance import RebalanceService
        return {'ok': True, 'rebalance': RebalanceService(client.config).create(_context(client), p['plan'], client_key=p['client_request_id'])}

    @_safe
    def rebalance_get(client, p):
        from ..financial.rebalance import RebalanceService
        return {'ok': True, 'rebalance': RebalanceService(client.config).get(_context(client), p['rebalance_id'])}

    def rebalance_change(operation):
        @_safe
        def change(client, p):
            from ..financial.rebalance import RebalanceService
            ctx = _context(client)
            ctx.require('approve:funds')
            service = RebalanceService(client.config)
            row = service.get(ctx, p['rebalance_id'])
            if operation == 'advance' and any(request['kind'] == 'swap' for request in row['plan']['requests']):
                ctx.require('approve:trade')
            return {'ok': True, 'rebalance': getattr(service, operation)(ctx, p['rebalance_id'], expected_revision=p['expected_revision'])}
        return change

    @_safe
    def update_resource(client,p):
        ctx=_context(client);ctx.require('write:config');ctx.require('approve:funds')
        if client.config.get('runtime.task_run_id'):raise FinancialError('background_task_cannot_modify_funds_permissions',403)
        from ..financial.contracts import amount,digest
        from ..core import yaml_io,jsonl
        resource=str(p['resource_id']);wallet=resource.startswith('wallet:')
        rid=resource[7:] if wallet else resource
        if wallet:
            from ..wallet.registry import list_configured_providers
            if not any(b['wallet_id']==rid for b in list_configured_providers(client.config.data)):raise FinancialError('wallet_not_found',404)
        else:
            from ..trading.accounts import get_account_profile
            get_account_profile(client.config.paths,rid)
        from ..financial.contracts import PROTOCOL_ACTIONS,ALLOWANCE_ACTIONS
        permissions=p['permissions'];allowed=({'wallet_transfer','bridge_swap','swap'}|PROTOCOL_ACTIONS|ALLOWANCE_ACTIONS) if wallet else {'withdraw','transfer'}
        if not isinstance(permissions,dict) or set(permissions)-allowed or any(type(v) is not bool for v in permissions.values()):raise FinancialError('invalid_resource_permissions',400)
        limits={key:format(amount(p['limits'].get(key)),'f') for key in ('single_usd','rolling_24h_usd')}
        if amount(limits['single_usd'])>amount(limits['rolling_24h_usd']):raise FinancialError('inconsistent_resource_limits',400)
        store=FinancialStore(client.config)
        with store.transaction():
            raw=yaml_io.load(client.config.paths.config,default={}) or {}
            finance=raw.setdefault('financial',{})
            current=finance.setdefault('wallet_permissions' if wallet else 'account_permissions',{})
            ceilings=finance.setdefault('account_limits',{})
            if p.get('expected_revision')!=digest({'permissions':current.get(rid,{}),'limits':ceilings.get(resource,{})}):raise FinancialError('revision_conflict')
            current[rid]=permissions;ceilings[resource]=limits
            yaml_io.dump(client.config.paths.config,raw)
            client.config.data.setdefault('financial',{}).update(finance)
            jsonl.append(client.config.paths.journal('financial_permissions'),{'resource_id':resource,'actor_id':ctx.actor_id,
                'permissions':permissions,'limits':limits,'revision':digest({'permissions':permissions,'limits':limits})})
        return resource_config(client,p)

    return [("GET","/financial/grants",list_grants),("POST","/financial/grants",create_grant),
        ("POST","/financial/grants/{grant_id}/approve",approve_grant),("POST","/financial/grants/{grant_id}/revoke",revoke_grant),
        ("GET","/financial/actions",list_actions),("GET","/financial/actions/{action_id}",action),
        ("POST","/financial/actions/prepare",prepare),("POST","/financial/actions/{action_id}/execute",execute),
        ("POST","/financial/actions/{action_id}/reconcile",reconcile),("GET","/financial/capabilities",capabilities),
        ('POST','/financial/actions/{action_id}/refresh',refresh),
        ("GET","/financial/readiness",readiness),
        ("GET","/financial/components",components),("POST","/financial/components/reload",reload_components),
        ("POST","/financial/components/account-state",component_state),
        ("POST","/financial/rebalances/prepare",rebalance_prepare),
        ("GET","/financial/rebalances/{rebalance_id}",rebalance_get),
        ("POST","/financial/rebalances/{rebalance_id}/advance",rebalance_change('advance')),
        ("POST","/financial/rebalances/{rebalance_id}/stop",rebalance_change('stop')),
        ("GET","/financial/resources/{resource_id}",_safe(resource_config)),
        ("POST","/financial/resources/{resource_id}",update_resource)]
