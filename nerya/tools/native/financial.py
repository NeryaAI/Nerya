"""Funds tools enter the producer-owned financial gate; no grant approval tool."""
from ..registry import make_native_descriptor
from ..types import ToolResult,ToolError,ToolErrorKind,RiskLevel,PermissionScope
from ...financial.contracts import FinancialError,context_from_config
from ...financial.gateway import FinancialGateway


def register_financial_tools(registry,deps,*,replace=False):
    def handler(action):
        def call(tool):
            try:
                context=context_from_config(deps.config,actor_id=deps.config.get("runtime.financial_actor_id"),
                                            scopes=deps.config.get("runtime.financial_actor_scopes",()))
                gateway=FinancialGateway(deps.config)
                if action=='readiness':
                    context.require('read:funds')
                    from ...financial.readiness import execution_readiness
                    result=execution_readiness(deps.config)
                elif action=="components":
                    from ...trading.components import trading_components
                    context.require("read:funds")
                    result=trading_components(deps.config).describe()
                elif action=="account_state":result=gateway.account_state(context,tool.arguments["request"])
                elif action=="prepare":
                    result=gateway.prepare(context,tool.arguments["request"],action_key=f"{context.run_id or context.actor_id}:{tool.id}",parent_action_id=tool.arguments.get('parent_action_id'))
                elif action=="execute":result=gateway.execute(context,tool.arguments["action_id"],quote_hash=tool.arguments["quote_hash"])
                elif action=='refresh':result=gateway.refresh(context,tool.arguments['action_id'],expected_revision=tool.arguments['expected_revision'])
                elif action=='discard':result=gateway.discard(context,tool.arguments['action_id'],expected_revision=tool.arguments['expected_revision'])
                elif action=="reconcile":result=gateway.reconcile(context,tool.arguments["action_id"])
                else:result=gateway.store.get_action(tool.arguments["action_id"],context)
                if result.get("status")=="approval_required":
                    return ToolResult.from_error(tool_use_id=tool.id,name=tool.name,error=ToolError(
                        kind=ToolErrorKind.PERMISSION_PENDING,message="This fixed financial action requires operator approval.",
                        retryable=False,recovery_hint={"approval_id":result["approval_id"],"tool_name":tool.name,
                            "payload":tool.arguments,"caller":tool.caller,"approval_request":result}))
                return ToolResult.from_json(tool_use_id=tool.id,name=tool.name,data=result)
            except FinancialError as exc:
                return ToolResult.from_error(tool_use_id=tool.id,name=tool.name,error=ToolError(
                    kind=ToolErrorKind.PERMISSION_DENIED if exc.status==403 else ToolErrorKind.EXECUTION_ERROR,
                    message=exc.code,retryable=False))
        return call
    descriptors=[]
    for action in ("readiness","components","account_state","prepare","execute","get","reconcile","refresh","discard"):
        fields={} if action in {'components','readiness'} else {"request":{"type":"object"}} if action in {"prepare","account_state"} else {"action_id":{"type":"string"}}
        if action=="execute":fields["quote_hash"]={"type":"string"}
        if action in {'refresh','discard'}:fields['expected_revision']={'type':'integer'}
        required=list(fields)
        if action=='prepare':fields['parent_action_id']={'type':'string'}
        descriptors.append(make_native_descriptor(name="financial_"+action,
            description={"readiness":"Read local trading dependencies and configured execution prerequisites. Does not resolve secrets, call providers, enable trading or certify live-money execution.",
                         "components":"Read available workspace trading components, immutable revisions, actions and parameter schemas. A declaration is not live verification.",
                         "account_state":"Read actual protocol account state, positions, collateral, debt and collection health through an available component. No order or signature.",
                         "prepare":"Prepare a fixed financial action and quote. Read financial_ops Skill first. No funds are sent.",
                         "execute":"Execute a prepared action within a finite approved policy. Missing authorization pauses for an operator; never auto-retry uncertainty.",
                         "get":"Read the observed receipt of an owned financial action.",
                         "reconcile":"Query the existing provider/chain identity. Never re-send an operation.",
                         'refresh':'Refresh an unsubmitted quote after confirmed prerequisites. Invalidates old approvals. Never changes an already submitted transaction.',
                         'discard':'Discard an unsubmitted plan. Does not cancel broadcast transactions or revoke chain allowances.'}[action],
            input_schema={"type":"object","properties":fields,"required":required,"additionalProperties":False},
            handler=handler(action),risk=RiskLevel.DANGEROUS if action=="execute" else RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,read_only=action in {"get","account_state","components","readiness"},is_concurrency_safe=action in {"get","account_state","components","readiness","prepare"},
            tags=("financial","funds"),auto_approve=True))
    registry.register_all(descriptors,replace=replace)
    _register_rebalance_tools(registry, deps, replace=replace)


def _register_rebalance_tools(registry, deps, *, replace=False):
    from ...financial.rebalance import RebalanceService

    def handler(action):
        def call(tool):
            try:
                context = context_from_config(deps.config, actor_id=deps.config.get('runtime.financial_actor_id'),
                    scopes=deps.config.get('runtime.financial_actor_scopes', ()))
                service = RebalanceService(deps.config)
                p = tool.arguments
                if action == 'prepare':
                    result = service.create(context, p['plan'], client_key=p['client_request_id'])
                elif action == 'get':
                    result = service.get(context, p['rebalance_id'])
                else:
                    result = getattr(service, action)(context, p['rebalance_id'], expected_revision=p['expected_revision'])
                return ToolResult.from_json(tool_use_id=tool.id, name=tool.name, data=result)
            except FinancialError as exc:
                return ToolResult.from_error(tool_use_id=tool.id, name=tool.name, error=ToolError(
                    kind=ToolErrorKind.PERMISSION_DENIED if exc.status == 403 else ToolErrorKind.EXECUTION_ERROR,
                    message=exc.code, retryable=False))
        return call

    descriptions = {
        'prepare': 'Prepare a bounded LP remove, optional ratio swap, and new-range add workflow. Read financial_ops. No funds are sent and no permissions are granted.',
        'advance': 'Advance one LP rebalance step through existing financial gates; reconcile original hashes before progressing. Never re-send uncertain steps.',
        'get': 'Read an owned LP rebalance plan, confirmed proceeds, child actions and recovery state.',
        'stop': 'Stop an LP rebalance only after outstanding child actions are resolved or discarded. Does not cancel transactions or automatically unwind assets.',
    }
    descriptors = []
    for action, description in descriptions.items():
        fields = ({'plan': {'type': 'object'}, 'client_request_id': {'type': 'string'}} if action == 'prepare' else
                  {'rebalance_id': {'type': 'string'}})
        if action in {'advance', 'stop'}:
            fields['expected_revision'] = {'type': 'integer', 'minimum': 1}
        descriptors.append(make_native_descriptor(name='financial_rebalance_' + action, description=description,
            input_schema={'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False},
            handler=handler(action), risk=RiskLevel.DANGEROUS if action == 'advance' else RiskLevel.READ if action == 'get' else RiskLevel.WRITE,
            permission_scope=PermissionScope.WORKSPACE, read_only=action == 'get', is_concurrency_safe=action == 'get',
            tags=('financial', 'funds', 'lp'), auto_approve=True))
    registry.register_all(descriptors, replace=replace)
