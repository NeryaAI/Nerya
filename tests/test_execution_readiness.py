from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.financial.readiness import execution_readiness
from nerya.api.routes_financial import routes

pytestmark = pytest.mark.smoke


def test_empty_workspace_never_looks_live_ready_or_leaks_credentials(tmp_path, monkeypatch):
    def denied(*a,**kw):
        raise AssertionError('network_or_secret_access_forbidden')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr('nerya.security.secrets.SecretVault.open',denied)
    cfg=Config(WorkspacePaths(tmp_path),deepcopy(DEFAULT_CONFIG))
    result=execution_readiness(cfg,version_provider=lambda name:None)
    assert result['network_probed'] is False
    assert result['inventory_scope'] == 'builtin_execution_families'
    assert result['workspace_components_included'] is False
    assert all(row['state']!='ready_for_preflight' and row['live_verified'] is False for row in result['families'])
    assert 'native_orca_lp' in result['unsupported']


def test_readiness_discovery_resolves_workspace_contract_without_building_transport(tmp_path, monkeypatch):
    from nerya.tools.native.financial import register_financial_tools
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall

    def denied(*a, **kw):
        raise AssertionError('network_or_secret_access_forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr('nerya.security.secrets.SecretVault.open', denied)
    directory = tmp_path / 'plugins' / 'example'
    directory.mkdir(parents=True)
    (directory / 'trading.py').write_text('''
from nerya.trading.components import TradingComponentSpec
def build(config, request):
    raise AssertionError('discovery_must_not_construct_transport')
TRADING_COMPONENTS = (TradingComponentSpec(
    'funds', '1', ('wallet_transfer',), ('transfer',), build),)
''')
    data = deepcopy(DEFAULT_CONFIG)
    data['runtime'].update(financial_actor_id='operator', financial_actor_scopes=['read:funds'])
    cfg = Config(WorkspacePaths(tmp_path), data)
    readiness = execution_readiness(cfg, version_provider=lambda _: None)
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    discovery = readiness['workspace_component_discovery']
    result = registry.get(discovery['tool']).handler(ToolCall(
        id='discover', name=discovery['tool'], arguments=discovery['arguments']))
    assert not result.is_error
    component = next(row for row in tool_json_data(result)['components']
                     if row['id'] == 'user:example:funds')
    assert component['actions'] == ['wallet_transfer']
    assert component['parameters_schema']['additionalProperties'] is False
    assert component['revision'] and component['live_verified'] is False


def test_jupiter_requires_rpc_signer_api_key_and_dependencies(tmp_path):
    data=deepcopy(DEFAULT_CONFIG)
    data['wallet']={'providers':{'sol':{'provider':'self_custody','config':{
        'signer_ref':'vault://synthetic-signer','jupiter_api_key_ref':'vault://synthetic-jupiter',
        'rpc_urls':{'solana':'https://rpc.invalid'}}}}}
    cfg=Config(WorkspacePaths(tmp_path),data)
    result=execution_readiness(cfg,version_provider=lambda name:'0.12.0' if name=='polymarket-client' else '1')
    row=next(row for row in result['families'] if row['id']=='jupiter_v2')
    assert row['resources']==['sol'] and row['state']=='runtime_disabled'
    assert 'vault://' not in json.dumps(result) and 'rpc.invalid' not in json.dumps(result)
    del data['wallet']['providers']['sol']['config']['jupiter_api_key_ref']
    row=next(row for row in execution_readiness(cfg,version_provider=lambda _: '1')['families'] if row['id']=='jupiter_v2')
    assert row['state']=='configuration_required'


def test_readiness_route_requires_owned_read_funds_scope(tmp_path):
    handler=next(handler for method,path,handler in routes() if method=='GET' and path=='/financial/readiness')
    client=SimpleNamespace(config=Config(WorkspacePaths(tmp_path),deepcopy(DEFAULT_CONFIG)),auth_actor_id='operator',auth_scopes=())
    assert handler(client,{})['_status']==403
    client.auth_scopes=('read:funds',)
    assert handler(client,{})['ok'] is True


def test_paper_prediction_credentials_do_not_count_as_live_config(tmp_path):
    from nerya.core import yaml_io
    cfg=Config(WorkspacePaths(tmp_path),deepcopy(DEFAULT_CONFIG))
    yaml_io.dump(cfg.paths.accounts_file,{'accounts':[{'id':'pm','kind':'prediction_market','venue':'polymarket',
        'mode':'paper','status':'active','live_trading_enabled':False,'credentials':{
        field:'vault://test-'+field for field in ('api_key','api_secret','api_passphrase','private_key')}}]})
    row=next(row for row in execution_readiness(cfg,version_provider=lambda _:'0.12.0')['families'] if row['id']=='polymarket')
    assert row['resources']==[] and row['state']=='configuration_required'
