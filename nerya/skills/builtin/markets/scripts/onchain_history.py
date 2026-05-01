"""Recent on-chain transfers involving an address.

Standalone CLI usage::

    python -m nerya.skills.builtin.markets.scripts.onchain_history \\
        --json '{"chain": "ethereum", "address": "0xabc...",
                 "limit": 20, "min_native_amount": 0}'

Routes through :func:`nerya.data.onchain.fetch_whale_events` because
that helper already implements:

* RPC scan (no API key) for EVM + Solana,
* Etherscan-family Scan API scan when ``NERYA_SCAN_KEY`` (or chain-
  specific ``NERYA_SCAN_KEY_<CHAIN>``) is set,
* truth envelopes (``live`` / ``mock`` / ``degraded``) on every event,
* watch-address filtering on the Scan API path.

The script wires ``watch_addresses=[address]`` so the result set is
bounded to the requested wallet. ``min_native_amount`` defaults to
``0`` here (rather than 100) because address-history typically wants
the long tail, not just whales.

Output schema::

    {
      "chain": str,
      "address": str,
      "events": [...],
      "count": int,
      "envelope": {...}  # taken from the first event when present
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def run(
    *,
    chain: str,
    address: str,
    limit: int = 20,
    min_native_amount: float = 0.0,
    blocks_to_scan: int = 200,
    rpc_url: str | None = None,
    api_key: str = "",
) -> dict[str, Any]:
    if not chain:
        return {"error": "chain is required"}
    if not address:
        return {"error": "address is required"}

    from nerya.data.onchain import fetch_whale_events

    events = fetch_whale_events(
        chain,
        limit=int(limit),
        min_native_amount=float(min_native_amount),
        blocks_to_scan=int(blocks_to_scan),
        rpc_url=rpc_url,
        api_key=api_key,
        watch_addresses=[address],
    )
    envelope: dict[str, Any] = {}
    if events and isinstance(events[0], dict):
        envelope = events[0].get("_envelope") or {}
    return {
        "chain": chain,
        "address": address,
        "events": events,
        "count": len(events),
        "envelope": envelope,
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
    args = parser.parse_args()

    payload = _load_payload(args)
    try:
        result = run(
            chain=str(payload.get("chain") or ""),
            address=str(payload.get("address") or ""),
            limit=int(payload.get("limit") or 20),
            min_native_amount=float(payload.get("min_native_amount") or 0.0),
            blocks_to_scan=int(payload.get("blocks_to_scan") or 200),
            rpc_url=payload.get("rpc_url") or None,
            api_key=str(payload.get("api_key") or ""),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
