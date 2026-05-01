"""Print the current portfolio snapshot as JSON.

Standalone CLI usage::

    python -m nerya.skills.builtin.trading.scripts.portfolio_summary \
        --json '{"workspace": "/path/to/workspace"}'

If ``workspace`` is omitted, the current working directory is used.

Output schema::

    {
      "workspace": "<abs path>",
      "accounts": [{id, mode, cash_usd, equity_usd, positions, ...}, ...],
      "totals": {"cash_usd": float, "equity_usd": float}
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def run(workspace: str | None = None) -> dict[str, Any]:
    """In-process entry point — return a portfolio snapshot dict."""

    from nerya.core.paths import WorkspacePaths
    from nerya.trading import portfolio as portfolio_mod

    root = Path(workspace).expanduser().resolve() if workspace else Path(os.getcwd()).resolve()
    paths = WorkspacePaths(root=root)
    summary = portfolio_mod.get_portfolio_summary(paths)
    return {"workspace": str(root), **summary}


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None,
                        help="JSON-encoded payload")
    parser.add_argument("--payload-file", dest="payload_file", default=None,
                        help="path to JSON file with the payload")
    parser.add_argument("--workspace", dest="workspace", default=None,
                        help="workspace root path (overrides payload)")
    args = parser.parse_args()

    payload = _load_payload(args)
    workspace = args.workspace or payload.get("workspace")

    try:
        result = run(workspace=workspace)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
