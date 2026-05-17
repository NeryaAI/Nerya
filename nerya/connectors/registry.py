"""Connector registry.

Builds a connector from an account config dict. The actual venue →
class mapping now lives in :mod:`nerya.connectors.provider_spec`, which
also supports hot-loading user-authored providers from
``workspace/providers/<id>/provider.py``.

Example account (workspace/accounts/accounts.yml):

    accounts:
      - id: binance_main
        venue: binance          # routed through CcxtConnector
        kind: cex
        live: true
        api_key_ref: vault://binance_api_key
        api_secret_ref: vault://binance_api_secret

      - id: kraken_main
        venue: ccxt:kraken      # explicit ccxt:<id> picks any ccxt exchange
        kind: cex
        live: false
        api_key_ref: vault://kraken_api_key
        api_secret_ref: vault://kraken_api_secret

      - id: polymarket_read
        venue: polymarket
        kind: prediction_market
        live: false

      - id: paper_main
        venue: mock
        kind: cex
        live: false

Users can ask Nerya to auto-author a new venue (``exchange_author`` skill).
Accepted proposals land in ``workspace/providers/<id>/provider.py`` and
are loaded on the next ``build_connector`` call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import Connector
from .cex_base import CEXCredentials


@dataclass
class ConnectorRegistry:
    workspace: Path
    vault_passphrase: str | None = None
    _cache: dict[str, Connector] = field(default_factory=dict, init=False)

    def get(self, account_id: str, account_cfg: dict[str, Any]) -> Connector:
        if account_id in self._cache:
            return self._cache[account_id]
        conn = build_connector(account_cfg, workspace=self.workspace,
                                vault_passphrase=self.vault_passphrase)
        self._cache[account_id] = conn
        return conn

    def invalidate(self, account_id: str | None = None) -> None:
        if account_id:
            self._cache.pop(account_id, None)
        else:
            self._cache.clear()

    def reload_providers(self) -> None:
        from .provider_spec import get_registry
        get_registry().reload_workspace(self.workspace)
        self._cache.clear()


def build_connector(
    account_cfg: dict[str, Any],
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> Connector:
    """Construct a connector for an account config."""
    from .provider_spec import get_registry
    venue = (account_cfg.get("venue") or "mock").lower()
    cfg = dict(account_cfg)
    cfg["venue"] = venue
    return get_registry().build(
        cfg, workspace=workspace, vault_passphrase=vault_passphrase,
    )


# ------------------------------------------------------------ internal
def _resolve_cex_creds(
    account_cfg: dict[str, Any],
    workspace: Path | None,
    vault_passphrase: str | None,
    *,
    with_passphrase: bool = False,
) -> CEXCredentials:
    """Resolve API key / secret / passphrase from a Nerya account config.

    Two shapes are accepted, in priority order:

    1. The new ``credentials: {api_key, api_secret, api_passphrase}``
       map written by :func:`nerya.trading.accounts.upsert_account`.
       Each value must be a ``vault://`` reference.
    2. Legacy flat ``api_key_ref``/``api_secret_ref``/``api_passphrase_ref``
       keys for hand-edited YAML files predating 04-29.

    Anything that doesn't resolve falls through as an empty string so
    public-data calls keep working without credentials.
    """

    creds_map = account_cfg.get("credentials")
    if not isinstance(creds_map, dict):
        creds_map = {}
    provider_config = account_cfg.get("provider_config")
    if not isinstance(provider_config, dict):
        provider_config = {}

    def _pull(field: str, legacy_key: str) -> str:
        ref = (
            creds_map.get(field)
            or provider_config.get(field)
            or account_cfg.get(field)
            or account_cfg.get(legacy_key)
            or ""
        )
        if not isinstance(ref, str):
            return ""
        if ref.startswith("vault://"):
            return _resolve_ref(
                ref, workspace, vault_passphrase, scope="exchange",
            ) or ""
        # Legacy fixtures still occasionally embed non-vault values for
        # local mock runs. Treat them as already-resolved.
        return ref

    key = _pull("api_key", "api_key_ref")
    sec = _pull("api_secret", "api_secret_ref")
    pw = _pull("api_passphrase", "api_passphrase_ref") if with_passphrase else ""
    extras = {
        "uid": _pull("uid", "uid_ref"),
        "accountId": _pull("account_id", "account_id_ref"),
        "login": _pull("login", "login_ref"),
        "privateKey": (
            _pull("private_key", "private_key_ref")
            or _pull("wallet_private_key", "wallet_private_key_ref")
        ),
        "walletAddress": (
            _pull("wallet_address", "wallet_address_ref")
            or _pull("address", "address_ref")
        ),
        "token": _pull("token", "token_ref"),
    }
    # Backward compatibility for older Hyperliquid rows that used the
    # generic api_key/api_secret slots before the CCXT-specific wallet
    # field names were exposed.
    venue = str(account_cfg.get("venue") or account_cfg.get("exchange") or "").lower()
    if venue in {"hyperliquid", "hyperliquid_perpetual", "hyperliquid_perp", "hl", "hl_perp"}:
        extras["walletAddress"] = extras.get("walletAddress") or key
        extras["privateKey"] = extras.get("privateKey") or sec
    extras = {k: v for k, v in extras.items() if v}
    return CEXCredentials(
        api_key=key,
        api_secret=sec,
        api_passphrase=pw,
        extras=extras,
    )


def _resolve_ref(
    ref: str | None,
    workspace: Path | None,
    vault_passphrase: str | None,
    *,
    scope: str,
) -> str | None:
    if not ref or not workspace:
        return None
    if not ref.startswith("vault://"):
        return None
    try:
        from ..security.secrets import SecretVault
        vp = workspace / "vault" / "secrets.enc"
        if not vp.exists():
            return None
        v = SecretVault.open(vp, passphrase=vault_passphrase)
        return v.resolve(ref.split("vault://", 1)[-1], required_scope=scope)
    except Exception:
        return None


def list_providers() -> list[dict[str, Any]]:
    """Public helper used by /exchanges API route + dashboard readiness."""
    from .provider_spec import get_registry
    return [s.to_info() for s in get_registry().list_specs()]


__all__ = ["ConnectorRegistry", "build_connector", "list_providers"]
