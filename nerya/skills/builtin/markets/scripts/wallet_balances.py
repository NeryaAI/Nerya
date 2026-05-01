"""Per-wallet token balance reads.

Standalone CLI usage::

    python -m nerya.skills.builtin.markets.scripts.wallet_balances \\
        --json '{
            "provider": "self_custody",
            "chain": "ethereum",
            "address": "0xabc...",
            "tokens": ["native", "USDC"]
        }'

Routes through :func:`nerya.wallet.registry.build_provider` so the
script honours the configured wallet provider (``self_custody``,
``okx_os``, ``coinbase``, …). Provider-specific config (RPC URLs,
signer refs, base URLs) is read from ``nerya.yml`` ``wallet.<provider>``
just like the runtime expects.

Output schema::

    {
      "provider": str,
      "chain": str,
      "address": str,
      "balances": [
        {"token": str, "balance": float|str, "symbol": str|null,
         "decimals": int|null},
        ...
      ],
      "errors": [{"token": str, "error": str}, ...]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def run(
    *,
    provider: str,
    chain: str,
    address: str,
    tokens: list[str] | None = None,
    workspace: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not provider:
        return {"error": "provider is required"}
    if not address:
        return {"error": "address is required"}
    if not chain:
        return {"error": "chain is required"}

    from nerya.core.config import load_config
    from nerya.wallet.registry import build_provider

    root = Path(workspace).expanduser().resolve() if workspace else Path(os.getcwd()).resolve()
    cfg = load_config(workspace=root)
    provider_cfg = (cfg.get("wallet", {}) or {}).get(provider) or {}
    if extra:
        provider_cfg = {**provider_cfg, **extra}

    try:
        p = build_provider(provider, dict(provider_cfg), workspace=root)
    except Exception as exc:
        return {
            "provider": provider, "chain": chain, "address": address,
            "balances": [], "errors": [{"token": "*", "error": f"{type(exc).__name__}: {exc}"}],
        }

    tokens = tokens or ["native"]
    balances: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for tok in tokens:
        try:
            bal = p.get_balance(chain=chain, address=address, token=tok)
        except Exception as exc:
            errors.append({"token": tok, "error": f"{type(exc).__name__}: {exc}"})
            continue
        balances.append({
            "token": getattr(bal, "token", tok),
            "balance": getattr(bal, "balance", None),
            "symbol": getattr(bal, "symbol", None),
            "decimals": getattr(bal, "decimals", None),
        })
    return {
        "provider": provider, "chain": chain, "address": address,
        "balances": balances, "errors": errors,
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
    args = parser.parse_args()

    payload = _load_payload(args)
    workspace = args.workspace or payload.get("workspace")
    try:
        result = run(
            provider=str(payload.get("provider") or ""),
            chain=str(payload.get("chain") or ""),
            address=str(payload.get("address") or ""),
            tokens=payload.get("tokens") if isinstance(payload.get("tokens"), list) else None,
            workspace=workspace,
            extra=payload.get("extra") if isinstance(payload.get("extra"), dict) else None,
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
