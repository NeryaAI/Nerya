from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_exchanges
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.accounts import account_list_handler, account_upsert_handler
from nerya.tools.types import ToolCall
from nerya.trading import accounts as accounts_mod


pytestmark = pytest.mark.smoke


def _config(tmp_path):
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _json(result):
    assert result.content
    return result.content[0].data


def test_account_upsert_creates_paper_ccxt_venue_through_registry(tmp_path):
    cfg = _config(tmp_path)
    result = account_upsert_handler(
        ToolCall(
            name="account_upsert",
            arguments={
                "id": "kraken_paper",
                "venue": "ccxt:kraken",
                "mode": "paper",
            },
        ),
        config_like=cfg,
    )

    assert not result.is_error
    data = _json(result)
    assert data["ok"] is True
    assert data["account"]["id"] == "kraken_paper"
    assert data["account"]["venue"] == "kraken"
    assert data["completion_signal"] == {
        "kind": "account_setup",
        "finalizable": True,
        "safety": "paper_only",
    }
    assert "write the final answer" in data["next"]
    assert "do not keep probing market data" in data["next"]

    profile = accounts_mod.get_account_profile(cfg.paths, "kraken_paper")
    assert profile.mode == "paper"
    assert profile.venue == "kraken"
    assert profile.raw["provider_config"]["ccxt_id"] == "kraken"

    listed = account_list_handler(
        ToolCall(name="account_list", arguments={"venue": "kraken"}),
        config_like=cfg,
    )
    assert _json(listed)["count"] == 1


def test_account_upsert_refuses_plaintext_credentials(tmp_path):
    cfg = _config(tmp_path)
    result = account_upsert_handler(
        ToolCall(
            name="account_upsert",
            arguments={
                "id": "okx_paper",
                "venue": "okx",
                "mode": "paper",
                "credentials": {"api_key": "plain-key"},
            },
        ),
        config_like=cfg,
    )

    assert result.is_error
    assert "vault:// ref" in result.text()


def test_exchange_credential_schema_keeps_legacy_schema_fields_shape(tmp_path):
    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route = {
        (method, path): handler for method, path, handler in routes_exchanges.routes()
    }[("POST", "/exchanges/credential_schema")]

    out = route(client, {"venue": "okx"})
    names = {field["name"] for field in out["schema"]["fields"]}

    assert out["credential_fields"] == out["schema"]["fields"]
    assert {"api_key", "api_secret", "api_passphrase"}.issubset(names)


def test_account_profile_connector_account_modes_are_explicit():
    canary = accounts_mod.AccountProfile(
        id="canary_main",
        mode="canary",
        venue="bybit",
        kind="cex",
        provider_spec="bybit",
        base_currency="USDT",
        subaccount="",
        status="active",
        live_trading_enabled=True,
        initial_balance_usd=0.0,
        permissions=accounts_mod.AccountPermissions(place_order=True),
    )
    shadow = accounts_mod.AccountProfile(
        id="shadow_main",
        mode="shadow",
        venue="bybit",
        kind="cex",
        provider_spec="bybit",
        base_currency="USDT",
        subaccount="",
        status="active",
        live_trading_enabled=False,
        initial_balance_usd=0.0,
        permissions=accounts_mod.AccountPermissions(read_balances=True),
    )

    assert canary.to_connector_account().connector_cfg()["live"] is True
    assert shadow.to_connector_account().connector_cfg()["live"] is False
    assert shadow.to_connector_account(live=True).connector_cfg()["live"] is True
