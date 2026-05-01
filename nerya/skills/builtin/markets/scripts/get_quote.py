"""Single-symbol quote on a named venue.

Standalone CLI usage::

    python -m nerya.skills.builtin.markets.scripts.get_quote \\
        --json '{"market": "binance:BTC/USDT"}'

Output schema::

    {
      "market": str,
      "venue": str,
      "bid": float | null,
      "ask": float | null,
      "mid": float | null,
      "last": float | null,
      "ts_ms": int,
      "envelope": {"truth": "live"|"mock"|"degraded", "venue": str, ...},
      "error": str | null
    }

A ``degraded`` envelope means the venue is unavailable and mock mode
is not authorised. Treat the price as **missing**, not zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from nerya.core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
)

from ._connector import public_connector, venue_of


def run(*, market: str, workspace: str | None = None) -> dict[str, Any]:
    venue = venue_of(market)
    if not market:
        return {"error": "market is required"}
    try:
        conn = public_connector(market, workspace=workspace)
    except Exception as exc:
        env = degraded_envelope(
            "ticker", error=f"connector_unavailable:{type(exc).__name__}",
            venue=venue,
        ).as_dict()
        return {
            "market": market, "venue": venue,
            "bid": None, "ask": None, "mid": None, "last": None, "ts_ms": 0,
            "envelope": env, "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        tk = conn.get_ticker(market)
    except Exception as exc:
        env = degraded_envelope(
            "ticker", error=f"connector_error:{type(exc).__name__}",
            venue=venue,
        ).as_dict()
        return {
            "market": market, "venue": venue,
            "bid": None, "ask": None, "mid": None, "last": None, "ts_ms": 0,
            "envelope": env, "error": f"{type(exc).__name__}: {exc}",
        }
    if venue in ("mock", "paper", ""):
        env = mock_envelope(source="mock", venue=venue or "mock").as_dict()
    else:
        env = live_envelope(source=venue, venue=venue).as_dict()
    return {
        "market": tk.market,
        "venue": tk.venue,
        "bid": tk.bid, "ask": tk.ask, "mid": tk.mid, "last": tk.last,
        "ts_ms": tk.ts_ms,
        "envelope": env,
        "error": None,
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
