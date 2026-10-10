from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
import shutil

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.install import sdk_runtime as sdk
from nerya.trading.asset_observations import ObservationError, normalize_observation

pytestmark = pytest.mark.smoke


def config(tmp_path):
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def adapter(tmp_path, name='reader.py'):
    script = tmp_path / 'skills' / 'sdk_reader' / 'scripts' / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('''import json,sys,time
request=json.load(sys.stdin)
assert request['command']=='account_state'
now=time.time()
print(json.dumps({'schema_version':1,'as_of':now,'address':request['payload'].get('address'),
 'balances':[{'chain':request['payload'].get('chain','base'),'token':'USDC','symbol':'USDC',
 'raw_amount':'1234567','decimals':6,'price_usd':'1'}],
 'positions':[{'id':'lp1','kind':'lp','protocol':'example','label':'LP position','value_usd':'25',
 'claimable_rewards_usd':'0.35'}],
 'performance':{'realized_pnl_usd':'2.50','income_usd':'0.20','fees_usd':'0.03',
 'period_start':now-86400,'period_end':now}}))
''')
    return script


def spec(identifier='base_wallet', **changes):
    return {'connector_id': identifier, 'label': identifier, 'skill_id': 'sdk_reader', 'script': 'reader.py',
            'runtime': 'python', 'requirements': [], 'payload': {'chain': 'base', 'address': 'owner'}, **changes}


def test_two_sdk_scripts_feed_one_persisted_ui_contract_without_global_sdk_install(tmp_path):
    cfg = config(tmp_path)
    for identifier, chain in [('base_wallet', 'base'), ('solana_wallet', 'solana')]:
        assert sdk.prepare(cfg, spec(identifier, payload={'chain': chain, 'address': 'owner'}),
                           script_path=adapter(tmp_path, identifier+'.py'))['ok']
        observed = sdk.collect(cfg, identifier)['connector']['observation']
        assert observed['balances'][0]['amount'] == '1.234567'
        assert observed['balances'][0]['chain'] == chain
        assert observed['performance']['income_usd'] == '0.20'
        assert observed['performance']['realized_pnl_usd'] == '2.50'
        assert observed['positions'][0]['claimable_rewards_usd'] == '0.35'
        assert observed['usage'] == 'display_only'
        assert not cfg.paths.db.exists(), 'SDK observations must not become risk-gate NAV or ledger state'
    assert sdk.list_connections(cfg)['count'] == 2
    assert not (tmp_path / '.venv').exists()


def test_unpriced_unknown_and_large_integer_units_are_not_zero_or_float(tmp_path):
    raw = {'schema_version':1, 'as_of':100, 'balances':[
        {'chain':'solana','token':'mint','symbol':'TOKEN','raw_amount':'900719925474099312345678','decimals':9}]}
    normalized = normalize_observation(raw, connector_id='wallet', source={}, now=100)
    row = normalized['balances'][0]
    assert Decimal(row['amount']) == Decimal('900719925474099.312345678')
    assert row['value_usd'] is None and normalized['summary']['priced_balance_value_usd'] is None
    assert normalized['performance']['realized_pnl_usd'] is None
    assert normalized['coverage']['positions'] == 'unknown'


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), '-1', True, {}, 'unknown'])
def test_invalid_quantities_are_rejected(bad):
    with pytest.raises(ObservationError):
        normalize_observation({'schema_version':1,'as_of':100,'balances':[
            {'token':'USDC','amount':bad}]},connector_id='wallet',source={},now=100)


def test_apy_or_equity_does_not_invent_realized_income_and_lp_values_are_not_double_counted():
    result=normalize_observation({'schema_version':1,'as_of':100,'balances':[
        {'token':'USDC','amount':'5','price_usd':'1'}],
        'positions':[{'id':'lp','kind':'lp','value_usd':'5'}],
        'performance':{'apy':1000,'equity_change_usd':50}},connector_id='wallet',source={},now=100)
    assert result['summary']['priced_balance_value_usd']=='5'
    assert result['performance']['income_usd'] is None


def test_failed_refresh_keeps_last_good_observation_and_marks_it_stale(tmp_path, monkeypatch):
    cfg=config(tmp_path);script=adapter(tmp_path)
    sdk.prepare(cfg,spec(),script_path=script);old=sdk.collect(cfg,'base_wallet')['connector']['observation']
    monkeypatch.setattr(sdk,'run',lambda *args,**kw:'not JSON')
    with pytest.raises(sdk.SDKError):sdk.collect(cfg,'base_wallet')
    row=sdk.list_connections(cfg)['connectors'][0]
    assert row['stale'] is True and row['observation']['observation_id']==old['observation_id']


def test_changed_connection_target_never_reuses_another_owner_snapshot(tmp_path):
    cfg=config(tmp_path);script=adapter(tmp_path)
    sdk.prepare(cfg,spec(),script_path=script);sdk.collect(cfg,'base_wallet')
    sdk.prepare(cfg,spec(payload={'chain':'base','address':'different'}),script_path=script)
    assert sdk.list_connections(cfg)['connectors'][0]['observation'] is None


def test_pinned_dependency_install_is_shared_and_retry_safe(tmp_path, monkeypatch):
    cfg=config(tmp_path);calls=[]
    monkeypatch.setattr(sdk,'install_environment',lambda *args: calls.append(args[2]) or sys.executable)
    script=adapter(tmp_path)
    first=sdk.prepare(cfg,spec(requirements=['example-sdk==1.2.3']),script_path=script,approve=True)
    assert first['ok']
    assert sdk.prepare(cfg,spec(requirements=['example-sdk==1.2.3']),script_path=script,approve=True)['reused']
    assert sdk.prepare(cfg,spec('another',requirements=['example-sdk==1.2.3']),script_path=script,approve=True)['ok']
    assert len(calls)==1


@pytest.mark.parametrize('requirement', ['example-sdk','example-sdk>=1','git+https://example.com/x',
                                        '--target=/tmp/unsafe','name==1;touch /tmp/x'])
def test_dependency_requests_are_exact_version_registry_targets(requirement):
    with pytest.raises(sdk.SDKError):sdk.validate_requirements('python',[requirement])


def test_disabled_install_policy_is_reported_without_running_installer(tmp_path,monkeypatch):
    cfg=config(tmp_path)
    monkeypatch.setattr(sdk,'install_environment',lambda *args:pytest.fail('installation must not run'))
    row=sdk.prepare(cfg,spec(requirements=['example-sdk==1.2.3']),script_path=adapter(tmp_path))
    assert row['error']=='installation_required'
    assert sdk.list_connections(cfg)['connectors'][0]['phase']=='installation_required'


def test_collection_rejects_tampered_script_revision(tmp_path):
    cfg=config(tmp_path);sdk.prepare(cfg,spec(),script_path=adapter(tmp_path))
    manifest=json.loads((sdk.path_for(cfg,'base_wallet')/'manifest.json').read_text())
    Path(manifest['adapter']).write_text('print("tampered")')
    with pytest.raises(sdk.SDKError,match='revision_changed'):sdk.collect(cfg,'base_wallet')


def test_native_agent_tool_prepares_then_collects_without_trading_or_ui_install(tmp_path,monkeypatch):
    from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.types import ToolCall
    cfg=config(tmp_path);script=adapter(tmp_path)
    (script.parent.parent/'SKILL.md').write_text('---\nname: sdk_reader\ndescription: Read fixture account data\n---\n# SDK reader\n')
    deps=build_native_tool_deps(workspace_root=tmp_path,skill_roots=[tmp_path/'skills'],paths=cfg.paths)
    deps.config=cfg;deps.permission_mode='auto'
    registry=ToolRegistry();register_native_tools(registry,deps)
    descriptor=registry.find('sdk_account')
    assert descriptor and descriptor.auto_approve_when({'op':'prepare'})
    calls=[]
    monkeypatch.setattr(sdk,'install_environment',lambda *args:calls.append(args[2]) or sys.executable)
    prepared=descriptor.handler(ToolCall(name='sdk_account',arguments={'op':'prepare',**spec(requirements=['example-sdk==1.2.3'])}))
    assert not prepared.is_error,prepared.text()
    refreshed=descriptor.handler(ToolCall(name='sdk_account',arguments={'op':'refresh','connector_id':'base_wallet'}))
    assert not refreshed.is_error,refreshed.text()
    assert refreshed.content[0].data['connector']['observation']['performance']['income_usd']=='0.20'
    assert calls==[['example-sdk==1.2.3']]
    cfg.data['runtime']['allow_auto_install']=False
    assert not descriptor.auto_approve_when({'op':'prepare'})


def test_http_ui_contract_reads_cached_observations_and_only_refreshes_requested_source(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from nerya.api.routes_portfolio import routes
    from nerya.api.route_scopes import required_scope
    cfg=config(tmp_path);sdk.prepare(cfg,spec(),script_path=adapter(tmp_path));sdk.collect(cfg,'base_wallet')
    client=SimpleNamespace(config=cfg)
    read=next(handler for method,path,handler in routes() if path=='/portfolio/sdk_accounts')
    refresh=next(handler for method,path,handler in routes() if path=='/portfolio/sdk_accounts/refresh')
    monkeypatch.setattr(sdk,'install_environment',lambda *args:pytest.fail('UI must not install packages'))
    assert read(client,{})['connectors'][0]['observation']['balances'][0]['amount']=='1.234567'
    assert refresh(client,{'connector_id':'base_wallet'})['ok']
    assert required_scope('GET','/portfolio/sdk_accounts')=='read:runtime'
    assert required_scope('POST','/portfolio/sdk_accounts/refresh')=='read:runtime'


def test_approved_script_is_required_before_agent_can_prepare(tmp_path):
    from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.types import ToolCall
    cfg=config(tmp_path)
    deps=build_native_tool_deps(workspace_root=tmp_path,skill_roots=[tmp_path/'skills'],paths=cfg.paths);deps.config=cfg
    registry=ToolRegistry();register_native_tools(registry,deps)
    result=registry.find('sdk_account').handler(ToolCall(name='sdk_account',arguments={'op':'prepare',**spec()}))
    assert result.is_error and not (tmp_path/'runtime/sdk_accounts/base_wallet/manifest.json').exists()


def test_one_corrupt_source_does_not_hide_valid_connected_accounts(tmp_path):
    cfg=config(tmp_path);sdk.prepare(cfg,spec(),script_path=adapter(tmp_path));sdk.collect(cfg,'base_wallet')
    bad=sdk.path_for(cfg,'bad');bad.mkdir();(bad/'manifest.json').write_text('not-json')
    rows=sdk.list_connections(cfg)['connectors']
    assert len(rows)==2
    assert next(row for row in rows if row['connector_id']=='bad')['phase']=='error'


def test_invalid_adapter_owner_or_period_never_replaces_known_good_data(tmp_path,monkeypatch):
    cfg=config(tmp_path);sdk.prepare(cfg,spec(),script_path=adapter(tmp_path));good=sdk.collect(cfg,'base_wallet')['connector']['observation']
    monkeypatch.setattr(sdk,'run',lambda *args,**kw:json.dumps({'schema_version':1,'as_of':time.time(),
        'address':'different','balances':[]}))
    with pytest.raises(sdk.SDKError,match='owner_mismatch'):sdk.collect(cfg,'base_wallet')
    assert sdk.list_connections(cfg)['connectors'][0]['observation']['observation_id']==good['observation_id']
    with pytest.raises(ObservationError,match='period_required'):
        normalize_observation({'schema_version':1,'as_of':100,'balances':[],
                               'performance':{'income_usd':'1'}},connector_id='wallet',source={},now=100)


def test_install_exception_has_final_failure_state_instead_of_endless_preparing(tmp_path,monkeypatch):
    cfg=config(tmp_path)
    def failed(*args):raise OSError('sensitive-output-must-not-be-returned')
    monkeypatch.setattr(sdk,'install_environment',failed)
    result=sdk.prepare(cfg,spec(requirements=['example-sdk==1.2.3']),script_path=adapter(tmp_path),approve=True)
    assert not result['ok'] and result['error']=='OSError'
    assert sdk.list_connections(cfg)['connectors'][0]['phase']=='error'
    assert 'sensitive-output' not in json.dumps(result)


@pytest.mark.parametrize('payload', [{'api_key':'do-not-save'}, {'params':{'private_key':'do-not-save'}},
                                   {'authorization':'do-not-save'}])
def test_plaintext_credentials_cannot_be_persisted_in_connection_payload(tmp_path,payload):
    cfg=config(tmp_path)
    with pytest.raises(sdk.SDKError,match='credentials_must_use_env_refs'):
        sdk.prepare(cfg,spec(payload=payload),script_path=adapter(tmp_path))
    assert not (sdk.path_for(cfg,'base_wallet')/'manifest.json').exists()


@pytest.mark.skipif(not shutil.which('node'),reason='Node runtime is not installed')
def test_node_sdk_reader_uses_the_same_asset_schema(tmp_path):
    cfg=config(tmp_path);script=tmp_path/'reader.cjs'
    script.write_text('''let input="";
process.stdin.on("data",chunk=>input+=chunk);
process.stdin.on("end",()=>{const request=JSON.parse(input);console.log(JSON.stringify({schema_version:1,
as_of:Date.now()/1000,address:request.payload.address,balances:[{chain:"solana",token:"native",symbol:"SOL",amount:"2"}]}));});
''')
    result=sdk.prepare(cfg,spec('node_wallet',runtime='node',script='reader.cjs',payload={'address':'node_owner'}),script_path=script)
    assert result['ok']
    observed=sdk.collect(cfg,'node_wallet')['connector']['observation']
    assert observed['balances'][0]['amount']=='2'
    assert observed['summary']['priced_balance_value_usd'] is None


def test_signing_secret_is_not_forwarded_even_if_it_has_sdk_read_scope(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from nerya.security.secrets import SecretVault
    cfg=config(tmp_path)
    sdk.prepare(cfg,spec(env_refs={'PRIVATE_KEY':'vault://key'}),script_path=adapter(tmp_path))
    fake=SimpleNamespace(meta=lambda name:SimpleNamespace(kind='private_key'),
                         resolve=lambda *args,**kw:pytest.fail('signing secret must not be resolved'))
    monkeypatch.setattr(SecretVault,'open',lambda *args,**kw:fake)
    with pytest.raises(sdk.SDKError,match='signing_secret_not_allowed'):sdk.collect(cfg,'base_wallet')


def test_empty_observed_balance_is_zero_but_unknown_pnl_remains_unknown():
    result=normalize_observation({'schema_version':1,'as_of':100,'balances':[]},connector_id='wallet',source={},now=100)
    assert result['summary']['priced_balance_value_usd']=='0'
    assert result['performance']['realized_pnl_usd'] is None


@pytest.mark.parametrize('requirement',['sdk@1.x','sdk@latest','sdk@^1.2.3','sdk@1.2'])
def test_npm_versions_cannot_float(requirement):
    with pytest.raises(sdk.SDKError):sdk.validate_requirements('node',[requirement])


def test_agent_compaction_keeps_sdk_income_and_exact_token_quantity(tmp_path):
    from nerya.llm.tool_compaction import compact_tool_result
    cfg=config(tmp_path);sdk.prepare(cfg,spec(),script_path=adapter(tmp_path))
    raw=sdk.collect(cfg,'base_wallet')
    raw['connector']['observation']['balances']*=30
    compact=compact_tool_result('sdk_account',raw,size_threshold=0)
    observed=compact.kept['connector']['observation']
    assert observed['performance']['income_usd']=='0.20'
    assert observed['balances_sample'][0]['amount']=='1.234567'
    assert observed['positions_sample'][0]['claimable_rewards_usd']=='0.35'
    assert compact.compacted_bytes<compact.original_bytes


def test_structured_sdk_auth_failure_preserves_old_observation(tmp_path,monkeypatch):
    cfg=config(tmp_path);sdk.prepare(cfg,spec(),script_path=adapter(tmp_path));sdk.collect(cfg,'base_wallet')
    monkeypatch.setattr(sdk,'run',lambda *args,**kw:json.dumps({'schema_version':1,'error':{'code':'auth_required','token':'must-not-leak'}}))
    with pytest.raises(sdk.SDKError,match='sdk_provider_auth_required'):sdk.collect(cfg,'base_wallet')
    row=sdk.list_connections(cfg)['connectors'][0]
    assert row['stale'] and row['observation'] is not None
    assert 'must-not-leak' not in json.dumps(row)
