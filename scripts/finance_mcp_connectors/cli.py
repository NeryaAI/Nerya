"""CLI implementation — list / materialize / doctor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from nerya.core.paths import WorkspacePaths
from nerya.mcp.connectors import (
    HttpTransportConfig,
    MCPServerConfig,
    StdioTransportConfig,
    bootstrap_mcp_connectors,
    build_adapter_for_server,
    ensure_mcp_servers_config,
    load_mcp_servers_config,
)
from nerya.mcp.connectors.bootstrap import VaultResolver
from nerya.mcp.transports import OAuthTokenCache


def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _paths_from_args(args: argparse.Namespace) -> WorkspacePaths:
    workspace = Path(args.workspace).expanduser().resolve()
    return WorkspacePaths(root=workspace)


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


def cmd_materialize(args: argparse.Namespace) -> int:
    """Write the seed stub if missing. Always idempotent."""

    paths = _paths_from_args(args)
    target = paths.connectors_mcp_servers
    written = ensure_mcp_servers_config(target)
    _emit_json(
        {
            "subcommand": "materialize",
            "workspace": str(paths.root),
            "target": str(target),
            "written_new_file": written,
            "note": (
                "wrote default mcp_servers.yml stub" if written
                else "file already exists; left alone"
            ),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _transport_summary(
    transport: HttpTransportConfig | StdioTransportConfig,
) -> dict[str, Any]:
    if isinstance(transport, HttpTransportConfig):
        return {
            "kind": "http",
            "url": transport.url,
            "timeout_seconds": transport.timeout_seconds,
            "extra_header_count": len(transport.extra_headers),
        }
    return {
        "kind": "stdio",
        "command": list(transport.command),
        "cwd": transport.cwd,
        "startup_timeout": transport.startup_timeout,
        "read_timeout": transport.read_timeout,
        "env_var_count": len(transport.env_refs),
    }


def _auth_summary(auth: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": auth.kind}
    if auth.kind == "bearer_static":
        out["token_ref"] = auth.token_ref.as_str() if auth.token_ref else None
    elif auth.kind == "oauth_client_credentials":
        out["client_id"] = auth.client_id
        out["client_id_ref"] = auth.client_id_ref.as_str() if auth.client_id_ref else None
        out["client_secret_ref"] = (
            auth.client_secret_ref.as_str() if auth.client_secret_ref else None
        )
        out["token_url"] = auth.token_url
        out["token_url_ref"] = auth.token_url_ref.as_str() if auth.token_url_ref else None
        out["scope"] = auth.scope
        out["audience"] = auth.audience
    return out


def cmd_list(args: argparse.Namespace) -> int:
    """Pretty-print the catalogue for the operator."""

    paths = _paths_from_args(args)
    cfg_path = paths.connectors_mcp_servers
    if not cfg_path.exists() and args.materialize_if_missing:
        ensure_mcp_servers_config(cfg_path)

    cfg = load_mcp_servers_config(cfg_path)

    rows = [
        {
            "id": s.id,
            "namespace": s.namespace,
            "enabled": s.enabled,
            "transport": _transport_summary(s.transport),
            "auth": _auth_summary(s.auth),
            "notes": s.notes,
        }
        for s in cfg.servers
    ]
    _emit_json(
        {
            "subcommand": "list",
            "workspace": str(paths.root),
            "config_path": str(cfg_path),
            "exists": cfg_path.exists(),
            "version": cfg.version,
            "total": len(cfg.servers),
            "enabled": len(cfg.enabled_servers()),
            "servers": rows,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor_one(
    server_cfg: MCPServerConfig,
    *,
    paths: WorkspacePaths,
    vault: VaultResolver,
    token_cache: OAuthTokenCache,
) -> dict[str, Any]:
    """Build the adapter, run ``list_tools()``, capture tool count + error."""

    row: dict[str, Any] = {
        "id": server_cfg.id,
        "enabled": server_cfg.enabled,
        "transport_kind": (
            "http" if isinstance(server_cfg.transport, HttpTransportConfig) else "stdio"
        ),
        "auth_kind": server_cfg.auth.kind,
        "tool_count": 0,
        "tool_names_sample": [],
        "error": None,
    }

    try:
        adapter = build_adapter_for_server(
            server_cfg, vault=vault, token_cache=token_cache,
        )
    except Exception as exc:
        row["error"] = f"build_adapter: {type(exc).__name__}: {exc}"
        return row

    try:
        tools = adapter.client.list_tools()
    except Exception as exc:
        row["error"] = f"list_tools: {type(exc).__name__}: {exc}"
        # For stdio adapters we should still close the subprocess.
        close_fn = getattr(adapter.client, "close", None)
        if close_fn is not None:
            try:
                close_fn()
            except Exception:
                pass
        return row

    row["tool_count"] = len(tools)
    row["tool_names_sample"] = [
        str(t.get("name") or "") for t in tools[:5] if isinstance(t, dict)
    ]

    close_fn = getattr(adapter.client, "close", None)
    if close_fn is not None:
        try:
            close_fn()
        except Exception:
            pass

    return row


def cmd_doctor(args: argparse.Namespace) -> int:
    """Probe each enabled server for reachability + tool surface."""

    paths = _paths_from_args(args)
    cfg_path = paths.connectors_mcp_servers
    if not cfg_path.exists():
        if args.materialize_if_missing:
            ensure_mcp_servers_config(cfg_path)
        else:
            _emit_json(
                {
                    "subcommand": "doctor",
                    "workspace": str(paths.root),
                    "error": (
                        f"{cfg_path} does not exist; run `... materialize "
                        f"--workspace {paths.root}` first"
                    ),
                }
            )
            return 2

    cfg = load_mcp_servers_config(cfg_path)
    vault = VaultResolver(paths=paths, passphrase=args.vault_passphrase)
    token_cache = OAuthTokenCache(cache_path=paths.connectors_oauth_cache)

    targets: list[MCPServerConfig]
    if args.server:
        target_cfg = cfg.by_id(args.server)
        if target_cfg is None:
            _emit_json(
                {
                    "subcommand": "doctor",
                    "error": f"unknown server id {args.server!r}; declared: "
                    f"{[s.id for s in cfg.servers]}",
                }
            )
            return 2
        targets = [target_cfg]
    elif args.all:
        targets = list(cfg.servers)
    else:
        targets = cfg.enabled_servers()

    results = [
        _doctor_one(s, paths=paths, vault=vault, token_cache=token_cache)
        for s in targets
    ]
    summary = {
        "subcommand": "doctor",
        "workspace": str(paths.root),
        "config_path": str(cfg_path),
        "probed": len(results),
        "ok": sum(1 for r in results if r["error"] is None),
        "failed": sum(1 for r in results if r["error"] is not None),
        "results": results,
    }
    _emit_json(summary)
    return 0 if summary["failed"] == 0 else 1


# ---------------------------------------------------------------------------
# Top-level argparse
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="finance_mcp_connectors",
        description=(
            "Operator CLI for the workspace-track MCP connector catalogue. "
            "Pairs with the finance_skills_importer (Phase D) — finance skills "
            "are useless without at least one reachable MCP server."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="show declared servers + status")
    p_list.add_argument("--workspace", required=True, help="absolute path to workspace root")
    p_list.add_argument(
        "--materialize-if-missing", action="store_true",
        help="write the seed stub if mcp_servers.yml does not exist yet",
    )
    p_list.set_defaults(func=cmd_list)

    p_materialize = sub.add_parser(
        "materialize", help="write the default mcp_servers.yml stub if missing",
    )
    p_materialize.add_argument("--workspace", required=True)
    p_materialize.set_defaults(func=cmd_materialize)

    p_doctor = sub.add_parser(
        "doctor", help="probe enabled servers with list_tools()",
    )
    p_doctor.add_argument("--workspace", required=True)
    p_doctor.add_argument(
        "--server", default=None,
        help="probe only this server id (default: every enabled server)",
    )
    p_doctor.add_argument(
        "--all", action="store_true",
        help="probe every declared server, not just enabled ones",
    )
    p_doctor.add_argument(
        "--vault-passphrase", default=None,
        help="passphrase for SecretVault (defaults to NERYA_VAULT_PASSPHRASE env)",
    )
    p_doctor.add_argument(
        "--materialize-if-missing", action="store_true",
        help="write the seed stub if mcp_servers.yml does not exist yet",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
