from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.gateway import FinancialGateway


def test_gateway_rereads_saved_config_and_honors_later_live_disable(tmp_path):
    paths=WorkspacePaths(tmp_path)
    data=deepcopy(DEFAULT_CONFIG)
    data['financial']['enabled']=True
    data['runtime']['live_trading_enabled']=True
    yaml_io.dump(paths.config,data)
    gateway=FinancialGateway(Config(paths,data))
    context=FinancialContext('operator',frozenset({'api:all'}))
    action={'request':{},'quote':{}}
    gateway._fence(context,action)
    changed=deepcopy(data)
    changed['runtime']['live_trading_enabled']=False
    yaml_io.dump(paths.config,changed)
    # In-memory config remains enabled; authoritative disk change must stop it.
    assert gateway.config.live_trading_enabled()
    with pytest.raises(FinancialError,match='live_trading_disabled'):
        gateway._fence(context,action)
