"""Aggregate metrics for a single strategy id.

Standalone CLI usage::

    python -m nerya.skills.builtin.trading.scripts.strategy_report \\
        --json '{"strategy_id": "demo"}'

Reads:

* the strategy spec (status, limits, lifecycle state),
* recent risk decisions + errors from the workspace journals,
* the per-strategy ledgers in ``strategies/<id>/history/`` (trigger
  → decision → intent → risk → order → fill → pnl rolls).

Output schema::

    {
      "strategy_id": "demo",
      "spec": {...Strategy.asdict()...},
      "risk_summary": {...summarize_risk(...)...},
      "errors": {...summarize_errors(...)...},
      "ledger_counts": {"triggers": int, "decisions": int, ...},
      "active_version_id": "v_..." or null
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


_LEDGERS = (
    "triggers",
    "skill_calls",
    "subagents",
    "decisions",
    "intents",
    "risk",
    "orders",
    "fills",
    "pnl",
    "messages",
    "reviews",
)


def run(*, strategy_id: str, workspace: str | None = None) -> dict[str, Any]:
    from nerya.core.paths import WorkspacePaths
    from nerya.evolution.journal_analyzer import (
        summarize_errors,
        summarize_risk,
    )
    from nerya.strategy_history import store as history_store
    from nerya.trading import strategies as strategies_mod
    from nerya.trading import strategy_versions as versions_mod

    root = (
        Path(workspace).expanduser().resolve()
        if workspace
        else Path(os.getcwd()).resolve()
    )
    paths = WorkspacePaths(root=root)

    try:
        spec = strategies_mod.load_strategy(paths, strategy_id).asdict()
    except Exception as exc:
        return {
            "strategy_id": strategy_id,
            "error": f"strategy_unknown: {type(exc).__name__}: {exc}",
        }

    risk = summarize_risk(paths, strategy_id) or {}
    errors = summarize_errors(paths) or {}

    ledger_counts: dict[str, int] = {}
    for name in _LEDGERS:
        try:
            rows = history_store.read_ledger(paths, strategy_id, name)
        except Exception:
            rows = []
        ledger_counts[name] = len(rows)

    try:
        active_version = versions_mod.active_version_id(paths, strategy_id)
    except Exception:
        active_version = None

    return {
        "strategy_id": strategy_id,
        "spec": spec,
        "risk_summary": risk,
        "errors": errors,
        "ledger_counts": ledger_counts,
        "active_version_id": active_version,
    }


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
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--workspace", dest="workspace", default=None)
    parser.add_argument("--strategy-id", dest="strategy_id", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    strategy_id = args.strategy_id or payload.get("strategy_id")
    if not strategy_id:
        sys.stderr.write("strategy_id is required\n")
        raise SystemExit(2)

    workspace = args.workspace or payload.get("workspace")

    try:
        result = run(strategy_id=strategy_id, workspace=workspace)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
