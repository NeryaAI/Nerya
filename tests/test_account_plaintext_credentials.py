from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_accounts
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.connectors.registry import _resolve_cex_creds
from nerya.security.secrets import SecretVault

pytestmark = pytest.mark.smoke


def test_accounts_upsert_converts_plaintext_credentials_to_vault(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_accounts.routes()}
    handler = route_map[("POST", "/accounts/upsert")]

    res = handler(
        client,
        {
            "id": "bn_live",
            "venue": "binance",
            "kind": "cex",
            "mode": "live",
            "live_trading_enabled": True,
            "credentials": {
                "api_key": "plain-key",
                "api_secret": "plain-secret",
            },
            "permissions": {
                "read_balances": True,
                "place_order": True,
                "cancel_order": True,
            },
            "operator": "dashboard",
        },
    )

    assert res["ok"] is True
    refs = res["account"]["profile"]["credentials"]
    assert refs["api_key"].startswith("vault://")
    assert refs["api_secret"].startswith("vault://")
    saved = yaml_io.load(tmp_path / "accounts" / "accounts.yml", default={})
    row = saved["accounts"][0]
    assert row["credentials"] == refs
    assert "plain-key" not in str(saved)
    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    assert vault.resolve(refs["api_key"].removeprefix("vault://"), required_scope="exchange") == "plain-key"


def test_live_connector_drops_plaintext_credentials(tmp_path):
    live = _resolve_cex_creds(
        {
            "venue": "bybit",
            "live_trading_enabled": True,
            "api_key": "plain-key",
            "api_secret": "plain-secret",
        },
        tmp_path,
        None,
    )
    paper_mock = _resolve_cex_creds(
        {
            "venue": "mock",
            "live": False,
            "api_key": "plain-key",
            "api_secret": "plain-secret",
        },
        tmp_path,
        None,
    )

    assert live.api_key == ""
    assert live.api_secret == ""
    assert paper_mock.api_key == "plain-key"
    assert paper_mock.api_secret == "plain-secret"
