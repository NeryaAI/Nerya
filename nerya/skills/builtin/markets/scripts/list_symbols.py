"""Trade-able universe + min size / tick metadata for a venue.

Standalone CLI usage::

    python -m nerya.skills.builtin.markets.scripts.list_symbols \\
        --json '{"venue": "binance"}'

Implementation: use ccxt directly when available — Nerya's connector
base does not expose a uniform ``list_symbols`` (each venue surfaces
metadata differently), and ccxt's ``load_markets()`` already
normalises the bits the agent typically cares about (id, base,
quote, precision, limits.min/max). For venues without ccxt support,
the script falls back to the venue connector's ``list_markets``
attribute when present and otherwise returns an empty list with a
clear error.

Output schema::

    {
      "venue": str,
      "count": int,
      "symbols": [
        {
          "symbol": str, "base": str, "quote": str,
          "active": bool, "type": str,
          "limits": {"amount": {"min": float|null, "max": float|null},
                     "price":  {"min": float|null, "max": float|null}},
          "precision": {"amount": int|null, "price": int|null},
        },
        ...
      ],
      "error": str | null
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def run(*, venue: str, workspace=None) -> dict[str, Any]:
    if not venue:
        return {"error": "venue is required", "symbols": [], "count": 0}
    venue = venue.lower()
    from nerya.connectors.provider_spec import get_registry
    registry = get_registry(workspace or Path.cwd())
    spec = registry.find(venue)
    if spec and spec.supports.get('public_discovery'):
        try:
            symbols = registry.build({'venue': spec.id, 'live': False}).list_markets(limit=20)
            return {'venue':spec.id,'symbols':symbols,'count':len(symbols)}
        except Exception as exc:
            return {'venue':spec.id,'symbols':[],'count':0,'error':str(exc)}

    try:
        import ccxt  # type: ignore
    except Exception:
        return {
            "venue": venue,
            "count": 0,
            "symbols": [],
            "error": "ccxt is not installed; install ccxt to enumerate venue symbols",
        }

    klass = getattr(ccxt, venue, None)
    if klass is None:
        return {
            "venue": venue,
            "count": 0,
            "symbols": [],
            "error": f"ccxt has no exchange named {venue!r}",
        }
    try:
        exchange = klass({"enableRateLimit": True})
        markets = exchange.load_markets() or {}
    except Exception as exc:
        return {
            "venue": venue,
            "count": 0,
            "symbols": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    out: list[dict[str, Any]] = []
    for sym, m in markets.items():
        if not isinstance(m, dict):
            continue
        limits = m.get("limits") or {}
        amount = limits.get("amount") or {}
        price = limits.get("price") or {}
        precision = m.get("precision") or {}
        out.append({
            "symbol": sym,
            "id": m.get("id"),
            "base": m.get("base"),
            "quote": m.get("quote"),
            "active": bool(m.get("active", True)),
            "type": m.get("type") or m.get("contractType") or "spot",
            "limits": {
                "amount": {"min": amount.get("min"), "max": amount.get("max")},
                "price":  {"min": price.get("min"),  "max": price.get("max")},
            },
            "precision": {
                "amount": precision.get("amount"),
                "price":  precision.get("price"),
            },
        })

    return {
        "venue": venue,
        "count": len(out),
        "symbols": out,
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
    parser.add_argument("--venue", dest="venue", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    venue = args.venue or payload.get("venue") or ""
    try:
        result = run(venue=venue)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
