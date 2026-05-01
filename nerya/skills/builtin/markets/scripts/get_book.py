"""Top-of-book / depth snapshot for a market.

Standalone CLI usage::

    python -m nerya.skills.builtin.markets.scripts.get_book \\
        --json '{"market": "binance:BTC/USDT"}'

Returns the venue's order-book snapshot tagged with a truth envelope
(see :mod:`nerya.core.truth`). Most public connectors only surface
top-of-book (``bid`` / ``ask`` / ``mid``); deeper books require the
venue's WebSocket — when that's needed, write a custom script and
document the choice.

Output schema::

    {
      "market": str, "venue": str,
      "bid": float, "ask": float, "mid": float,
      "spread_bps": int, "ts_ms": int,
      "_envelope": {...}
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from nerya.data.orderbook import build_snapshot

from ._connector import public_connector, venue_of


def run(*, market: str, workspace: str | None = None) -> dict[str, Any]:
    if not market:
        return {"error": "market is required"}
    venue = venue_of(market)
    try:
        conn = public_connector(market, workspace=workspace)
    except Exception:
        conn = None
    snap = build_snapshot(market=market, connector=conn)
    snap.setdefault("market", market)
    snap.setdefault("venue", venue)
    return snap


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
    parser.add_argument("--market", dest="market", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    market = args.market or payload.get("market") or ""
    workspace = args.workspace or payload.get("workspace")
    try:
        result = run(market=market, workspace=workspace)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
