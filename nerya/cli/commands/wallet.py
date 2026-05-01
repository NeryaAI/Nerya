"""``nerya wallet`` subcommands.

List providers, check readiness, print install hints, and switch the
active provider. The provider list is imported lazily so missing
optional deps don't explode the whole CLI.
"""

from __future__ import annotations

from typing import Any

from .._common import _add_ws, _client, _print
from ...core import yaml_io


_VALID_PROVIDERS: tuple[str, ...] = (
    "self_custody", "okx_os", "bitget", "binance_agentic", "coinbase",
)


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
            name, ((cfg.get("wallet") or {}).get(name) or {}),
            workspace=client.config.paths.root,
        )
    except wallet_mod.WalletProviderNotFound as exc:
        _print({"ok": False, "reason": str(exc),
                "known": sorted(wallet_mod.PROVIDERS)})
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


def register(sub) -> None:
    wallet = sub.add_parser("wallet").add_subparsers(dest="wcmd", required=True)
    p = wallet.add_parser("list"); _add_ws(p)
    p.set_defaults(func=cmd_wallet_list)
    p = wallet.add_parser("status"); _add_ws(p)
    p.add_argument("--provider", default=None)
    p.set_defaults(func=cmd_wallet_status)
    p = wallet.add_parser("install-hint")
    p.add_argument("provider", choices=list(_VALID_PROVIDERS))
    _add_ws(p)
    p.set_defaults(func=cmd_wallet_install_hint)
    p = wallet.add_parser("use"); _add_ws(p)
    p.add_argument("provider", choices=list(_VALID_PROVIDERS) + ["none"])
    p.set_defaults(func=cmd_wallet_use)


__all__ = [
    "cmd_wallet_list", "cmd_wallet_status",
    "cmd_wallet_install_hint", "cmd_wallet_use",
    "register",
]
