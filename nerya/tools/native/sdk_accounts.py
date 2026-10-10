"""Agent-owned SDK preparation and read-only account observation collection."""
from ..registry import make_native_descriptor
from ..types import ToolError, ToolErrorKind, ToolResult, RiskLevel, PermissionScope
from .skill import _script_path, skill_access_error, skill_asset_access_error


SCHEMA = {'type':'object','properties':{
    'op':{'type':'string','enum':['list','inspect','prepare','refresh']},
    'connector_id':{'type':'string'}, 'label':{'type':'string'},
    'skill_id':{'type':'string'}, 'script':{'type':'string'},
    'runtime':{'type':'string','enum':['python','node']},
    'requirements':{'type':'array','items':{'type':'string'},'maxItems':20},
    'payload':{'type':'object'}, 'env_refs':{'type':'object','additionalProperties':{'type':'string'}},
},'required':['op'],'additionalProperties':False}


def register_sdk_account_tools(registry, deps, *, replace=False):
    def autonomous():
        return bool(deps.config and deps.permission_mode in {'auto','yolo'}
                    and deps.config.get('runtime.allow_auto_install') is not False)

    def handler(call):
        from ...install import sdk_runtime as sdk
        args = dict(call.arguments or {})
        op = args.pop('op', None)
        try:
            if deps.config is None:
                raise sdk.SDKError('workspace_config_required')
            if op == 'list':
                result = sdk.list_connections(deps.config)
            else:
                identifier = args.get('connector_id')
                directory = sdk.path_for(deps.config, identifier)
                record = sdk.load(directory / 'manifest.json')
                if op == 'inspect':
                    if record is None:
                        raise sdk.SDKError('sdk_connection_not_found')
                    result = {'ok':True,'connector':sdk.public_state(deps.config,record)}
                else:
                    spec = args if op == 'prepare' else record
                    if not spec or not spec.get('skill_id') or not spec.get('script'):
                        raise sdk.SDKError('sdk_skill_and_script_required')
                    denied = skill_access_error(call, spec['skill_id'])
                    if denied is not None:
                        return denied
                    path = _script_path(deps.skill_index, spec['skill_id'], spec['script'])
                    if path is None:
                        raise sdk.SDKError('sdk_skill_script_not_found')
                    denied = skill_asset_access_error(call, deps.skill_index.get(spec['skill_id']), path, deps.skill_index)
                    if denied is not None:
                        return denied
                    if op == 'prepare':
                        result = sdk.prepare(deps.config, spec, script_path=path, approve=autonomous())
                        if not result['ok']:
                            return ToolResult.from_error(tool_use_id=call.id,name=call.name,error=ToolError(
                                kind=ToolErrorKind.EXECUTION_ERROR,message=result.get('error') or 'sdk_prepare_failed',
                                detail=result,retryable=False,recovery_hint={
                                    'action':'inspect_sdk_connection','hint':'Inspect the preparation result; correct dependency/installation policy before retrying.'}))
                    if op == 'refresh':
                        result = sdk.collect(deps.config, identifier)
            response = ToolResult.from_json(tool_use_id=call.id,name=call.name,data=result)
            response.metadata['read_only'] = op in {'list','inspect','refresh'}
            return response
        except Exception as exc:
            code = str(exc) if isinstance(exc,sdk.SDKError) else type(exc).__name__
            response = ToolResult.from_error(tool_use_id=call.id,name=call.name,error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,message=code,retryable=False,
                recovery_hint={'action':'sdk_account','hint':'Inspect connection state and correct SDK/script/credentials; last good data is retained. Do not retry unchanged.'}))
            response.metadata['read_only'] = op in {'list','inspect','refresh'}
            return response

    registry.register(make_native_descriptor(name='sdk_account', description=(
        'Prepare only the pinned Python/npm SDKs required by an approved Skill script, then collect its '
        'standard account_state result for the portfolio UI. No provider SDK is installed at boot. '
        'Use op=prepare with connector_id, skill_id, script, runtime, exact-version requirements and public payload; '
        'op=refresh collects balances, protocol positions and period-scoped income/PnL. '
        'Auto/yolo modes prepare workspace-local environments unless automatic installation is explicitly disabled. '
        'env_refs must be vault references with sdk.read scope; never pass signing keys. '
        'Outputs are display-only observations and never authorize orders or alter trading NAV.'),
        input_schema=SCHEMA, handler=handler, risk=RiskLevel.EXEC, permission_scope=PermissionScope.NETWORK,
        read_only=False,is_concurrency_safe=False,tags=('sdk','accounts','portfolio'),result_kind='json',
        auto_approve_when=lambda args: args.get('op') in {'list','inspect'} or autonomous()),replace=replace)
