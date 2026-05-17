"""Phase L live verifier — bootstrap the actual workspace and assert
that the denied yahoo tool is gone from the registry.

This is in-process (no LLM, no network beyond list_tools). It exercises
the full path: yaml → MCPServerConfig → bootstrap → attach_mcp_adapters
→ register_external_mcp_tools → ToolRegistry. If the denied tool name
appears in the registry, this verifier fails loudly.

Usage::

    python -m scripts.verify_phase_l_filter --workspace ~/.nerya
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
from nerya.mcp.connectors.bootstrap import bootstrap_mcp_connectors  # noqa: E402
from nerya.tools.registry import ToolRegistry  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True)
    args = p.parse_args()

    ws = Path(args.workspace).expanduser().resolve()
    paths = WorkspacePaths(root=ws)

    registry = ToolRegistry()
    diagnostics = bootstrap_mcp_connectors(
        registry=registry,
        executor=None,  # no-executor path; tests / CLI compatible
        resource_index=None,
        paths=paths,
    )

    denied_target = "mcp__yahoo__get_historical_stock_prices"
    denied_present = registry.find(denied_target) is not None

    yahoo_survivors = sorted(
        d.name for d in registry.list_tools()
        if d.name.startswith("mcp__yahoo__")
    )
    edgar_count = sum(
        1 for d in registry.list_tools() if d.name.startswith("mcp__edgar__")
    )
    coingecko_count = sum(
        1 for d in registry.list_tools()
        if d.name.startswith("mcp__coingecko__")
    )

    out = {
        "workspace": str(ws),
        "declared_servers": diagnostics.total_declared,
        "enabled_servers": diagnostics.total_enabled,
        "results": [
            {"server_id": r.server_id, "tool_count": r.tool_count,
             "error": r.error}
            for r in diagnostics.results
        ],
        "yahoo_survivors": yahoo_survivors,
        "edgar_tool_count": edgar_count,
        "coingecko_tool_count": coingecko_count,
        "denied_target": denied_target,
        "denied_present_in_registry": denied_present,
        "verdict": "PASS" if not denied_present else "FAIL",
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if not denied_present else 2


if __name__ == "__main__":
    sys.exit(main())
