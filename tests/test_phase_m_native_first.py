"""Phase M-2 — credential-plumbing tests for the agent's market_data path.

These cover three things:

1. ``_resolve_account_for_venue`` returns a bare cfg when ``config_like``
   is None or has no matching account row, and threads through cfg /
   workspace / vault_passphrase when a row is present.
2. ``_public_connector`` builds the right connector class for the venue
   (yahoo / tushare / polygon) regardless of credential availability.
3. The data-source connectors' ``_env_fallback_api_key`` helper picks
   up environment-supplied tokens so operators can enable a venue
   without registering an account row + vault entry.
"""

from __future__ import annotations

import os
from copy import deepcopy

import pytest

# Pre-import to break a benign circular import seen at module-load time.
import nerya.agent  # noqa: F401
from nerya.connectors.data_sources import (
    DataSourceCredentials,
    GlassnodeConnector,
    PolygonConnector,
    TushareConnector,
    _env_fallback_api_key,
)
from nerya.connectors.yahoo import YahooFinanceConnector
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.connectors import (
    connector_view_handler,
    market_data_handler,
    _public_connector,
    _resolve_account_for_venue,
    _vault_passphrase_from_env,
)
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(cfg.paths.accounts_file, {"accounts": []})
    return cfg


def _config_with_tushare_account(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "tushare_main",
                    "exchange": "tushare",
                    "venue": "tushare",
                    "kind": "data_source",
                    "mode": "paper",
                    "status": "active",
                    "credentials": {
                        "api_key": "vault://tushare_token_test",
                    },
                }
            ]
        },
    )
    return cfg


# ---------------------------------------------------------------------------
# _resolve_account_for_venue
# ---------------------------------------------------------------------------


def test_phase_m_resolve_account_returns_bare_when_config_is_none() -> None:
    cfg, ws, pp = _resolve_account_for_venue("tushare", config_like=None)
    assert cfg == {"venue": "tushare", "live": False}
    assert ws is None
    assert pp is None


def test_phase_m_resolve_account_returns_bare_when_no_matching_row(tmp_path) -> None:
    cfg_obj = _bare_config(tmp_path)
    cfg, ws, pp = _resolve_account_for_venue("tushare", config_like=cfg_obj)
    assert cfg == {"venue": "tushare", "live": False}
    assert ws is None
    assert pp is None


def test_phase_m_resolve_account_threads_credentials_when_row_exists(
    tmp_path, monkeypatch
) -> None:
    cfg_obj = _config_with_tushare_account(tmp_path)
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "test_pp")

    cfg, ws, pp = _resolve_account_for_venue("tushare", config_like=cfg_obj)

    assert cfg.get("venue") == "tushare"
    assert cfg.get("credentials", {}).get("api_key") == "vault://tushare_token_test"
    assert ws == cfg_obj.paths.root
    assert pp == "test_pp"


def test_phase_m_resolve_account_case_insensitive_venue_match(tmp_path) -> None:
    cfg_obj = _config_with_tushare_account(tmp_path)
    cfg, ws, _pp = _resolve_account_for_venue("TUSHARE", config_like=cfg_obj)
    assert cfg.get("venue") == "tushare"
    assert ws == cfg_obj.paths.root


def test_phase_m_resolve_account_returns_bare_on_loading_error(
    tmp_path, monkeypatch
) -> None:
    """If accounts.yml is corrupted, fall through gracefully."""

    cfg_obj = _bare_config(tmp_path)
    cfg_obj.paths.accounts_file.write_text("::: not yaml :::", encoding="utf-8")

    cfg, ws, pp = _resolve_account_for_venue("tushare", config_like=cfg_obj)

    assert cfg == {"venue": "tushare", "live": False}
    assert ws is None
    assert pp is None


# ---------------------------------------------------------------------------
# _public_connector — preserves existing keyless paths, builds for new venues
# ---------------------------------------------------------------------------


def test_phase_m_public_connector_yahoo_unchanged() -> None:
    """Yahoo is keyless; no behavior change after Phase M-2."""
    conn = _public_connector("yahoo", config_like=None)
    assert isinstance(conn, YahooFinanceConnector)


def test_phase_m_public_connector_builds_tushare_even_without_creds() -> None:
    """The connector is built (instantiated). It will raise on actual
    .get_klines() if no token is around — that's the connector's
    responsibility to surface, not _public_connector's. Build path
    must succeed so the caller can decide whether to call or not."""
    conn = _public_connector("tushare", config_like=None)
    assert isinstance(conn, TushareConnector)


def test_phase_m_public_connector_builds_polygon_io_even_without_creds() -> None:
    """Note: the venue id is ``polygon_io`` — the bare ``polygon`` venue
    id is owned by the EVM Polygon chain (a DEX-style native), which is
    a deliberate naming convention that pre-dates Phase M."""
    conn = _public_connector("polygon_io", config_like=None)
    assert isinstance(conn, PolygonConnector)


def test_phase_m_public_connector_builds_glassnode_even_without_creds() -> None:
    conn = _public_connector("glassnode", config_like=None)
    assert isinstance(conn, GlassnodeConnector)


def test_market_data_short_circuits_missing_data_source_credentials(tmp_path) -> None:
    cfg_obj = _bare_config(tmp_path)

    result = market_data_handler(
        ToolCall(
            name="market_data",
            arguments={
                "action": "get_ticker",
                "venue": "glassnode",
                "market": "BTC:supply/lth_sum",
            },
        ),
        config_like=cfg_obj,
    )

    data = result.content[0].data
    assert result.is_error is False
    assert data["error"] == "credential_missing"
    assert data["should_retry"] is False
    assert data["credential_status"]["status"] == "missing"
    assert "api_key" in data["credential_status"]["required_fields"]


def test_connector_view_reports_missing_data_source_credentials(tmp_path) -> None:
    cfg_obj = _bare_config(tmp_path)

    result = connector_view_handler(
        ToolCall(name="connector_view", arguments={"id": "glassnode"}),
        config_like=cfg_obj,
    )

    data = result.content[0].data
    assert data["found"] is True
    assert data["credential_status"]["required"] is True
    assert data["credential_status"]["status"] == "missing"
    assert data["credential_status"]["should_retry"] is False


def test_phase_m_public_connector_polygon_resolves_to_evm_chain_not_data_source() -> None:
    """Document the existing convention so future readers don't think
    Phase M-2 broke the data-source path. ``polygon`` -> EVM Polygon
    chain native is intended; data-source goes via ``polygon_io``."""
    from nerya.connectors.evm_native import EVMNative

    conn = _public_connector("polygon", config_like=None)
    assert isinstance(conn, EVMNative)


def test_phase_m_public_connector_unknown_venue_returns_none() -> None:
    assert _public_connector("not_a_real_venue_anywhere", config_like=None) is None


def test_phase_m_public_connector_mock_venue_returns_mock_exchange() -> None:
    from nerya.connectors.mock_exchange import MockExchange

    conn = _public_connector("mock", config_like=None)
    assert isinstance(conn, MockExchange)


# ---------------------------------------------------------------------------
# _env_fallback_api_key — connector-level operator UX
# ---------------------------------------------------------------------------


def test_phase_m_env_fallback_returns_first_non_empty(monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "primary")
    monkeypatch.setenv("TUSHARE_API_KEY", "legacy")
    assert (
        _env_fallback_api_key(("TUSHARE_TOKEN", "TUSHARE_API_KEY")) == "primary"
    )


def test_phase_m_env_fallback_skips_empty(monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "")
    monkeypatch.setenv("TUSHARE_API_KEY", "real_value")
    assert (
        _env_fallback_api_key(("TUSHARE_TOKEN", "TUSHARE_API_KEY")) == "real_value"
    )


def test_phase_m_env_fallback_returns_empty_when_none_set(monkeypatch) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_API_KEY", raising=False)
    assert _env_fallback_api_key(("TUSHARE_TOKEN", "TUSHARE_API_KEY")) == ""


def test_phase_m_tushare_uses_env_var_when_credentials_empty(monkeypatch) -> None:
    """Mark Tushare's _client() as smoke-passable via env var. We DON'T
    actually call upstream — we just verify ``_client()`` would succeed
    past the credential gate. We stub ``import tushare`` so the test
    doesn't need the SDK installed."""

    import sys
    import types

    stub_ts = types.ModuleType("tushare")

    set_token_calls: list[str] = []

    def _set_token(t: str) -> None:
        set_token_calls.append(t)

    class _ProClient:
        pass

    def _pro_api():
        return _ProClient()

    stub_ts.set_token = _set_token  # type: ignore[attr-defined]
    stub_ts.pro_api = _pro_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tushare", stub_ts)

    monkeypatch.setenv("TUSHARE_TOKEN", "env_var_token_xyz")

    conn = TushareConnector(credentials=DataSourceCredentials())
    pro = conn._client()  # type: ignore[attr-defined]

    assert isinstance(pro, _ProClient)
    assert set_token_calls == ["env_var_token_xyz"]


def test_phase_m_tushare_explicit_creds_win_over_env(monkeypatch) -> None:
    import sys
    import types

    stub_ts = types.ModuleType("tushare")
    set_token_calls: list[str] = []
    stub_ts.set_token = set_token_calls.append  # type: ignore[attr-defined]
    stub_ts.pro_api = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tushare", stub_ts)

    monkeypatch.setenv("TUSHARE_TOKEN", "env_token")

    conn = TushareConnector(
        credentials=DataSourceCredentials(api_key="explicit_token")
    )
    conn._client()  # type: ignore[attr-defined]

    assert set_token_calls == ["explicit_token"]


def test_phase_m_tushare_raises_when_neither_creds_nor_env(monkeypatch) -> None:
    import sys
    import types

    stub_ts = types.ModuleType("tushare")
    stub_ts.set_token = lambda _t: None  # type: ignore[attr-defined]
    stub_ts.pro_api = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tushare", stub_ts)

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_API_KEY", raising=False)
    monkeypatch.delenv("NERYA_TUSHARE_TOKEN", raising=False)

    conn = TushareConnector(credentials=DataSourceCredentials())
    with pytest.raises(RuntimeError, match=r"Tushare requires an API token"):
        conn._client()  # type: ignore[attr-defined]


def test_phase_m_polygon_uses_env_var_when_credentials_empty(monkeypatch) -> None:
    import sys
    import types

    stub_polygon = types.ModuleType("polygon")

    captured: dict[str, str] = {}

    class _RESTClient:
        def __init__(self, api_key: str) -> None:
            captured["api_key"] = api_key

    stub_polygon.RESTClient = _RESTClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "polygon", stub_polygon)

    monkeypatch.setenv("POLYGON_API_KEY", "env_polygon_key_zzz")

    conn = PolygonConnector(credentials=DataSourceCredentials())
    client = conn._client()  # type: ignore[attr-defined]

    assert isinstance(client, _RESTClient)
    assert captured["api_key"] == "env_polygon_key_zzz"


def test_phase_m_polygon_raises_when_neither_creds_nor_env(monkeypatch) -> None:
    import sys
    import types

    stub_polygon = types.ModuleType("polygon")

    class _RESTClient:
        def __init__(self, api_key: str) -> None:
            pass

    stub_polygon.RESTClient = _RESTClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "polygon", stub_polygon)

    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("POLYGONIO_API_KEY", raising=False)
    monkeypatch.delenv("NERYA_POLYGON_API_KEY", raising=False)

    conn = PolygonConnector(credentials=DataSourceCredentials())
    with pytest.raises(RuntimeError, match=r"Polygon\.io requires an API key"):
        conn._client()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _vault_passphrase_from_env — small env-reading helper
# ---------------------------------------------------------------------------


def test_phase_m_vault_passphrase_returns_env_value(monkeypatch) -> None:
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "my_pp")
    assert _vault_passphrase_from_env() == "my_pp"


def test_phase_m_vault_passphrase_returns_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("NERYA_VAULT_PASSPHRASE", raising=False)
    assert _vault_passphrase_from_env() is None


def test_phase_m_vault_passphrase_returns_none_when_empty(monkeypatch) -> None:
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "")
    assert _vault_passphrase_from_env() is None
