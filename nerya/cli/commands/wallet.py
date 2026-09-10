"""``nerya wallet`` subcommands.

List providers, check readiness, print install hints, and switch the
active provider. The provider list is imported lazily so missing
optional deps don't explode the whole CLI.
"""

from __future__ import annotations

from typing import Any

from .._common import _add_ws, _client, _print
from ...core import yaml_io


_FALLBACK_PROVIDERS: tuple[str, ...] = (
    "self_custody", "metamask", "okx_os", "bitget", "binance_agentic",
    "coinbase",
)


def _valid_providers() -> list[str]:
    """Provider ids accepted by ``wallet install-hint`` / ``wallet use``.

    Built from ``wallet.registry.PROVIDERS`` at call time so registry
    additions (e.g. ``byreal``) are accepted without editing this
    module. Falls back to a static tuple when the wallet package itself
    cannot be imported, so argparse keeps working on a broken install.
    """
    try:
        from ... import wallet as wallet_mod
        return sorted(str(k) for k in wallet_mod.PROVIDERS.keys())
    except Exception:  # pragma: no cover — degraded CLI bootstrap
        return list(_FALLBACK_PROVIDERS)


def _meaningful_provider_cfg(cfg: dict[str, Any]) -> bool:
    for key, value in (cfg or {}).items():
        if value in (None, "", [], {}):
            continue
        if key == "entry" and value in {
            "dist/nerya.js",
            "dist/index.js",
            "scripts/bitget-wallet-agent-api.py",
        }:
            continue
        if key == "chains":
            # Shipped by DEFAULT_CONFIG (deep-merged into every config);
            # its presence does not mean the operator configured a legacy
            # block here.
            continue
        return True
    return False


def _provider_cfg(wallet_mod, cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """Resolve the credential config for ``name`` like the HTTP layer does.

    Prefers the legacy ``wallet.<name>`` block when it carries real
    values, otherwise falls back to the first
    ``wallet.providers.<id>`` binding declared for this provider.
    Re-implemented locally on purpose — the CLI must not import the
    HTTP route module.
    """
    wallet_cfg = cfg.get("wallet") or {}
    legacy = dict(wallet_cfg.get(name) or {})
    if _meaningful_provider_cfg(legacy):
        return legacy
    for binding in wallet_mod.list_configured_providers(cfg):
        if binding.get("provider") == name:
            return dict(binding.get("config") or {})
    return legacy


def cmd_wallet_list(args) -> int:
    from ... import wallet as wallet_mod
    client = _client(args.workspace, getattr(args, "profile", None))
    entries = wallet_mod.readiness_report(
        client.config.data, workspace=client.config.paths.root,
    )
    active = ((client.config.data.get("wallet") or {}).get("provider") or "")
    rows: list[dict[str, Any]] = []
    for e in entries:
        r = e.get("readiness") or {}
        rows.append({
            "id": e["id"],
            "label": e["label"],
            "runtime": e.get("runtime", "python"),
            "active": e["id"] == active,
            "ready": bool(r.get("ready")),
            "missing": r.get("missing") or [],
        })
    _print({"active": active or None, "providers": rows})
    return 0


def cmd_wallet_status(args) -> int:
    from ... import wallet as wallet_mod
    client = _client(args.workspace, getattr(args, "profile", None))
    cfg = client.config.data
    name = (args.provider or (cfg.get("wallet") or {}).get("provider") or "").lower()
    if not name:
        _print({"provider": None, "ready": False,
                "reason": "no wallet provider selected; run `nerya wallet use <name>`."})
        return 0
    try:
        p = wallet_mod.build_provider(
            name,
            _provider_cfg(wallet_mod, cfg, name),
            workspace=client.config.paths.root,
        )
    except wallet_mod.WalletProviderNotFound as exc:
        _print({"ok": False, "reason": str(exc),
                "known": sorted(wallet_mod.PROVIDERS)})
        return 1
    except wallet_mod.WalletError as exc:
        # e.g. an unreadable vault — config/ops problem, not a traceback.
        _print({"ok": False, "provider": name, "ready": False,
                "error": "wallet_config_error", "reason": str(exc)})
        return 1
    _print(p.readiness().to_dict())
    return 0


def cmd_wallet_install_hint(args) -> int:
    from ... import wallet as wallet_mod
    entry = wallet_mod.PROVIDERS.get(args.provider)
    if not entry:
        _print({"error": "unknown_provider"})
        return 1
    _print({
        "provider": args.provider,
        "install_hint": entry.get("install_hint", ""),
        "links": entry.get("links", {}),
        "runtime": entry.get("runtime", "python"),
    })
    return 0


def cmd_wallet_use(args) -> int:
    from ... import wallet as wallet_mod
    client = _client(args.workspace, getattr(args, "profile", None))
    name = args.provider.lower()
    conf_path = client.config.paths.config
    existing = yaml_io.load(conf_path, default={}) or {}
    wallet = existing.get("wallet") or {}
    if name == "none":
        wallet["provider"] = None
    else:
        if name not in wallet_mod.PROVIDERS:
            _print({"ok": False, "error": "unknown_provider",
                    "known": sorted(wallet_mod.PROVIDERS)})
            return 1
        wallet["provider"] = name
    existing["wallet"] = wallet
    yaml_io.dump(conf_path, existing)
    _print({"ok": True, "provider": wallet["provider"]})
    return 0


def cmd_wallet_create(args) -> int:
    """``nerya wallet create metamask`` — mint a fresh agent wallet.

    Generates a BIP-39 seed with crypto-grade entropy, derives the
    MetaMask-visible address (m/44'/60'/0'/0/<index>), stores the seed
    in the workspace SecretVault, and writes the
    ``wallet.providers.<wallet_id>`` binding. The seed is printed ONCE
    unless ``--no-reveal`` is passed; the same words imported into
    MetaMask show the same address.
    """
    import os

    from ...security.secrets import SecretVault

    name = args.provider.lower()
    if name != "metamask":
        _print({"ok": False, "error": "create_not_supported",
                "reason": "`wallet create` currently supports metamask only",
                "provider": name})
        return 1
    from ...wallet.providers.metamask import derive_address, generate_seed

    client = _client(args.workspace, getattr(args, "profile", None))
    words = int(getattr(args, "words", None) or 12)
    index = int(getattr(args, "index", None) or 0)
    wallet_id = (getattr(args, "wallet_id", None) or f"metamask_{index}").strip()

    try:
        seed = generate_seed(words)
    except ValueError as exc:
        _print({"ok": False, "error": "invalid_words", "reason": str(exc)})
        return 1
    address = derive_address(seed, index)

    secret_name = f"metamask-{wallet_id}-seed"
    vault_path = client.config.paths.root / "vault" / "secrets.enc"
    SecretVault.open(vault_path).put(
        name=secret_name, value=seed, kind="mnemonic", scope=["wallet"],
        owner="operator:cli",
    )
    ref = f"vault://{secret_name}"

    conf_path = client.config.paths.config
    existing = yaml_io.load(conf_path, default={}) or {}
    wallet_cfg = existing.get("wallet") or {}
    providers = wallet_cfg.get("providers") or {}
    providers[wallet_id] = {
        "provider": "metamask",
        "label": f"MetaMask agent wallet (index {index})",
        "config": {
            "seed_ref": ref,
            "address_index": index,
            "address": address,
        },
    }
    wallet_cfg["providers"] = providers
    existing["wallet"] = wallet_cfg
    yaml_io.dump(conf_path, existing)

    out: dict[str, Any] = {
        "ok": True,
        "provider": "metamask",
        "wallet_id": wallet_id,
        "address": address,
        "address_index": index,
        "derivation_path": f"m/44'/60'/0'/0/{index}",
        "seed_ref": ref,
        "binding": f"wallet.providers.{wallet_id}",
        "note": (
            "seed stored in the workspace vault; import the same words "
            "into MetaMask to see the same address."
        ),
    }
    if not os.environ.get("NERYA_VAULT_PASSPHRASE"):
        out["passphrase_warning"] = (
            "NERYA_VAULT_PASSPHRASE is not set — the vault fell back to "
            "the built-in default passphrase (at-rest protection only; "
            "live trading stays blocked without a real passphrase)."
        )
    if not getattr(args, "no_reveal", False):
        out["seed"] = seed
        out["warning"] = (
            "SEED REVEALED ONCE — import it into MetaMask now and delete "
            "this terminal output; it is not shown again."
        )
    _print(out)
    return 0


def register(sub) -> None:
    wallet = sub.add_parser("wallet").add_subparsers(dest="wcmd", required=True)
    p = wallet.add_parser("list"); _add_ws(p)
    p.set_defaults(func=cmd_wallet_list)
    p = wallet.add_parser("status"); _add_ws(p)
    p.add_argument("--provider", default=None)
    p.set_defaults(func=cmd_wallet_status)
    p = wallet.add_parser("install-hint")
    p.add_argument("provider", choices=_valid_providers())
    _add_ws(p)
    p.set_defaults(func=cmd_wallet_install_hint)
    p = wallet.add_parser("use"); _add_ws(p)
    p.add_argument("provider", choices=_valid_providers() + ["none"])
    p.set_defaults(func=cmd_wallet_use)
    p = wallet.add_parser("create"); _add_ws(p)
    p.add_argument("provider", choices=["metamask"])
    p.add_argument("--words", type=int, default=12,
                   help="BIP-39 word count: 12/15/18/21/24 (default 12)")
    p.add_argument("--index", type=int, default=0,
                   help="BIP-44 address index m/44'/60'/0'/0/<index>")
    p.add_argument("--wallet-id", default=None,
                   help="Binding id under wallet.providers (default metamask_<index>)")
    p.add_argument("--no-reveal", action="store_true",
                   help="Do not print the seed (vault-only)")
    p.set_defaults(func=cmd_wallet_create)


__all__ = [
    "cmd_wallet_list", "cmd_wallet_status",
    "cmd_wallet_install_hint", "cmd_wallet_use", "cmd_wallet_create",
    "register",
]
