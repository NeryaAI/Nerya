"""Regression coverage for the audited wallet integration-surface bugs.

Covers the HTTP/CLI/dashboard fixes that landed together:

1. ``route_scopes`` — ``/wallet/portfolio`` and ``/wallet/uninstall``
   must have explicit rules instead of falling into the ``admin:ops``
   unknown-route default (which broke remote dashboards on scoped
   tokens).
2. ``nerya wallet`` CLI — the provider choice list must come from
   ``wallet.registry.PROVIDERS`` (so ``byreal`` is accepted) and
   ``cmd_wallet_status`` must resolve credentials from
   ``wallet.providers.<id>`` bindings, not just the legacy
   ``wallet.<name>`` block.
3. ``_vaultify_wallet_config`` — credential fields missing from the
   provider schema default to sensitive (vaultified) instead of being
   persisted in plaintext.
4. ``_maybe_create_wallet_account`` — an operator-typed account-id hint
   that collides with an unrelated (e.g. CEX) account row must never
   overwrite that row's identity.
5. ``_configure_wallet_binding`` — a failed auto account creation is
   surfaced as a top-level ``account_warning`` next to ``ok: true``.
6. ``/wallet/swap`` — provider *transport* failures are reported with
   ``_status: 502`` (honoured by ``local_server._status_body_from_result``)
   instead of a 200 envelope.

All tests run fully offline: no real venue, no real RPC, no real CLI.
"""

from __future__ import annotations

from copy import deepcopy
import argparse
from types import SimpleNamespace

import pytest

from nerya.api import local_server, route_scopes
from nerya.api import routes_wallet
from nerya.cli.commands import wallet as wallet_cli
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import TradingError
from nerya.core.paths import WorkspacePaths
from nerya.trading import accounts as accounts_mod
from nerya.wallet.errors import WalletTransportError


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    data["runtime"]["kill_switch"] = False
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def _client(cfg: Config) -> SimpleNamespace:
    return SimpleNamespace(config=cfg)


def _route(routes, method: str, path: str):
    for m, p, handler in routes:
        if m == method and p == path:
            return handler
    raise AssertionError(f"route not registered: {method} {path}")


# ---------------------------------------------------------------------------
# 1. route scopes
# ---------------------------------------------------------------------------


def test_wallet_portfolio_requires_read_runtime_scope():
    assert route_scopes.required_scope("POST", "/wallet/portfolio") == "read:runtime"
    assert route_scopes.required_scope("GET", "/wallet/portfolio") == "read:runtime"


def test_wallet_uninstall_requires_write_config_scope():
    assert route_scopes.required_scope("POST", "/wallet/uninstall") == "write:config"


def test_scoped_remote_token_can_fetch_wallet_portfolio():
    ok, reason = route_scopes.authorize(
        {"read:runtime"}, "POST", "/wallet/portfolio",
    )
    assert ok is True
    assert reason is None
    # A read-only token still may not uninstall skill packages.
    ok, reason = route_scopes.authorize(
        {"read:runtime"}, "POST", "/wallet/uninstall",
    )
    assert ok is False
    assert reason == "insufficient_scope:needed=write:config"


# ---------------------------------------------------------------------------
# 2. CLI provider list + status config resolution
# ---------------------------------------------------------------------------


def test_cli_valid_providers_come_from_registry_and_include_byreal():
    from nerya import wallet as wallet_mod

    providers = wallet_cli._valid_providers()
    assert providers == sorted(wallet_mod.PROVIDERS.keys())
    assert "byreal" in providers


def test_cli_argparse_accepts_byreal_and_rejects_unknown():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    wallet_cli.register(sub)

    args = parser.parse_args(["wallet", "use", "byreal"])
    assert args.provider == "byreal"

    with pytest.raises(SystemExit):
        parser.parse_args(["wallet", "use", "definitely_not_a_provider"])


def test_cmd_wallet_status_uses_wallet_providers_binding(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "provider": "self_custody",
        # Only a wallet.providers binding — no legacy wallet.<name> block.
        "providers": {
            "evm_main": {
                "provider": "self_custody",
                "label": "EVM main",
                "config": {"rpc_url": "https://rpc.example.invalid"},
            },
        },
    }
    captured: dict = {}

    class FakeProvider:
        def readiness(self):
            return SimpleNamespace(to_dict=lambda: {
                "provider": captured.get("provider"),
                "ready": True,
                "saw_config": captured.get("cfg"),
            })

    def fake_build_provider(name, provider_cfg, workspace=None, **_kwargs):
        captured["provider"] = name
        captured["cfg"] = dict(provider_cfg or {})
        return FakeProvider()

    monkeypatch.setattr(wallet_cli, "_client", lambda ws, profile=None: _client(cfg))
    monkeypatch.setattr("nerya.wallet.build_provider", fake_build_provider)

    rc = wallet_cli.cmd_wallet_status(
        SimpleNamespace(workspace=str(tmp_path), profile=None, provider=None),
    )

    assert rc == 0
    assert captured["provider"] == "self_custody"
    # The binding config must reach the provider; an empty legacy block
    # must not shadow it.
    assert captured["cfg"] == {"rpc_url": "https://rpc.example.invalid"}
    out = capsys.readouterr().out
    assert "https://rpc.example.invalid" in out


def test_cmd_wallet_status_prefers_meaningful_legacy_block(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "provider": "self_custody",
        "self_custody": {"rpc_url": "https://legacy.example.invalid"},
        "providers": {
            "evm_main": {
                "provider": "self_custody",
                "config": {"rpc_url": "https://binding.example.invalid"},
            },
        },
    }
    captured: dict = {}

    class FakeProvider:
        def readiness(self):
            return SimpleNamespace(to_dict=lambda: {"ready": True})

    def fake_build_provider(name, provider_cfg, workspace=None, **_kwargs):
        captured["cfg"] = dict(provider_cfg or {})
        return FakeProvider()

    monkeypatch.setattr(wallet_cli, "_client", lambda ws, profile=None: _client(cfg))
    monkeypatch.setattr("nerya.wallet.build_provider", fake_build_provider)

    rc = wallet_cli.cmd_wallet_status(
        SimpleNamespace(workspace=str(tmp_path), profile=None, provider=None),
    )

    assert rc == 0
    assert captured["cfg"] == {"rpc_url": "https://legacy.example.invalid"}


# ---------------------------------------------------------------------------
# 3. vaultify default-sensitive behaviour
# ---------------------------------------------------------------------------


def test_vaultify_unknown_fields_default_to_sensitive(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setattr(
        routes_wallet,
        "_wallet_schema",
        lambda _provider: [
            {"name": "rpc_url", "sensitive": False},
            {"name": "api_key", "sensitive": True},
        ],
    )

    out, stored = routes_wallet._vaultify_wallet_config(
        _client(cfg),
        provider="fake_provider",
        wallet_id="fake_main",
        config={
            "rpc_url": "https://rpc.example.invalid",   # schema: public
            "api_key": "k",                             # schema: sensitive
            "mnemonic": "test mnemonic words",          # unknown -> sensitive
            "seed_phrase": "test seed words",           # unknown -> sensitive
            "label": "My wallet",                       # public allowlist
            "enabled": True,                            # boolean -> public
        },
        operator="tester",
    )

    assert out["rpc_url"] == "https://rpc.example.invalid"
    assert "api_key" not in out
    assert out["api_key_ref"].startswith("vault://")
    # Unknown credential fields must never persist plaintext.
    assert "mnemonic" not in out and "seed_phrase" not in out
    assert out["mnemonic_ref"].startswith("vault://")
    assert out["seed_phrase_ref"].startswith("vault://")
    # Public allowlist + boolean stay in the config block.
    assert out["label"] == "My wallet"
    assert "enabled" in out and "enabled_ref" not in out
    # stored_refs stay accurate: exactly the vaultified fields.
    stored_names = {row["name"] for row in stored}
    assert stored_names == {
        "wallet_fake_main_api_key",
        "wallet_fake_main_mnemonic",
        "wallet_fake_main_seed_phrase",
    }


# ---------------------------------------------------------------------------
# 4. account-id hint collision guard
# ---------------------------------------------------------------------------


def test_wallet_account_hint_does_not_overwrite_cex_account(tmp_path):
    cfg = _config(tmp_path)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "prod_cex",
                    "venue": "binance",
                    "kind": "cex",
                    "mode": "paper",
                    "status": "active",
                    "permissions": {"read_balances": True},
                }
            ]
        },
    )

    result = routes_wallet._maybe_create_wallet_account(
        _client(cfg),
        provider="byreal",
        wallet_id="byreal_main",
        label="Byreal wallet",
        config={},
        operator="tester",
        auto_create=True,
        account_mode="shadow",
        account_id_hint="prod_cex",
        initial_balance_usd=None,
        balances=None,
    )

    assert result["ok"] is True
    # A fresh id was allocated instead of hijacking the CEX row.
    assert result["account_id"] != "prod_cex"
    cex_row = accounts_mod.get_account_profile(cfg.paths, "prod_cex")
    assert cex_row.kind == "cex"
    assert cex_row.venue == "binance"
    assert not cex_row.wallet_id
    wallet_row = accounts_mod.get_account_profile(cfg.paths, result["account_id"])
    assert wallet_row.kind == "chain"
    assert wallet_row.wallet_id == "byreal_main"


def test_wallet_account_hint_adopts_compatible_chain_row(tmp_path):
    cfg = _config(tmp_path)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "my_wallet",
                    "venue": "byreal",
                    "kind": "chain",
                    "mode": "shadow",
                    "status": "active",
                }
            ]
        },
    )

    result = routes_wallet._maybe_create_wallet_account(
        _client(cfg),
        provider="byreal",
        wallet_id="byreal_main",
        label="Byreal wallet",
        config={},
        operator="tester",
        auto_create=True,
        account_mode="shadow",
        account_id_hint="my_wallet",
        initial_balance_usd=None,
        balances=None,
    )

    assert result["ok"] is True
    # Wallet-shaped rows with no conflicting wallet_id are adoptable.
    assert result["account_id"] == "my_wallet"
    profile = accounts_mod.get_account_profile(cfg.paths, "my_wallet")
    assert profile.kind == "chain"
    assert profile.wallet_id == "byreal_main"


# ---------------------------------------------------------------------------
# 5. account_warning on configure
# ---------------------------------------------------------------------------


def test_configure_reports_account_warning_when_auto_create_fails(
    tmp_path, monkeypatch,
):
    cfg = _config(tmp_path)

    def boom(*_args, **_kwargs):
        raise TradingError("simulated vault outage")

    monkeypatch.setattr(routes_wallet.accounts_mod, "upsert_account", boom)

    out = routes_wallet._configure_wallet_binding(
        _client(cfg),
        provider="byreal",
        wallet_id="byreal_main",
        label="Byreal wallet",
        config={},
        activate=False,
        operator="tester",
        auto_create_account=True,
        account_mode="live",
    )

    # The binding itself saved…
    assert out["ok"] is True
    # …but the failed account creation must be surfaced, not swallowed.
    assert out["account"]["ok"] is False
    assert out["account"]["error"] == "upsert_failed"
    warning = out["account_warning"]
    assert warning is not None
    assert warning["error"] == "upsert_failed"
    assert "simulated vault outage" in (warning["detail"] or "")
    assert warning["attempted_mode"] == "live"


def test_configure_account_warning_is_none_on_success(tmp_path):
    cfg = _config(tmp_path)
    out = routes_wallet._configure_wallet_binding(
        _client(cfg),
        provider="byreal",
        wallet_id="byreal_main",
        label="Byreal wallet",
        config={},
        activate=False,
        operator="tester",
        auto_create_account=True,
        account_mode="shadow",
    )
    assert out["ok"] is True
    assert out["account"]["ok"] is True
    assert out["account_warning"] is None


# ---------------------------------------------------------------------------
# 6. swap transport failure -> 502
# ---------------------------------------------------------------------------


def test_swap_transport_failure_sets_502_status(tmp_path, monkeypatch):
    cfg = _config(tmp_path)

    def boom(*_args, **_kwargs):
        raise WalletTransportError("rpc upstream returned 503")

    monkeypatch.setattr(routes_wallet, "prepare_swap", boom)
    handler = _route(routes_wallet.routes(), "POST", "/wallet/swap")

    out = handler(_client(cfg), {"provider": "byreal"})

    assert out["ok"] is False
    assert out["error"] == "provider_error"
    assert "503" in out["reason"]
    assert out["_status"] == 502

    # And the dispatcher really does turn the marker into an HTTP 502
    # while stripping the private key from the body.
    status, body = local_server._status_body_from_result(out)
    assert status == 502
    assert "_status" not in body
    assert body["error"] == "provider_error"
