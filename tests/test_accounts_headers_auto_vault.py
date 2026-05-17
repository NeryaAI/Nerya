"""Tests for the auto-vault path in ``/accounts/headers/patch``.

The endpoint historically rejected anything that looked like a
plaintext API token and forced the operator to first call
``/security/secrets/put`` then submit ``vault://<ref>``. The dashboard
now opts into ``auto_vault=true`` which stashes the plaintext value
into :class:`SecretVault` under a deterministic name and rewrites the
header payload to ``vault://hdr_<account>_<header>_<fingerprint>``
before the strict validator runs.

These tests pin down:

1. Plaintext values are stored in SecretVault and removed from the
   on-disk ``provider_config.headers`` map.
2. ``Bearer <plaintext>`` keeps the bearer prefix and only vaults the
   token portion.
3. Existing ``vault://`` refs and short non-secret hints are
   passed through untouched.
4. ``auto_vault=false`` (default) still rejects plaintext for
   programmatic callers — backward-compatible.
5. The auto-vault entries are deterministic — re-saving the same
   value reuses the same vault row instead of creating duplicates.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_accounts
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.security.secrets import SecretVault

pytestmark = pytest.mark.smoke


def _bootstrap(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg)
    route_map = {(m, p): h for m, p, h in routes_accounts.routes()}
    upsert = route_map[("POST", "/accounts/upsert")]
    upsert(
        client,
        {
            "id": "cmc_paper",
            "venue": "coinmarketcap",
            "kind": "data_source",
            "mode": "paper",
            "provider_spec": "coinmarketcap",
            "credentials": {},
            "operator": "dashboard",
        },
    )
    patch = route_map[("POST", "/accounts/headers/patch")]
    return client, patch


def test_auto_vault_rewrites_plaintext_to_vault_ref(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    res = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {
                "X-CMC_PRO_API_KEY": "abcdef1234567890_CMC_KEY",
            },
            "operator": "dashboard",
        },
    )
    assert res["ok"] is True, res
    assert "vaulted_refs" in res
    assert "X-CMC_PRO_API_KEY" in res["vaulted_refs"]
    assert res["vaulted_refs"]["X-CMC_PRO_API_KEY"].startswith("vault://hdr_")

    # Persisted yaml never contains the plaintext token.
    saved = yaml_io.load(tmp_path / "accounts" / "accounts.yml", default={})
    serialised = str(saved)
    assert "abcdef1234567890_CMC_KEY" not in serialised, "plaintext leaked to disk"
    row = next(r for r in saved["accounts"] if r["id"] == "cmc_paper")
    stored_value = row["provider_config"]["headers"]["X-CMC_PRO_API_KEY"]
    assert stored_value.startswith("vault://hdr_")

    # The vault resolves back to the original plaintext.
    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    name = stored_value.removeprefix("vault://")
    assert vault.resolve(name, required_scope="exchange") == "abcdef1234567890_CMC_KEY"


def test_auto_vault_preserves_bearer_prefix(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    res = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {
                "Authorization": "Bearer abcdef1234567890_TOKEN",
            },
            "operator": "dashboard",
        },
    )
    assert res["ok"] is True
    ref = res["vaulted_refs"]["Authorization"]
    assert ref.startswith("vault://hdr_")

    saved = yaml_io.load(tmp_path / "accounts" / "accounts.yml", default={})
    row = next(r for r in saved["accounts"] if r["id"] == "cmc_paper")
    persisted = row["provider_config"]["headers"]["Authorization"]
    assert persisted.startswith("Bearer vault://hdr_")
    assert "abcdef1234567890_TOKEN" not in str(saved)


def test_auto_vault_passes_through_existing_vault_refs(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    res = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {
                "X-Preconfigured": "vault://my_existing_key",
            },
            "operator": "dashboard",
        },
    )
    assert res["ok"] is True
    # Existing vault refs do NOT trigger a new vault row.
    assert "X-Preconfigured" not in res["vaulted_refs"]


def test_auto_vault_passes_through_short_non_secrets(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    res = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {
                "Accept": "application/json",
                "X-Locale": "en-US",
            },
            "operator": "dashboard",
        },
    )
    assert res["ok"] is True
    assert res["vaulted_refs"] == {}
    saved = yaml_io.load(tmp_path / "accounts" / "accounts.yml", default={})
    row = next(r for r in saved["accounts"] if r["id"] == "cmc_paper")
    persisted = row["provider_config"]["headers"]
    # Non-secret hints survive verbatim.
    assert persisted["Accept"] == "application/json"
    assert persisted["X-Locale"] == "en-US"


def test_strict_mode_still_rejects_plaintext(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    res = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            # auto_vault defaults to false — programmatic callers must
            # still pre-vault their secrets explicitly.
            "headers": {
                "X-CMC_PRO_API_KEY": "abcdef1234567890_CMC_KEY",
            },
            "operator": "agent",
        },
    )
    assert res["ok"] is False
    assert res["error"] == "plaintext_secret_refused"


def test_auto_vault_is_deterministic_for_same_token(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    first = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {"X-API-Key": "abcdef1234567890_STABLE_TOKEN"},
        },
    )
    second = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {"X-API-Key": "abcdef1234567890_STABLE_TOKEN"},
        },
    )
    assert first["vaulted_refs"]["X-API-Key"] == second["vaulted_refs"]["X-API-Key"]
    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    assert len([m for m in vault.list() if m.kind == "header_token"]) == 1


def test_auto_vault_uses_different_name_when_token_rotates(tmp_path):
    client, patch_headers = _bootstrap(tmp_path)

    first = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {"X-API-Key": "abcdef1234567890_FIRST_TOKEN"},
        },
    )
    rotated = patch_headers(
        client,
        {
            "account_id": "cmc_paper",
            "auto_vault": True,
            "headers": {"X-API-Key": "abcdef1234567890_SECOND_TOKEN"},
        },
    )
    assert (
        first["vaulted_refs"]["X-API-Key"]
        != rotated["vaulted_refs"]["X-API-Key"]
    ), "rotated token must land in a new vault entry for audit trail"
