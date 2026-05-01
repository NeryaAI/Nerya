"""Wallet provider registry.

The registry exposes every provider that ships with Nerya — regardless
of whether its optional dependencies are currently installed. Use
:func:`build_provider` to materialize one, :func:`resolve_active` to
instantiate the provider selected in ``nerya.yml`` (``wallet.provider``),
and :func:`list_providers` to render a readiness report for the dashboard
/ CLI.

No factory in this module installs anything on its own. When an operator
selects a provider whose deps are missing, instantiation succeeds but the
provider's :meth:`~WalletProvider.readiness` returns ``ready=False`` and
every method that actually needs the dep raises
:class:`WalletDependencyError` with a precise install hint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import WalletProviderNotFound
from .protocol import WalletCapabilities, WalletProvider, WalletReadiness
from .providers import (
    BinanceAgenticWallet,
    BitgetWalletSkill,
    CoinbaseWallet,
    OkxOsWallet,
    SelfCustodyWallet,
)


# Per-provider credential schemas. Same shape as
# :class:`nerya.connectors.provider_spec.CredentialField` so the
# dashboard / agent intake flow can render exchange and wallet
# providers with the same renderer. Each field is keyed by the entry
# under ``wallet.<provider>.<field>`` in ``nerya.yml`` (or per-account
# overrides via ``wallet.<provider>.accounts.<id>.<field>``); secret
# values must already be ``vault://`` references — the system never
# stores plaintext.
def _field(
    name: str,
    label: str,
    *,
    kind: str = "secret",
    required: bool = True,
    description: str = "",
    placeholder: str = "",
    sensitive: bool = True,
    vault_scope: str = "wallet",
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "kind": kind,
        "required": required,
        "description": description,
        "placeholder": placeholder,
        "sensitive": sensitive,
        "vault_scope": vault_scope,
    }


PROVIDERS: dict[str, dict[str, Any]] = {
    "self_custody": {
        "id": "self_custody",
        "label": "Self-custody (goat-sdk / eth_account / solders)",
        "description": (
            "Hold keys locally, sign swaps via goat-sdk or the eth_account "
            "/ solders fallback. Chain coverage tracks the installed SDK."
        ),
        "install_hint": (
            "pip install eth-account web3 solders solana  "
            "# or follow https://github.com/goat-sdk/goat for goat."
        ),
        "install_command": "pip install eth-account web3 solders solana",
        "links": {
            "docs": "https://github.com/goat-sdk/goat",
            "config": "wallet.self_custody.{signer_ref, rpc_urls, chains}",
        },
        "runtime": "python",
        "credential_fields": [
            _field(
                "signer_ref", "Signer Vault Ref", kind="public",
                required=False, sensitive=False,
                description=(
                    "Pointer to a workspace signer policy entry. Leave "
                    "blank to keep the wallet read-only."
                ),
                placeholder="local:my_signer",
            ),
            _field(
                "rpc_urls.ethereum", "Ethereum RPC URL", kind="url",
                required=False, sensitive=False,
                placeholder="https://...",
            ),
            _field(
                "rpc_urls.solana", "Solana RPC URL", kind="url",
                required=False, sensitive=False,
                placeholder="https://api.mainnet-beta.solana.com",
            ),
        ],
    },
    "okx_os": {
        "id": "okx_os",
        "label": "OKX On-Chain OS (Web3 DEX API)",
        "description": (
            "Fetch quotes and broadcast swaps through OKX's DEX Aggregator "
            "API. Requires an OKX Developer API key + secret + passphrase."
        ),
        "install_hint": (
            "no pip/npm required. Create an API credential at "
            "https://www.okx.com/web3/build/dev-portal and store it via "
            "`nerya vault create-secret`."
        ),
        "install_command": "",
        "links": {
            "docs": "https://www.okx.com/web3/build/docs/waas/introduction",
            "config": "wallet.okx_os.{api_key_ref, api_secret_ref, api_passphrase_ref, api_project_id}",
        },
        "runtime": "http",
        "credential_fields": [
            _field("api_key", "OKX API Key", kind="secret"),
            _field("api_secret", "OKX API Secret", kind="secret"),
            _field("api_passphrase", "OKX API Passphrase", kind="secret"),
            _field(
                "api_project_id", "OKX Project ID", kind="public",
                sensitive=False, required=True,
                description="Project ID issued in the OKX dev portal.",
            ),
        ],
    },
    "bitget": {
        "id": "bitget",
        "label": "Bitget Wallet Skill (Node subprocess)",
        "description": (
            "Wrap the upstream bitget-wallet-skill Node module. Nerya invokes "
            "`node dist/nerya.js` with JSON commands on stdin/stdout."
        ),
        "install_hint": (
            "1) install node 20+, 2) `git clone https://github.com/bitget-wallet/bitget-wallet-skill`, "
            "3) run `npm install` inside it, 4) set `wallet.bitget.skill_path`."
        ),
        "install_command": (
            # The wallet install endpoint understands "git clone <repo> "
            # "<dest> && (cd <dest> && npm install)" as a structured
            # node-skill bootstrap; see nerya.install.dep_installer.
            "node-skill:https://github.com/bitget-wallet/bitget-wallet-skill"
        ),
        "install_alternatives": [
            {
                "label": "git clone (default)",
                "command": "node-skill:https://github.com/bitget-wallet/bitget-wallet-skill",
                "kind": "node-skill",
            },
            {
                "label": "npm package (@bitget-wallet/sdk)",
                "command": "npm:@bitget-wallet/sdk",
                "kind": "npm",
                "note": "Lighter install, requires an npm-published Bitget SDK.",
            },
        ],
        "links": {
            "docs": "https://github.com/bitget-wallet/bitget-wallet-skill",
            "config": "wallet.bitget.{skill_path, entry}",
        },
        "runtime": "node",
        "credential_fields": [
            _field(
                "skill_path", "Skill Directory", kind="public",
                sensitive=False,
                description="Absolute path to the cloned bitget-wallet-skill checkout.",
                placeholder="/Users/me/skills/bitget-wallet-skill",
            ),
            _field(
                "entry", "Entry file", kind="public",
                sensitive=False, required=False,
                description="Relative path inside the skill dir (defaults to dist/nerya.js).",
                placeholder="dist/nerya.js",
            ),
        ],
    },
    "binance_agentic": {
        "id": "binance_agentic",
        "label": "Binance Agentic Wallet (binance-web3 skill)",
        "description": (
            "Wrap the binance-web3 agentic wallet skill from "
            "binance-skills-hub. Same stdin/stdout protocol as Bitget."
        ),
        "install_hint": (
            "1) install node 20+, 2) `git clone https://github.com/binance/binance-skills-hub`, "
            "3) `cd skills/binance-web3/binance-agentic-wallet && npm install`, "
            "4) set `wallet.binance_agentic.skill_path` to that directory."
        ),
        "install_command": (
            "node-skill:https://github.com/binance/binance-skills-hub"
            "#path=skills/binance-web3/binance-agentic-wallet"
        ),
        "links": {
            "docs": "https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet",
            "config": "wallet.binance_agentic.{skill_path, entry}",
        },
        "runtime": "node",
        "credential_fields": [
            _field(
                "skill_path", "Skill Directory", kind="public",
                sensitive=False,
                description="Absolute path to binance-agentic-wallet inside the cloned hub.",
                placeholder="/Users/me/skills/binance-skills-hub/skills/binance-web3/binance-agentic-wallet",
            ),
            _field(
                "entry", "Entry file", kind="public",
                sensitive=False, required=False,
                description="Relative path inside the skill dir.",
                placeholder="dist/nerya.js",
            ),
        ],
    },
    "coinbase": {
        "id": "coinbase",
        "label": "Coinbase CDP Wallet (cdp-sdk / coinbase-agentkit or Node skill)",
        "description": (
            "Use Coinbase's Developer Platform wallet. Prefers the Python "
            "cdp-sdk / coinbase-agentkit package; falls back to a Node TS "
            "skill (@coinbase/cdp-sdk) using the same stdin/stdout protocol "
            "as the other Node-backed providers."
        ),
        "install_hint": (
            "pip install cdp-sdk  # or: pip install coinbase-agentkit. "
            "Create an API key at https://portal.cdp.coinbase.com/ and set "
            "wallet.coinbase.{api_key_name_ref, api_private_key_ref} via "
            "`nerya vault create-secret`. TS-only users can instead clone "
            "@coinbase/cdp-sdk and point wallet.coinbase.skill_path at it."
        ),
        "install_command": "pip install cdp-sdk",
        "install_alternatives": [
            {
                "label": "Python SDK (cdp-sdk)",
                "command": "pip install cdp-sdk",
                "kind": "pip",
            },
            {
                "label": "Python AgentKit",
                "command": "pip install coinbase-agentkit",
                "kind": "pip",
            },
            {
                "label": "Node SDK (@coinbase/cdp-sdk via git)",
                "command": "node-skill:https://github.com/coinbase/cdp-sdk",
                "kind": "node-skill",
            },
            {
                "label": "Node SDK (@coinbase/cdp-sdk via npm)",
                "command": "npm:@coinbase/cdp-sdk",
                "kind": "npm",
                "note": "Cleanest install when only the TS SDK is needed.",
            },
        ],
        "links": {
            "docs": "https://docs.cdp.coinbase.com/",
            "agentkit": "https://github.com/coinbase/coinbase-agentkit",
            "ts_sdk": "https://github.com/coinbase/cdp-sdk",
            "config": "wallet.coinbase.{api_key_name_ref, api_private_key_ref, network_id, skill_path}",
        },
        "runtime": "python_or_node",
        "credential_fields": [
            _field(
                "api_key_name", "CDP API Key Name", kind="secret",
                description="The named API key string from CDP (organizations/...).",
            ),
            _field(
                "api_private_key", "CDP Private Key", kind="secret",
                description="PEM-encoded private key generated alongside the API key name.",
            ),
            _field(
                "network_id", "Network ID", kind="public",
                sensitive=False, required=False,
                description="Default network the wallet operates on.",
                placeholder="base-mainnet",
            ),
        ],
    },
}


def _capabilities_for(name: str) -> WalletCapabilities | None:
    """Best-effort static capability lookup without a full config.

    Instantiates the provider with an empty config to reach its static
    ``capabilities()`` method. If the provider needs config just to
    build, we swallow the error and return ``None`` — the live
    ``readiness_report`` path is free to retry with the real config.
    """
    try:
        provider = build_provider(name, {})
    except Exception:
        return None
    try:
        return provider.capabilities()
    except Exception:
        return None


def list_providers() -> list[dict[str, Any]]:
    """Return the static provider catalog (not instantiated).

    Each entry is enriched with the provider's static capability
    ceiling so operator UIs can render an honest
    *installed / dependency-ready / execution-ready / experimental*
    matrix without having to actually construct credentials.
    """
    out: list[dict[str, Any]] = []
    for name, entry in PROVIDERS.items():
        item = dict(entry)
        item.setdefault("installed", True)
        caps = _capabilities_for(name)
        item["capabilities"] = caps.to_dict() if caps else None
        item["stability"] = (
            caps.execution_profile if caps is not None else "experimental"
        )
        out.append(item)
    return out


def build_provider(
    name: str,
    cfg: dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> WalletProvider:
    """Instantiate the given provider, applying cfg without installing anything."""
    name_l = (name or "").lower()
    cfg = dict(cfg or {})
    if name_l == "self_custody":
        return SelfCustodyWallet(
            signer_ref=cfg.get("signer_ref", ""),
            rpc_urls=dict(cfg.get("rpc_urls") or {}),
            chains=tuple(cfg.get("chains") or SelfCustodyWallet.chains),
            config=cfg,
        )
    if name_l == "okx_os":
        api_key = _resolve(cfg.get("api_key_ref") or cfg.get("api_key"),
                           workspace, vault_passphrase)
        api_secret = _resolve(cfg.get("api_secret_ref") or cfg.get("api_secret"),
                              workspace, vault_passphrase)
        api_pass = _resolve(cfg.get("api_passphrase_ref") or cfg.get("api_passphrase"),
                            workspace, vault_passphrase)
        return OkxOsWallet(
            api_key=api_key or "",
            api_secret=api_secret or "",
            api_passphrase=api_pass or "",
            api_project_id=str(cfg.get("api_project_id") or ""),
            base_url=str(cfg.get("base_url") or OkxOsWallet.base_url),
            config=cfg,
        )
    if name_l == "bitget":
        return BitgetWalletSkill(
            skill_path=str(cfg.get("skill_path") or ""),
            entry=str(cfg.get("entry") or BitgetWalletSkill.entry),
            repo=str(cfg.get("repo") or BitgetWalletSkill.repo),
            config=cfg,
        )
    if name_l == "binance_agentic":
        return BinanceAgenticWallet(
            skill_path=str(cfg.get("skill_path") or ""),
            entry=str(cfg.get("entry") or BinanceAgenticWallet.entry),
            repo=str(cfg.get("repo") or BinanceAgenticWallet.repo),
            config=cfg,
        )
    if name_l == "coinbase":
        api_key = _resolve(cfg.get("api_key_name_ref") or cfg.get("api_key_name"),
                            workspace, vault_passphrase)
        api_priv = _resolve(cfg.get("api_private_key_ref") or cfg.get("api_private_key"),
                             workspace, vault_passphrase)
        return CoinbaseWallet(
            api_key_name=api_key or "",
            api_private_key=api_priv or "",
            network_id=str(cfg.get("network_id") or CoinbaseWallet.network_id),
            skill_path=str(cfg.get("skill_path") or ""),
            entry=str(cfg.get("entry") or CoinbaseWallet.entry),
            repo=str(cfg.get("repo") or CoinbaseWallet.repo),
            config=cfg,
        )
    raise WalletProviderNotFound(
        f"unknown wallet provider {name!r}. Known: {sorted(PROVIDERS)}"
    )


def resolve_active(
    config: dict[str, Any] | None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> tuple[str, WalletProvider | None]:
    """Return (selected-name-or-empty, provider-or-None) based on nerya.yml."""
    wallet_cfg = ((config or {}).get("wallet") or {})
    name = str(wallet_cfg.get("provider") or "").strip().lower()
    if not name:
        return "", None
    provider_cfg = dict((wallet_cfg.get(name) or {}))
    provider = build_provider(name, provider_cfg,
                               workspace=workspace, vault_passphrase=vault_passphrase)
    return name, provider


def list_configured_providers(
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return every wallet binding declared in ``nerya.yml``.

    Plan 2026-04-29 §11 P8 — Nerya now lets operators run *multiple*
    wallet providers in parallel by declaring them under
    ``wallet.providers.<wallet_id>`` (each entry carries a provider id
    and a per-id config). The legacy ``wallet.provider`` key still works
    and is surfaced here as a single ``default`` binding for
    backward compatibility.

    The result is the dict:

    ``{wallet_id, provider, label, config, source}``

    where ``source`` is either ``"providers"`` (new map) or ``"legacy"``
    (old ``wallet.provider`` key). Callers that need to render or
    enumerate available wallets — Settings page, account upsert form,
    accountdashboard — use this method instead of poking
    ``nerya.yml`` directly.
    """

    out: list[dict[str, Any]] = []
    wallet_cfg = dict(((config or {}).get("wallet") or {}))
    providers_map = wallet_cfg.get("providers")
    if isinstance(providers_map, dict):
        for wid, entry in providers_map.items():
            if not isinstance(entry, dict):
                continue
            provider_name = str(entry.get("provider") or "").lower().strip()
            if not provider_name:
                continue
            label = str(entry.get("label") or wid)
            cfg = dict(entry.get("config") or {})
            out.append(
                {
                    "wallet_id": str(wid),
                    "provider": provider_name,
                    "label": label,
                    "config": cfg,
                    "source": "providers",
                }
            )
    legacy_provider = str(wallet_cfg.get("provider") or "").lower().strip()
    if legacy_provider:
        legacy_cfg = dict((wallet_cfg.get(legacy_provider) or {}))
        # Don't duplicate: if the legacy provider has the same id as a
        # new map entry, we keep the new map entry (operators typically
        # add the new declaration first then drop the legacy key).
        if not any(b["wallet_id"] == legacy_provider for b in out):
            out.append(
                {
                    "wallet_id": legacy_provider,
                    "provider": legacy_provider,
                    "label": f"{legacy_provider} (default)",
                    "config": legacy_cfg,
                    "source": "legacy",
                }
            )
    return out


def resolve_for_account(
    config: dict[str, Any] | None,
    account_id: str,
    wallet_id: str | None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> tuple[str, WalletProvider | None, str]:
    """Return ``(wallet_id, provider_or_None, source)`` for an account row.

    Resolution order:

    1. If ``wallet_id`` is provided and matches an entry in
       ``wallet.providers``, use that.
    2. If ``wallet_id`` matches a legacy ``wallet.<id>`` block, use that.
    3. Otherwise fall back to the legacy ``wallet.provider`` key.

    ``account_id`` is currently informational only (it shows up in
    error messages and journals). Per-account credential overrides
    live inside the wallet binding's ``config.accounts.<account_id>``
    so multiple accounts can share a provider with different sub-keys.
    """

    bindings = list_configured_providers(config)
    selected: dict[str, Any] | None = None
    if wallet_id:
        for b in bindings:
            if b["wallet_id"] == wallet_id:
                selected = b
                break
    if selected is None and bindings:
        selected = bindings[0]
    if selected is None:
        return "", None, "none"
    cfg = dict(selected["config"])
    per_account = (cfg.pop("accounts", None) or {}).get(account_id)
    if isinstance(per_account, dict):
        cfg.update(per_account)
    try:
        provider = build_provider(
            selected["provider"],
            cfg,
            workspace=workspace,
            vault_passphrase=vault_passphrase,
        )
    except WalletProviderNotFound:
        return selected["wallet_id"], None, selected["source"]
    return selected["wallet_id"], provider, selected["source"]


def resolve_for_strategy(
    config: dict[str, Any] | None,
    strategy_id: str,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> tuple[str, WalletProvider | None, str]:
    """Return ``(provider_name, provider_or_None, source)`` for a strategy.

    ``source`` is ``"strategy"`` when the strategy.yml carries an explicit
    ``wallet_id``, ``"global"`` when it falls through to ``wallet.provider``
    in nerya.yml, and ``"none"`` when neither is set. ``strategy_id`` must
    already be sanitised by the caller — we don't touch paths beyond reading
    the strategy yaml.

    Per-strategy config overrides live at ``wallet.<provider>.strategies.<sid>``
    in nerya.yml and are merged on top of the provider's global config
    (so e.g. different strategies can use different OKX sub-keys while
    sharing the provider selection).
    """
    if not workspace:
        return "", None, "none"
    sid = str(strategy_id or "").strip()
    wallet_cfg = dict(((config or {}).get("wallet") or {}))
    strat_path = Path(workspace) / "strategies" / sid / "strategy.yml"

    strategy_wallet_id: str | None = None
    if sid and strat_path.exists():
        try:
            from ..core import yaml_io
            doc = yaml_io.load(strat_path, default={}) or {}
            val = doc.get("wallet_id")
            if val:
                strategy_wallet_id = str(val).strip().lower() or None
        except Exception:
            strategy_wallet_id = None

    if strategy_wallet_id:
        name = strategy_wallet_id
        source = "strategy"
    else:
        name = str(wallet_cfg.get("provider") or "").strip().lower()
        source = "global" if name else "none"
    if not name:
        return "", None, "none"

    provider_cfg = dict((wallet_cfg.get(name) or {}))
    per_strategy = (
        dict((provider_cfg.pop("strategies", None) or {}).get(sid) or {})
        if sid else {}
    )
    if per_strategy:
        provider_cfg.update(per_strategy)

    provider = build_provider(
        name, provider_cfg,
        workspace=workspace, vault_passphrase=vault_passphrase,
    )
    return name, provider, source


def readiness_report(
    config: dict[str, Any] | None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> list[dict[str, Any]]:
    """Return a rendered catalog with live readiness + capability status.

    Each entry carries:

    - ``installed`` — always ``True`` for providers that ship with Nerya
      (the catalog is static); surfaced here so the UI never has to
      re-derive it.
    - ``readiness`` — live *dependency-ready* signal from the provider.
    - ``capabilities`` — the static *execution-ready* ceiling
      (per-method real/partial/experimental/stub) rolled up into
      ``capabilities.execution_profile``.
    - ``stability`` — shortcut for ``capabilities.execution_profile`` so
      UIs can render a single truthful pill per provider.
    """
    out: list[dict[str, Any]] = []
    for name in PROVIDERS:
        static = dict(PROVIDERS[name])
        static.setdefault("installed", True)
        cfg = dict((((config or {}).get("wallet") or {}).get(name) or {}))
        caps: WalletCapabilities | None = None
        try:
            p = build_provider(name, cfg, workspace=workspace,
                                vault_passphrase=vault_passphrase)
            r: WalletReadiness = p.readiness()
            static["readiness"] = r.to_dict()
            try:
                caps = p.capabilities()
            except Exception:
                caps = None
        except Exception as exc:
            static["readiness"] = WalletReadiness(
                provider=name, ready=False, reason=str(exc),
            ).to_dict()
            caps = _capabilities_for(name)
        static["capabilities"] = caps.to_dict() if caps else None
        static["stability"] = (
            caps.execution_profile if caps is not None else "experimental"
        )
        out.append(static)
    return out


# ------------------------------------------------------------------
def _resolve(
    value: str | None,
    workspace: Path | None,
    vault_passphrase: str | None,
) -> str | None:
    if not value:
        return None
    v = str(value)
    if not v.startswith("vault://") or not workspace:
        return v
    try:
        from ..security.secrets import SecretVault
        vp = Path(workspace) / "vault" / "secrets.enc"
        if not vp.exists():
            return None
        s = SecretVault.open(vp, passphrase=vault_passphrase)
        return s.resolve(v.split("vault://", 1)[-1], required_scope="wallet")
    except Exception:
        return None


# Convenience alias for older code paths.
class WalletRegistry:
    """Tiny stateful wrapper so consumers can cache one provider per workspace."""

    def __init__(self, workspace: Path | None = None,
                 vault_passphrase: str | None = None):
        self.workspace = workspace
        self.vault_passphrase = vault_passphrase
        self._cached: dict[str, WalletProvider] = {}

    def get(self, name: str, cfg: dict[str, Any] | None = None) -> WalletProvider:
        if name in self._cached:
            return self._cached[name]
        p = build_provider(name, cfg, workspace=self.workspace,
                            vault_passphrase=self.vault_passphrase)
        self._cached[name] = p
        return p

    def invalidate(self, name: str | None = None) -> None:
        if name:
            self._cached.pop(name, None)
        else:
            self._cached.clear()


__all__ = [
    "PROVIDERS", "WalletRegistry", "build_provider",
    "list_providers", "readiness_report", "resolve_active",
    "resolve_for_strategy", "resolve_for_account",
    "list_configured_providers",
]
