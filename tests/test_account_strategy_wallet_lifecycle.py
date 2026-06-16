"""Coverage for the account/wallet/strategy lifecycle optimisations.

Five behaviours land here:

1. ``/accounts/equity_curve`` returns the real NAV history sourced
   from ``account_snapshots`` (so the dashboard can render a fund
   curve straight from the same data the snapshot loop already
   persists for paper / live / wallet accounts).
2. ``/wallet/configure`` with ``auto_create_account=true`` writes
   both ``wallet.providers.<id>`` and a matching ``kind=chain``
   account row.
3. ``strategy_crud.bind_account`` / ``create`` emit a soft
   ``warning`` payload (not an error) when the target account is
   already used by a non-archived strategy.
4. ``accounts.upsert_account`` (through the HTTP route) refuses a
   second CEX account with the same venue + primary credential ref
   unless ``force=true`` is passed.
5. ``account_refresh.account_refresh_interval_for_profile`` returns
   the live cadence (60s default) for live/canary/shadow modes and
   the paper cadence (300s default) for paper accounts.

All tests run fully offline: no real venue, no real RPC, no real
wallet provider. Vault references are pre-seeded so the upsert
flow does not trip the plaintext refusal.
"""

from __future__ import annotations

import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_account_intake, routes_accounts, routes_wallet
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import TradingError
from nerya.core.paths import WorkspacePaths
from nerya.security.secrets import SecretVault
from nerya.trading import accounts as accounts_mod
from nerya.trading import strategy_crud
from nerya.trading.account_refresh import (
    DEFAULT_ACCOUNT_REFRESH_INTERVAL_S,
    DEFAULT_LIVE_REFRESH_INTERVAL_S,
    account_refresh_interval_for_profile,
    next_refresh_ts_for_profile,
)
from nerya.trading.account_snapshots import capture_snapshot, equity_curve


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "venue": "mock",
                    "exchange": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                }
            ]
        },
    )
    return cfg


def _client(cfg: Config) -> SimpleNamespace:
    return SimpleNamespace(config=cfg)


def _route(routes, method: str, path: str):
    for m, p, handler in routes:
        if m == method and p == path:
            return handler
    raise AssertionError(f"route not registered: {method} {path}")


# ---------------------------------------------------------------------------
# 1. equity curve endpoint
# ---------------------------------------------------------------------------


def test_account_equity_curve_returns_persisted_nav_points(tmp_path):
    cfg = _config(tmp_path)
    snap_a = capture_snapshot(cfg, "paper_main", persist=True)
    # Bump the wall-clock by one second so the next snapshot is a
    # distinct row even when the test machine is fast.
    time.sleep(0.01)
    snap_b = capture_snapshot(cfg, "paper_main", persist=True)
    points = equity_curve(cfg.paths, "paper_main", limit=10)
    assert len(points) >= 2
    # NAV must be present and finite on every point.
    for pt in points:
        assert isinstance(pt["nav_usd"], float)
        assert pt["health"] == "ok"
    assert points[-1]["nav_usd"] == pytest.approx(snap_b.nav_usd)
    assert points[0]["nav_usd"] == pytest.approx(snap_a.nav_usd)


def test_accounts_equity_curve_route_payload(tmp_path):
    cfg = _config(tmp_path)
    capture_snapshot(cfg, "paper_main", persist=True)
    handler = _route(routes_accounts.routes(), "POST", "/accounts/equity_curve")
    out = handler(_client(cfg), {"account_id": "paper_main", "limit": 50})
    assert out["ok"] is True
    assert out["account_id"] == "paper_main"
    assert out["base_currency"] == "USDT"
    assert out["count"] >= 1
    assert out["points"][0]["health"] == "ok"


def test_accounts_equity_curve_rejects_unknown_account(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_accounts.routes(), "POST", "/accounts/equity_curve")
    out = handler(_client(cfg), {"account_id": "does_not_exist"})
    assert out["ok"] is False
    assert out["error"] == "unknown_account"


# ---------------------------------------------------------------------------
# 2. wallet auto-create account
# ---------------------------------------------------------------------------


def test_wallet_configure_auto_creates_chain_account(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_wallet.routes(), "POST", "/wallet/configure")
    out = handler(
        _client(cfg),
        {
            "provider": "self_custody",
            "wallet_id": "evm_main",
            "label": "EVM main wallet",
            "config": {},
            "auto_create_account": True,
            "account_mode": "shadow",  # operator opted into shadow mode
            "balances": [
                {"chain": "ethereum", "address": "0xabc", "token": "USDT"},
            ],
            "operator": "test",
        },
    )
    assert out["ok"] is True
    assert out["account"] is not None
    assert out["account"]["ok"] is True
    assert out["account"]["mode"] == "shadow"
    profile = accounts_mod.get_account_profile(cfg.paths, out["account"]["account_id"])
    assert profile.kind == "chain"
    assert profile.wallet_id == "evm_main"
    assert profile.venue == "self_custody"
    assert profile.permissions.place_order is False
    assert profile.permissions.read_balances is True
    # The provider_config keeps the balances the operator passed so
    # ``_wallet_snapshot`` can pick them up on the next refresh tick.
    balances = (profile.raw or {}).get("provider_config", {}).get("balances")
    assert isinstance(balances, list) and balances
    assert balances[0]["address"] == "0xabc"


def test_wallet_configure_live_account_does_not_require_account_credentials(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_wallet.routes(), "POST", "/wallet/configure")
    out = handler(
        _client(cfg),
        {
            "provider": "byreal",
            "wallet_id": "byreal_main",
            "label": "Byreal wallet",
            "config": {},
            "auto_create_account": True,
            "account_mode": "live",
            "operator": "test",
        },
    )

    assert out["ok"] is True
    assert out["account"] is not None
    assert out["account"]["ok"] is True
    assert out["account"]["mode"] == "live"
    profile = accounts_mod.get_account_profile(cfg.paths, out["account"]["account_id"])
    assert profile.kind == "chain"
    assert profile.wallet_id == "byreal_main"
    assert profile.venue == "byreal"
    assert profile.credentials == {}
    assert profile.initial_balance_usd == pytest.approx(0.0)


def test_wallet_configure_auto_create_reuses_existing_wallet_account(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_wallet.routes(), "POST", "/wallet/configure")
    payload = {
        "provider": "self_custody",
        "wallet_id": "evm_main",
        "label": "EVM main wallet",
        "config": {},
        "auto_create_account": True,
        "account_mode": "shadow",
        "operator": "test",
    }

    first = handler(_client(cfg), dict(payload))
    second = handler(_client(cfg), dict(payload))

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["account"]["account_id"] == second["account"]["account_id"]
    profiles = accounts_mod.load_account_profiles(cfg.paths)
    wallet_accounts = [p for p in profiles.values() if p.wallet_id == "evm_main"]
    assert len(wallet_accounts) == 1


def test_account_intake_schema_accepts_wallet_market_source_alias(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_account_intake.routes(), "POST", "/accounts/intake/schema")
    out = handler(
        _client(cfg),
        {"venue": "byreal_onchain", "account_kind": "chain"},
    )

    assert out["ok"] is True
    assert out["venue"] == "byreal_onchain"
    assert out["provider_label"] == "Byreal CLMM DEX (Solana)"
    assert any(field["name"] == "cli_path" for field in out["credential_fields"])


def test_accounts_test_balance_uses_snapshot_path(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_accounts.routes(), "POST", "/accounts/test_balance")
    out = handler(_client(cfg), {"account_id": "paper_main"})

    assert out["ok"] is True
    assert out["account_id"] == "paper_main"
    assert out["snapshot"]["health"] == "ok"
    assert out["snapshot"]["nav_usd"] == pytest.approx(10_000)


def test_exchange_provider_schema_matches_ccxt_special_credentials():
    pytest.importorskip("ccxt")
    from nerya.connectors.provider_spec import get_registry, reset_registry

    reset_registry()
    providers = {spec.id: spec for spec in get_registry().list_specs()}

    coinbase_fields = {field.name for field in providers["coinbase"].credential_fields}
    assert {"api_key", "api_secret"} <= coinbase_fields
    assert "api_passphrase" not in coinbase_fields

    coinbase_exchange_fields = {
        field.name for field in providers["coinbase_exchange"].credential_fields
    }
    assert {"api_key", "api_secret", "api_passphrase"} <= coinbase_exchange_fields

    coinbase_intx_fields = {
        field.name for field in providers["coinbase_international"].credential_fields
    }
    assert {"api_key", "api_secret", "api_passphrase"} <= coinbase_intx_fields

    hyperliquid_fields = {
        field.name for field in providers["hyperliquid_perpetual"].credential_fields
    }
    assert {"wallet_address", "private_key"} <= hyperliquid_fields
    assert "api_key" not in hyperliquid_fields
    assert "api_secret" not in hyperliquid_fields

    bitmart_fields = {field.name for field in providers["bitmart"].credential_fields}
    assert {"api_key", "api_secret", "uid"} <= bitmart_fields
    assert "api_passphrase" not in bitmart_fields

    ndax_fields = {field.name for field in providers["ndax"].credential_fields}
    assert {"api_key", "api_secret", "uid", "login", "api_passphrase"} <= ndax_fields


def test_ccxt_hyperliquid_connector_passes_wallet_credentials():
    pytest.importorskip("ccxt")
    from nerya.connectors.ccxt_adapter import CcxtConnector
    from nerya.connectors.cex_base import CEXCredentials

    connector = CcxtConnector(
        exchange_id="hyperliquid",
        credentials=CEXCredentials(
            extras={
                "walletAddress": "0x0000000000000000000000000000000000000001",
                "privateKey": "0x" + "1" * 64,
            }
        ),
        live=True,
    )

    assert connector.client.walletAddress == "0x0000000000000000000000000000000000000001"
    assert connector.client.privateKey == "0x" + "1" * 64


def test_real_balance_modes_require_provider_required_fields(tmp_path):
    cfg = _config(tmp_path)

    with pytest.raises(TradingError, match="required connection fields"):
        accounts_mod.upsert_account(
            cfg.paths,
            {
                "id": "binance_shadow",
                "venue": "binance",
                "kind": "cex",
                "mode": "shadow",
                "permissions": {"read_balances": True},
            },
        )


def test_coinbase_advanced_live_does_not_require_passphrase(tmp_path):
    cfg = _config(tmp_path)
    key_ref = _seed_vault_ref(cfg, "coinbase_key", "key")
    secret_ref = _seed_vault_ref(cfg, "coinbase_secret", "secret")

    profile = accounts_mod.upsert_account(
        cfg.paths,
        {
            "id": "coinbase_live",
            "venue": "coinbase",
            "kind": "cex",
            "mode": "live",
            "live_trading_enabled": True,
            "credentials": {"api_key": key_ref, "api_secret": secret_ref},
        },
    )

    assert profile.venue == "coinbase"
    assert "api_passphrase" not in profile.credentials


def test_hyperliquid_live_requires_wallet_address_and_private_key(tmp_path):
    cfg = _config(tmp_path)
    private_key_ref = _seed_vault_ref(cfg, "hl_private_key", "0x" + "1" * 64)

    with pytest.raises(TradingError, match="wallet_address"):
        accounts_mod.upsert_account(
            cfg.paths,
            {
                "id": "hl_missing_address",
                "venue": "hyperliquid",
                "kind": "cex",
                "mode": "live",
                "live_trading_enabled": True,
                "credentials": {"private_key": private_key_ref},
            },
        )

    profile = accounts_mod.upsert_account(
        cfg.paths,
        {
            "id": "hl_live",
            "venue": "hyperliquid",
            "kind": "cex",
            "mode": "live",
            "live_trading_enabled": True,
            "credentials": {"private_key": private_key_ref},
            "provider_config": {
                "wallet_address": "0x0000000000000000000000000000000000000001",
            },
        },
    )

    assert profile.venue == "hyperliquid"
    assert profile.raw["provider_config"]["wallet_address"].startswith("0x")


def test_wallet_configure_without_auto_create_does_not_touch_accounts(tmp_path):
    cfg = _config(tmp_path)
    handler = _route(routes_wallet.routes(), "POST", "/wallet/configure")
    out = handler(
        _client(cfg),
        {
            "provider": "self_custody",
            "wallet_id": "evm_silent",
            "config": {},
            "operator": "test",
        },
    )
    assert out["ok"] is True
    assert out["account"] is None
    profiles = accounts_mod.load_account_profiles(cfg.paths)
    # The seed paper account is still the only one in the registry.
    assert set(profiles) == {"paper_main"}


# ---------------------------------------------------------------------------
# 3. strategy soft-warning
# ---------------------------------------------------------------------------


def test_strategy_create_warns_when_account_is_shared(tmp_path):
    cfg = _config(tmp_path)
    first = strategy_crud.create(
        cfg.paths,
        strategy_crud.CreateRequest(
            strategy_id="alpha",
            title="Alpha",
            account_id="paper_main",
            markets=("mock:BTC/USDT",),
        ),
    )
    assert first["ok"] is True
    assert "warning" not in first  # first strategy is fine

    second = strategy_crud.create(
        cfg.paths,
        strategy_crud.CreateRequest(
            strategy_id="beta",
            title="Beta",
            account_id="paper_main",
            markets=("mock:ETH/USDT",),
        ),
    )
    assert second["ok"] is True
    warning = second.get("warning")
    assert warning is not None
    assert warning["code"] == "account_already_bound"
    assert warning["account_id"] == "paper_main"
    assert any(s["strategy_id"] == "alpha" for s in warning["strategies"])
    assert "sub-account" in warning["recommendation"].lower()


def test_strategy_bind_account_emits_warning(tmp_path):
    cfg = _config(tmp_path)
    strategy_crud.create(
        cfg.paths,
        strategy_crud.CreateRequest(
            strategy_id="alpha",
            title="Alpha",
            account_id="paper_main",
            markets=("mock:BTC/USDT",),
        ),
    )
    strategy_crud.create(
        cfg.paths,
        strategy_crud.CreateRequest(
            strategy_id="gamma",
            title="Gamma",
            account_id="other_account",
            markets=("mock:ETH/USDT",),
        ),
    )
    res = strategy_crud.bind_account(cfg.paths, "gamma", "paper_main")
    assert res["ok"] is True
    assert res["warning"]["code"] == "account_already_bound"
    assert {s["strategy_id"] for s in res["warning"]["strategies"]} == {"alpha"}


# ---------------------------------------------------------------------------
# 4. CEX duplicate detection
# ---------------------------------------------------------------------------


def _seed_vault_ref(cfg: Config, name: str, value: str) -> str:
    vault = SecretVault.open(cfg.paths.vault_enc)
    vault.put(
        name=name,
        value=value,
        kind="exchange",
        scope=["exchange"],
        owner="test",
    )
    return f"vault://{name}"


def test_upsert_account_blocks_duplicate_credentials_without_force(tmp_path):
    cfg = _config(tmp_path)
    ref = _seed_vault_ref(cfg, "test_binance_key", "abc123")
    accounts_mod.upsert_account(
        cfg.paths,
        {
            "id": "binance_live",
            "venue": "binance",
            "kind": "cex",
            "mode": "live",
            "live_trading_enabled": True,
            "credentials": {"api_key": ref, "api_secret": ref},
        },
    )
    handler = _route(routes_accounts.routes(), "POST", "/accounts/upsert")
    out = handler(
        _client(cfg),
        {
            "id": "binance_live_dupe",
            "venue": "binance",
            "kind": "cex",
            "mode": "live",
            "live_trading_enabled": True,
            "credentials": {"api_key": ref, "api_secret": ref},
            "operator": "test",
        },
    )
    assert out["ok"] is False
    assert out["error"] == "duplicate_candidate"
    assert out["duplicate"]["account_id"] == "binance_live"


def test_upsert_account_allows_duplicate_with_force(tmp_path):
    cfg = _config(tmp_path)
    ref = _seed_vault_ref(cfg, "test_binance_key", "abc123")
    accounts_mod.upsert_account(
        cfg.paths,
        {
            "id": "binance_live",
            "venue": "binance",
            "kind": "cex",
            "mode": "live",
            "live_trading_enabled": True,
            "credentials": {"api_key": ref, "api_secret": ref},
        },
    )
    handler = _route(routes_accounts.routes(), "POST", "/accounts/upsert")
    out = handler(
        _client(cfg),
        {
            "id": "binance_subaccount",
            "venue": "binance",
            "kind": "cex",
            "mode": "live",
            "subaccount": "scalper",
            "live_trading_enabled": True,
            "credentials": {"api_key": ref, "api_secret": ref},
            "operator": "test",
            "force": True,
        },
    )
    assert out["ok"] is True
    assert out["applied"] is True
    assert out["account"]["profile"]["id"] == "binance_subaccount"


# ---------------------------------------------------------------------------
# 5. refresh cadence
# ---------------------------------------------------------------------------


def test_refresh_cadence_differs_between_paper_and_live(tmp_path):
    cfg = _config(tmp_path)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                },
                {
                    "id": "binance_live",
                    "venue": "binance",
                    "mode": "live",
                    "status": "active",
                    "live_trading_enabled": True,
                    "initial_balance_usd": 5_000,
                    "credentials": {},
                },
            ]
        },
    )
    profiles = accounts_mod.load_account_profiles(cfg.paths)
    paper = profiles["paper_main"]
    live = profiles["binance_live"]
    assert (
        account_refresh_interval_for_profile(cfg, paper)
        == pytest.approx(DEFAULT_ACCOUNT_REFRESH_INTERVAL_S)
    )
    assert (
        account_refresh_interval_for_profile(cfg, live)
        == pytest.approx(DEFAULT_LIVE_REFRESH_INTERVAL_S)
    )
    # next refresh ts must always be in the future even without any
    # persisted snapshot.
    eta = next_refresh_ts_for_profile(cfg, live)
    assert eta >= time.time()
