import subprocess
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke


def test_discovery_and_other_venues_do_not_load_prediction_adapter(tmp_path):
    subprocess.run([sys.executable, '-c', '''
import sys
from pathlib import Path
from nerya.connectors.provider_spec import get_registry
registry = get_registry(Path(sys.argv[1]))
assert registry.find('pm').module == 'prediction_market'
registry.build({'venue': 'mock'})
assert 'nerya.connectors.polymarket' not in sys.modules
assert 'polymarket' not in sys.modules
sys.modules['polymarket'] = None  # Even an attempted private SDK import must fail.
connector = registry.build({'venue':'pm','live':False})
assert 'nerya.connectors.polymarket' in sys.modules
from types import SimpleNamespace
connector.transport = SimpleNamespace(request=lambda *a, **kw: (200, {'tick_size': '0.01', 'timestamp': '1700000000000'}))
assert connector.get_order_book('pm:' + '1' * 25)['size_step'] == '0.01'
''', str(tmp_path)], check=True)


def test_disabled_blocks_paper_quote_and_order_without_blocking_other_venues(tmp_path):
    from nerya.connectors.module_policy import require_order_modules
    from nerya.connectors.provider_spec import get_registry
    from nerya.skills.builtin.prediction_market.scripts.inspect_prediction import run
    registry = get_registry(tmp_path)
    connector = registry.build({'venue': 'pm', 'live': False})
    yaml_io.dump(tmp_path/'nerya.yml', {'modules': {'prediction_market':'disabled'}})
    with pytest.raises(TradingError, match='module_disabled'):
        connector.get_ticker('pm:event#No')
    with pytest.raises(TradingError, match='module_disabled'):
        run(SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(root=tmp_path))), provider='pm')
    for venue, market in [('mock','pm:123'), ('polymarket','123')]:
        with pytest.raises(TradingError, match='module_disabled'):
            require_order_modules(tmp_path, SimpleNamespace(venue=venue), market)
    require_order_modules(tmp_path, SimpleNamespace(venue='mock', kind='cex'), 'MOCK:BTCUSD')
    with pytest.raises(TradingError, match='provider order rules unavailable'):
        require_order_modules(tmp_path, SimpleNamespace(venue='mock', kind='prediction_market'), 'MOCK:BTCUSD')


def test_public_book_precision_without_signer_matches_pinned_sdk():
    from nerya.connectors.polymarket import public_share_step
    from nerya.connectors.polymarket_client import share_step
    pytest.importorskip('polymarket')
    for tick in ('0.1', '0.01', '0.005', '0.0025', '0.001', '0.0001'):
        assert Decimal(public_share_step(tick)) == share_step(tick)
    assert public_share_step('0.003') is None


def test_existing_adapter_permission_discovers_and_reads_module(tmp_path):
    from nerya.core.paths import WorkspacePaths
    from nerya.skills.registry import SkillRegistry
    from nerya.tools.native.skill import SkillIndex, skill_view_handler
    from nerya.tools.types import ToolCall

    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(paths.skills_enabled, {'enabled': ['adapter']})
    index = SkillIndex([], registry_provider=lambda: SkillRegistry.load_builtin(paths))
    match = index.search('预测市场策略')[0]
    assert match['skill_id'] == 'adapter.prediction_market'
    result = skill_view_handler(ToolCall(name='skill_view', arguments={
        'skill_id': match['skill_id'], 'file': 'references/prediction-strategies.md',
    }), skill_index=index)
    assert not result.is_error
    yaml_io.dump(paths.skills_enabled, {'enabled': ['markets']})
    assert index.get(match['skill_id'], refresh=True) is None
