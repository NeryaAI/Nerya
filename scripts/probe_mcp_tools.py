"""Probe specific live MCP servers and dump their full tool catalogue.

Usage::

    python -m scripts.probe_mcp_tools --workspace ~/.nerya --server yahoo_finance
    python -m scripts.probe_mcp_tools --workspace ~/.nerya --server coingecko

Phase L-0 helper: we need the exact tool names to write a precise
``deny_tools`` list in ``mcp_servers.yml``.

This is a one-shot probe — it does not modify any state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nerya.core.paths import WorkspacePaths  # noqa: E402
from nerya.mcp.connectors.bootstrap import (  # noqa: E402
    VaultResolver,
    build_adapter_for_server,
)
from nerya.mcp.connectors.config import load_mcp_servers_config  # noqa: E402
from nerya.mcp.transports.oauth import OAuthTokenCache  # noqa: E402


def _maybe_close(client) -> None:
    close_fn = getattr(client, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True)
    p.add_argument("--server", required=True, help="server id in mcp_servers.yml")
    args = p.parse_args()

    ws = Path(args.workspace).expanduser().resolve()
    paths = WorkspacePaths(root=ws)
    cfg = load_mcp_servers_config(paths.connectors_mcp_servers)
    server = cfg.by_id(args.server)
    if server is None:
        print(f"ERROR: server {args.server!r} not in {paths.connectors_mcp_servers}",
              file=sys.stderr)
        return 2
    if not server.enabled:
        print(f"WARN: server {args.server!r} is disabled in yaml; probing anyway",
              file=sys.stderr)

    vault = VaultResolver(paths=paths)
    token_cache = OAuthTokenCache(cache_path=paths.connectors_oauth_cache)
    adapter = build_adapter_for_server(server, vault=vault, token_cache=token_cache)
    try:
        tools = list(adapter.client.list_tools() or [])
    finally:
        _maybe_close(adapter.client)

    out = {
        "server_id": server.id,
        "namespace": server.namespace,
        "tool_count": len(tools),
        "tools": [
            {
                "name": str(t.get("name") or ""),
                "description": (str(t.get("description") or "")[:200]),
                "input_keys": sorted(
                    list(((t.get("inputSchema") or {}).get("properties") or {}).keys())
                ),
            }
            for t in tools
            if isinstance(t, dict)
        ],
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
