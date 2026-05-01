"""Run the risk gate against a proposed trade intent.

Standalone CLI usage::

    python -m nerya.skills.builtin.trading.scripts.risk_check \\
        --json '{"intent": {"strategy_id": "demo", "account_id": "paper",
                            "market": "binance:BTC/USDT", "side": "buy",
                            "size": 100, "size_unit": "usd",
                            "order_type": "market"}}'

The payload accepts either a fully-formed ``TradeIntent`` (use the
runtime's ``intent_id``) or a partial dict — the script fills the
required fields with a generated id and a reasoning string before
running the gate. ``market_snapshot`` is optional and forwarded to
``RiskGate.evaluate`` so the gate can convert ``base/quote`` sizes to
USD when needed.

Output schema mirrors :class:`nerya.trading.risk.RiskDecision.asdict`,
with the resolved ``intent`` echoed under ``intent`` so the caller
can journal both halves together::

    {
      "intent": {...TradeIntent.asdict()...},
      "decision": {"intent_id": "...", "decision": "allow|reject|escalate",
                   "reasons": [...], ...}
    }

Exit code is always ``0`` if the gate ran (even when it returns
``reject``) — the caller decides what to do with the verdict.
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
    intent: dict[str, Any],
    workspace: str | None = None,
    market_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from nerya.core.config import load_config
    from nerya.trading.intents import TradeIntent
    from nerya.trading.risk import RiskGate

    root = (
        Path(workspace).expanduser().resolve()
        if workspace
        else Path(os.getcwd()).resolve()
    )
    cfg = load_config(workspace=root)

    spec = dict(intent)
    spec.setdefault("reasoning", "risk_check.py CLI invocation")
    spec.setdefault("source", "script")
    if "intent_id" not in spec:
        ti = TradeIntent.new(**spec)
    else:
        ti = TradeIntent(**spec)

    decision = RiskGate(cfg).evaluate(ti, market_snapshot=market_snapshot)
    return {"intent": ti.asdict(), "decision": decision.asdict()}


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
    intent = payload.get("intent") or {}
    if not isinstance(intent, dict) or not intent:
        sys.stderr.write("payload.intent is required (dict)\n")
        raise SystemExit(2)

    workspace = args.workspace or payload.get("workspace")
    market_snapshot = payload.get("market_snapshot")
    try:
        result = run(
            intent=intent,
            workspace=workspace,
            market_snapshot=market_snapshot if isinstance(market_snapshot, dict) else None,
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
