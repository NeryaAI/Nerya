"""Funds tools enter the producer-owned financial gate; no grant approval tool."""
from ..registry import make_native_descriptor
from ..types import ToolResult,ToolError,ToolErrorKind,RiskLevel,PermissionScope
from ...financial.contracts import FinancialError,context_from_config
from ...financial.gateway import FinancialGateway


_PAPER_DISCOVERY_SCOPES = frozenset({"execute:strategy", "trade:paper"})


def _component_discovery_mode(tool) -> str | None:
    mode = str((tool.arguments or {}).get("mode") or "").strip().lower()
    return mode if mode in {"paper", "backtest", "live"} else None


def _require_component_discovery(context, tool) -> str | None:
    """Allow bounded paper/backtest inventory without granting funds access."""

    mode = _component_discovery_mode(tool)
    if mode in {"paper", "backtest"}:
        scopes = set(context.scopes)
        if "api:all" in scopes or "read:funds" in scopes:
            return mode
        if scopes & _PAPER_DISCOVERY_SCOPES:
            return mode
    context.require("read:funds")
    return mode


def _paper_component_contract(result, tool):
    args = tool.arguments or {}
    protocol = str(args.get("protocol") or "").strip().lower()
    market = str(args.get("market") or "").strip()
    result.update({
        "inventory_scope": "paper_backtest",
        "financial_execution_allowed": False,
        "capability_note": (
            "This is read-only component metadata for paper/backtest routing. "
            "It grants no funds scope, approval, signer or live execution."
        ),
    })
    if not protocol:
        return result

    component_id = "builtin:" + protocol.replace("-", "_")
    component = next((row for row in result.get("components", []) if row.get("id") == component_id), None)
    if component is None:
        result["capability_gap"] = {
            "code": "paper_replay_component_unavailable",
            "status": "blocked",
            "retryable": False,
            "requested": {"protocol": protocol, **({"market": market} if market else {})},
            "available_actions": [],
            "missing_actions": ["swap"],
            "reason": "No installed component advertises the requested protocol.",
            "next_required_action": "stage a reviewed protocol replay component or retain the blocked receipt",
        }
        return result

    actions = sorted(str(action) for action in component.get("actions", []))
    if not ({"trade", "swap"} & set(actions)):
        result["capability_gap"] = {
            "code": "paper_replay_component_unavailable",
            "status": "blocked",
            "retryable": False,
            "requested": {"protocol": protocol, **({"market": market} if market else {})},
            "available_actions": actions,
            "missing_actions": ["swap"],
            "reason": (
                f"{component_id} exposes the listed protocol actions only; it does not "
                "provide a paper/backtest swap execution surface."
            ),
            "next_required_action": "stage a reviewed protocol replay component or retain the blocked receipt",
        }


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
                    mode = _require_component_discovery(context, tool)
                    result=trading_components(deps.config).describe()
                    if mode in {"paper", "backtest"}:
                        _paper_component_contract(result, tool)
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
        if action == "components":
            fields = {
                "mode": {"type": "string", "enum": ["paper", "backtest", "live"]},
                "protocol": {"type": "string"},
                "market": {"type": "string"},
            }
        if action=="execute":fields["quote_hash"]={"type":"string"}
        if action in {'refresh','discard'}:fields['expected_revision']={'type':'integer'}
        required=[] if action in {'components', 'readiness'} else list(fields)
        if action=='prepare':fields['parent_action_id']={'type':'string'}
        descriptors.append(make_native_descriptor(name="financial_"+action,
            description={"readiness":"Read local dependencies and builtin execution prerequisites. Discover workspace components through financial_components; absence here does not establish missing provider support. Does not resolve secrets, call providers, enable trading or certify live-money execution.",
                         "components":"Read available workspace trading components, immutable revisions, actions and parameter schemas. For paper/backtest pass mode=paper or mode=backtest plus the requested protocol/market; this is a bounded read-only inventory and never grants funds or approval. A declaration is not live verification.",
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
