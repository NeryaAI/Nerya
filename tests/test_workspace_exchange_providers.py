
import pytest

from nerya.connectors.provider_spec import get_registry, reset_registry
from nerya.connectors.registry import ConnectorRegistry, build_connector
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke


def provider(root, label="first", *, aliases="('newvenue',)", native=False):
    folder = root / "providers" / "venue"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "helper.py").write_text("LABEL = " + repr(label) + "\n")
    (folder / "provider.py").write_text(f'''
from .helper import LABEL
from nerya.connectors.provider_spec import ExchangeProviderSpec
from nerya.connectors.ccxt_adapter import CcxtConnector
def factory(config, **kwargs):
    return {"object()" if native else "CcxtConnector(exchange_id='deribit')"}
SPEC=ExchangeProviderSpec('attempted_builtin_id',LABEL,'cex',runtime='python_ccxt',aliases={aliases},factory=factory)
''')
    return folder


def test_workspace_providers_never_leak_to_another_workspace(tmp_path):
    reset_registry()
    one, two = tmp_path / "one", tmp_path / "two"
    provider(one, "first"); provider(two, "other")
    assert get_registry(one).find("newvenue").label == "first"
    assert get_registry(two).find("newvenue").label == "other"
    assert get_registry().find("newvenue") is None
    assert get_registry(one).find("newvenue").label == "first"


def test_helper_edit_invalidates_connector_cache(tmp_path):
    path = provider(tmp_path)
    registry = ConnectorRegistry(tmp_path)
    one = registry.get("account", {"venue": "newvenue"})
    revision = get_registry(tmp_path).find("newvenue").revision
    (path / "helper.py").write_text("LABEL = 'second'\n")
    two = registry.get("account", {"venue": "newvenue"})
    assert one is not two
    assert get_registry(tmp_path).find("newvenue").label == "second"
    assert get_registry(tmp_path).find("newvenue").revision != revision


def test_plugin_cannot_shadow_builtin_alias(tmp_path):
    provider(tmp_path, aliases="('bybit',)")
    registry = get_registry(tmp_path)
    assert registry.find("bybit").id == "bybit"
    assert registry.find("user:venue") is None
    assert registry.errors["venue"] == "provider_alias_collision"


def test_native_cex_implementation_is_rejected(tmp_path):
    provider(tmp_path, native=True)
    with pytest.raises(TradingError, match="requires_ccxt"):
        build_connector({"venue": "newvenue"}, workspace=tmp_path)


def test_broken_provider_blocks_new_builds(tmp_path):
    path = provider(tmp_path)
    assert get_registry(tmp_path).find("newvenue")
    (path / "provider.py").write_text("bad python !\n")
    assert get_registry(tmp_path).find("newvenue") is None
    with pytest.raises(TradingError, match="unknown venue"):
        build_connector({"venue": "newvenue"}, workspace=tmp_path)


def test_old_provider_can_query_after_removal_without_becoming_new_default(tmp_path):
    path=provider(tmp_path)
    binding=get_registry(tmp_path).find("newvenue").binding()
    (path/"provider.py").unlink()
    assert get_registry(tmp_path).find("newvenue") is None
    old=build_connector({"venue":"newvenue"},workspace=tmp_path,provider_binding=binding)
    assert old.exchange_id=="deribit"
    with pytest.raises(TradingError,match="unknown venue"):
        build_connector({"venue":"newvenue"},workspace=tmp_path)


def test_historical_retrieval_hotloads_real_workspace_provider_and_isolates_it(tmp_path, monkeypatch):
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.data import candles

    folder = tmp_path / 'providers' / 'research_feed'
    folder.mkdir(parents=True)
    (folder / 'provider.py').write_text("""
from nerya.connectors.provider_spec import ExchangeProviderSpec
class PublicHistory:
    venue = 'research_feed'
    connector_id = 'workspace:research_feed'
    def get_klines(self, market, *, interval, limit, since=None, end=None):
        assert market == 'RESEARCH_FEED:ASSET/QUOTE'
        assert interval == '1h'
        assert since == 1700000000000 and end == 1700003600000
        return [[1700000000000, 1, 2, .5, 1.5, 10]]
def factory(config, **kwargs):
    assert config['live'] is False
    return PublicHistory()
SPEC = ExchangeProviderSpec('research_feed', 'Public history', 'chain',
    aliases=('research_feed',), factory=factory,
    supports={'klines': True, 'place_order': False})
""")
    monkeypatch.setattr(candles, '_fetch_public_rest_klines', lambda *a, **kw: [])
    monkeypatch.setattr(candles, '_fetch_ccxt_klines', lambda *a, **kw: [])
    config = Config(paths=WorkspacePaths(root=tmp_path))
    rows = candles.fetch_candles('RESEARCH_FEED:ASSET/QUOTE', count=1, interval='1h',
        start=1700000000, end=1700003600, config_like=config, allow_mock=False)
    assert rows[0]['close'] == 1.5
    assert rows[0]['_envelope']['connector_id'] == 'workspace:research_feed'
    assert get_registry().find('research_feed') is None
    assert get_registry(tmp_path / 'other').find('research_feed') is None
