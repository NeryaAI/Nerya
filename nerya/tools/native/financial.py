"""Funds tools enter the producer-owned financial gate; no grant approval tool."""
from ..registry import make_native_descriptor
from ..types import ToolResult,ToolError,ToolErrorKind,RiskLevel,PermissionScope
from ...financial.contracts import FinancialError,KINDS,context_from_config
from ...financial.gateway import FinancialGateway


_PAPER_DISCOVERY_SCOPES = frozenset({"execute:strategy", "trade:paper"})


def _lending_pool_identity(pool):
    """Map reviewed catalog identity to the existing chain-qualified asset grammar."""
    import re
    from ...connectors.chains import ChainRegistry
    from ...core.errors import TradingError

    if pool.get("project") != "aave-v3":
        return None
    asset = pool.get("symbol")
    if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9_.]+", asset):
        return None
    chain = pool.get("chain")
    if not isinstance(chain, str):
        return None
    try:
        chain = ChainRegistry().get(chain).name
    except TradingError:
        return None
    return {"market": f"AAVE_V3:{chain}-{asset}", "protocol": "aave_v3",
            "component_id": "builtin:aave", "chain": chain, "asset": asset,
            "pool_id": pool.get("pool_id")}


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
    kind = str(args.get("kind") or "").strip()
    requested = {key: args[key] for key in ("protocol", "market", "kind") if args.get(key)}
    result.update({
        "inventory_scope": "paper_backtest",
        "financial_execution_allowed": False,
        "historical_data_verified": False,
        "requested": requested,
        "next_required_action": {
            "tool": "Skill",
            "arguments": {"skill": "backtest", "file": "references/replay-capabilities.md"},
            "requested": dict(requested),
            "preserve_requested_scope": True,
            "allow_proxy_substitution": False,
            "hint": (
                "Inspect the replay contract and an advertised read-only historical source for the exact requested "
                "protocol/market and original user window. Component matching is execution metadata, not historical "
                "OHLC or replay coverage; a current quote and an unsupported symbol enumerator establish neither. "
                "If unavailable, retain the disabled candidate where feasible and report the exact missing "
                "source, observed coverage and replay capability. Do not substitute a venue/token, invent an "
                "adapter, waive evidence or grant permissions. A limitation is not authorization."
            ),
        },
        "capability_note": (
            "This is read-only component metadata for paper/backtest routing. "
            "It grants no funds scope, approval, signer or live execution. "
            "Installed actions do not establish historical data or replay coverage."
        ),
    })
    components = [row for row in result.get("components", []) if row.get("available")]
    if protocol:
        # Only aliases supported by the existing financial adapters. Jupiter
        # swaps use TradingFunds, not a component named builtin:jupiter.
        component_name = protocol.replace("-", "_")
        component_id = "builtin:" + {
            "jupiter": "trading", "jupiter_v2": "trading",
            "uniswap_v3": "uniswap", "uniswap_v4": "uniswap",
            "aave_v3": "aave", "pancakeswap_v3": "pancakeswap",
        }.get(component_name, component_name)
        components = [row for row in components if row.get("id") == component_id]
        if components:
            component = components[0]
            result["matched_component"] = {"id": component["id"], "revision": component["revision"]}

    actions = sorted({str(action) for row in components for action in row.get("actions", [])})
    if protocol == 'uniswap_v3' and components:
        result['pool_discovery'] = {
            'status': 'unresolved', 'canonical_pool_identity': None,
            'pair_is_pool_identity': False, 'historical_data_verified': False,
            'next_required_action': {'tool': 'data_api', 'arguments': {
                'op': 'schema', 'provider': 'uniswap', 'action': 'pool_discover'}},
            'skill': 'strategy_author', 'file': 'references/mainstream-strategies.md',
            'message': 'Component inventory does not resolve a pool. Inspect uniswap.pool_discover '
                'for bounded Ethereum chain 1 current pool identity using exact catalog symbols '
                'or token addresses. It is not an ETH/WETH alias or historical coverage proof. '
                'Keep multiple fee-tier candidates explicit. uniswap.pool_history separately requires '
                'a verified pool_address and requested window. Other chains remain unresolved by '
                'this source; retain exact scope. ETH/WETH mapping requires explicit admitted task instructions.',
        }
    if (protocol and not components) or (kind and kind not in actions):
        result["capability_gap"] = {
            "code": "paper_replay_component_unavailable",
            "status": "blocked",
            "retryable": False,
            "requested": {key: value for key, value in (("protocol", protocol), ("market", market), ("kind", kind)) if value},
            "available_actions": actions,
            "missing_actions": [kind] if kind else [],
            "reason": (
                "No available installed component advertises the requested protocol."
                if protocol and not components else
                f"The available components in this inventory scope do not advertise the requested action {kind}."
            ),
            "next_required_action": "stage a reviewed component for the requested capability or retain the blocked receipt",
        }
    return result


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
                "kind": {
                    "type": "string", "enum": sorted(KINDS),
                    "description": "Optional financial request kind to check against advertised actions in paper/backtest mode. With protocol, checks that component; otherwise checks available inventory. Omission inventories all actions.",
                },
            }
        if action=="execute":fields["quote_hash"]={"type":"string"}
        if action in {'refresh','discard'}:fields['expected_revision']={'type':'integer'}
        required=[] if action in {'components', 'readiness'} else list(fields)
        if action=='prepare':fields['parent_action_id']={'type':'string'}
        descriptors.append(make_native_descriptor(name="financial_"+action,
            description={"readiness":"Read local dependencies and builtin execution prerequisites. Discover workspace components through financial_components; absence here does not establish missing provider support. Does not resolve secrets, call providers, enable trading or certify live-money execution.",
                         "components":"Discover builtin and workspace financial execution capabilities, including Jupiter swaps, independently of connector_list and wallet data_api catalogs. Read financial_ops for protocol routing. Returns immutable revisions, actions and parameter schemas. For paper/backtest pass mode=paper or mode=backtest plus the requested protocol/market without funds access. Installed actions establish neither configured wallets, historical data/replay coverage nor live verification; no funds or approval are granted.",
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
