"""Runtime-facing subcommands: ``vault``, ``llm``, ``mcp``, ``acp``,
``cron``, ``dev``.

Grouped together because they all inspect or configure *runtime*
state — nothing here talks to an exchange directly.
"""

from __future__ import annotations

import json

from .._common import _add_ws, _client, _print


# ----------------------------------------------------------------- vault
def cmd_vault_create_secret(args) -> int:
    from ...security.secrets import SecretVault
    client = _client(args.workspace, getattr(args, "profile", None))
    vault = SecretVault.open(client.config.paths.vault_enc)
    scope = (args.scope or "").split(",") if args.scope else []
    meta = vault.put(name=args.name, value=args.value, kind=args.kind, scope=scope)
    _print({"name": meta.name, "kind": meta.kind, "scope": meta.scope,
            "preview": meta.preview, "fingerprint": meta.fingerprint})
    return 0


def cmd_vault_list(args) -> int:
    from ...security.secrets import SecretVault
    client = _client(args.workspace, getattr(args, "profile", None))
    vault = SecretVault.open(client.config.paths.vault_enc)
    _print([{"name": m.name, "kind": m.kind, "scope": m.scope,
             "preview": m.preview, "fingerprint": m.fingerprint}
            for m in vault.list()])
    return 0


# ----------------------------------------------------------------- llm
def cmd_llm_models_refresh(args) -> int:
    from ...llm.model_catalog import ModelCatalog
    client = _client(args.workspace, getattr(args, "profile", None))
    catalog = ModelCatalog(workspace=client.config.paths.root)
    tiers = client.config.get("llm.tiers") or {}
    doc = catalog.refresh(tiers=tiers)
    summary = {
        "updated_at": doc["updated_at"],
        "providers": {p: len(ms) for p, ms in (doc.get("providers") or {}).items()},
        "errors": doc.get("errors") or {},
        "cache_path": str(catalog.cache_path),
    }
    _print(summary)
    return 0


def cmd_llm_models_list(args) -> int:
    from ...llm.model_catalog import ModelCatalog
    client = _client(args.workspace, getattr(args, "profile", None))
    catalog = ModelCatalog(workspace=client.config.paths.root)
    if args.provider:
        _print({args.provider: catalog.list(args.provider)})
    else:
        _print(catalog.load())
    return 0


# ----------------------------------------------------------------- mcp
def cmd_mcp_serve(args) -> int:
    from ...mcp.server import serve
    serve(args.workspace, verbose=args.verbose)
    return 0


def cmd_mcp_list_tools(args) -> int:
    from ...mcp.tools import NeryaTools, tools_as_json
    tools = NeryaTools.boot(args.workspace)
    print(tools_as_json(tools))
    return 0


# ----------------------------------------------------------------- acp
def cmd_acp_serve(args) -> int:
    from ...acp.server import AcpServer, serve_stdio
    server = AcpServer.boot()
    serve_stdio(server)
    return 0


# ----------------------------------------------------------------- cron
def cmd_cron_list(args) -> int:
    from ...triggers.schedule import load_schedules
    client = _client(args.workspace, getattr(args, "profile", None))
    entries = load_schedules(client.config.paths)
    _print([
        {"id": e.id, "every_seconds": e.every_seconds, "kind": e.kind,
         "target": e.target, "strategy_id": e.strategy_id,
         "payload": e.payload}
        for e in entries
    ])
    return 0


def cmd_cron_run_once(args) -> int:
    from ...triggers.cron import CronScheduler
    client = _client(args.workspace, getattr(args, "profile", None))
    scheduler = CronScheduler(client.config, client.triggers_runtime)
    fired = scheduler.tick(now_ts=args.now_ts)
    _print({"fired": fired})
    return 0


# ----------------------------------------------------------------- dev
def cmd_dev_status(args) -> int:
    import os
    from ...core import devmode
    client = _client(args.workspace, getattr(args, "profile", None))
    root = client.config.paths.dev_logs
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    _print({
        "active": devmode.is_active(),
        "env_flag": bool(os.environ.get("NERYA_DEV_MODE")),
        "config_flag": bool(client.config.get("runtime.dev_mode")),
        "dir": str(root),
        "files": [{"name": f.name, "size": f.stat().st_size} for f in files],
    })
    return 0


def cmd_dev_tail(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    path = client.config.paths.dev_log(args.kind)
    if not path.exists():
        _print({"empty": True, "path": str(path)})
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-max(1, args.limit):]
    for line in tail:
        try:
            _print(json.loads(line))
        except Exception:
            print(line)
    return 0


def cmd_dev_clear(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    root = client.config.paths.dev_logs
    removed = 0
    if root.exists():
        for f in root.glob("*.jsonl"):
            f.unlink()
            removed += 1
    _print({"removed": removed, "dir": str(root)})
    return 0


def register(sub) -> None:
    # vault
    vault = sub.add_parser("vault").add_subparsers(dest="vcmd", required=True)
    p = vault.add_parser("create-secret"); _add_ws(p)
    p.add_argument("--name", required=True); p.add_argument("--value", required=True)
    p.add_argument("--kind", required=True); p.add_argument("--scope", default="")
    p.set_defaults(func=cmd_vault_create_secret)
    p = vault.add_parser("list"); _add_ws(p); p.set_defaults(func=cmd_vault_list)

    # llm
    llm = sub.add_parser("llm").add_subparsers(dest="lcmd", required=True)
    models = llm.add_parser("models").add_subparsers(dest="mcmd", required=True)
    p = models.add_parser("refresh"); _add_ws(p); p.set_defaults(func=cmd_llm_models_refresh)
    p = models.add_parser("list"); _add_ws(p)
    p.add_argument("--provider", default=None); p.set_defaults(func=cmd_llm_models_list)

    # mcp
    mcp = sub.add_parser("mcp").add_subparsers(dest="mcpcmd", required=True)
    p = mcp.add_parser("serve"); _add_ws(p)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_mcp_serve)
    p = mcp.add_parser("list-tools"); _add_ws(p); p.set_defaults(func=cmd_mcp_list_tools)

    # acp
    acp = sub.add_parser("acp").add_subparsers(dest="acpcmd", required=True)
    p = acp.add_parser("serve"); _add_ws(p); p.set_defaults(func=cmd_acp_serve)

    # cron
    cron = sub.add_parser("cron").add_subparsers(dest="croncmd", required=True)
    p = cron.add_parser("list"); _add_ws(p); p.set_defaults(func=cmd_cron_list)
    p = cron.add_parser("run-once"); _add_ws(p)
    p.add_argument("--now-ts", type=float, default=None)
    p.set_defaults(func=cmd_cron_run_once)

    # dev
    dev = sub.add_parser("dev").add_subparsers(dest="devcmd", required=True)
    p = dev.add_parser("status"); _add_ws(p); p.set_defaults(func=cmd_dev_status)
    p = dev.add_parser("tail"); _add_ws(p)
    p.add_argument("kind", choices=["http", "tool", "error", "note"],
                   default="http", nargs="?")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_dev_tail)
    p = dev.add_parser("clear"); _add_ws(p); p.set_defaults(func=cmd_dev_clear)


__all__ = [
    "cmd_vault_create_secret", "cmd_vault_list",
    "cmd_llm_models_refresh", "cmd_llm_models_list",
    "cmd_mcp_serve", "cmd_mcp_list_tools",
    "cmd_acp_serve",
    "cmd_cron_list", "cmd_cron_run_once",
    "cmd_dev_status", "cmd_dev_tail", "cmd_dev_clear",
    "register",
]
