"""CLI wrapper around :class:`nerya.data.equities.EquitiesClient`.

Fetches one or more financial statements / metric surfaces for a single
ticker in a single bounded pass. Designed to be invoked by the agent via
``run_shell``; never persists results, never logs the API key.

CLI usage::

    python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_financials \\
        --json '{"ticker": "AAPL",
                  "statements": ["income", "balance", "cashflow", "snapshot"],
                  "period": "annual", "limit": 5}'

Supported ``statements`` values:

| value                | endpoint                              |
|----------------------|---------------------------------------|
| ``income``           | /financials/income-statements/        |
| ``balance``          | /financials/balance-sheets/           |
| ``cashflow``         | /financials/cash-flow-statements/     |
| ``all``              | /financials/                          |
| ``snapshot``         | /financial-metrics/snapshot           |
| ``historical_metrics`` | /financial-metrics/                 |
| ``analyst_estimates`` | /analyst-estimates/                  |
| ``segments``         | /financials/segments/                 |
| ``earnings``         | /earnings/                            |
| ``insider_trades``   | /insider-trades/                      |
| ``filings``          | /filings/                             |
| ``company_facts``    | /company/facts                        |

Output schema::

    {
      "ok": bool,
      "ticker": str,
      "statements": {
        "<name>": {
          "data": {...},                 # stripped payload
          "source_url": "...",
          "_envelope": {...}             # mode: live | unavailable | mock
        }
      },
      "source_urls": [str],
      "data_gaps": [str],   # statements whose envelope mode != "live"
      "elapsed_ms": int
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .....data.equities import EquitiesClient


_DISPATCH: dict[str, str] = {
    "income": "income_statements",
    "income_statements": "income_statements",
    "balance": "balance_sheets",
    "balance_sheets": "balance_sheets",
    "cashflow": "cash_flow_statements",
    "cash_flow": "cash_flow_statements",
    "cash_flow_statements": "cash_flow_statements",
    "all": "all_statements",
    "snapshot": "metrics_snapshot",
    "metrics_snapshot": "metrics_snapshot",
    "historical_metrics": "historical_metrics",
    "key_ratios": "historical_metrics",
    "analyst_estimates": "analyst_estimates",
    "segments": "segments",
    "earnings": "earnings",
    "insider_trades": "insider_trades",
    "filings": "filings",
    "company_facts": "company_facts",
    "facts": "company_facts",
}


def _apply_dependency_guidance(payload: dict[str, Any], *,
                              statement: str) -> dict[str, Any] | None:
    env = payload.get("_envelope") or {}
    if not isinstance(env, dict):
        return None
    if env.get("missing_key") or "Financial Datasets API key is not configured" in str(
        env.get("error") or "",
    ):
        guidance = env.get("setup_guidance")
        if isinstance(guidance, dict):
            payload["dependency_guidance"] = guidance
        payload["ok"] = False
        payload["error"] = str(env.get("error") or "dependency missing")
        payload["statement"] = statement
        return guidance if isinstance(guidance, dict) else {"statement": statement}
    return None


def run(
    *,
    ticker: str,
    statements: list[str],
    period: str = "annual",
    limit: int = 4,
) -> dict[str, Any]:
    if not ticker:
        return {"ok": False, "error": "ticker is required"}
    if not statements:
        return {"ok": False, "error": "statements is required"}

    client = EquitiesClient()
    started = time.monotonic()

    out: dict[str, Any] = {}
    source_urls: list[str] = []
    gaps: list[str] = []
    dependency_guidance: dict[str, Any] | None = None

    for name in statements:
        method_name = _DISPATCH.get(name.lower())
        if not method_name:
            out[name] = {
                "data": {},
                "_envelope": {"source": "unknown", "mode": "unavailable",
                              "error": f"unsupported statement: {name!r}"},
                "source_url": "",
            }
            gaps.append(name)
            continue
        method = getattr(client, method_name, None)
        if method is None:
            out[name] = {
                "data": {},
                "_envelope": {"source": "unknown", "mode": "unavailable",
                              "error": f"client method missing: {method_name!r}"},
                "source_url": "",
            }
            gaps.append(name)
            continue

        try:
            if method_name in (
                "income_statements", "balance_sheets",
                "cash_flow_statements", "all_statements",
                "segments",
            ):
                payload = method(ticker, period=period, limit=limit)
            elif method_name == "historical_metrics":
                payload = method(ticker, period=period, limit=max(limit, 8))
            elif method_name in ("metrics_snapshot", "company_facts"):
                payload = method(ticker)
            else:
                payload = method(ticker, limit=limit)
        except Exception as exc:  # noqa: BLE001
            payload = {
                "data": {},
                "_envelope": {"source": "financial_datasets",
                              "mode": "unavailable",
                              "error": f"{type(exc).__name__}: {exc}"},
                "source_url": "",
            }

        missing_guidance = _apply_dependency_guidance(
            payload, statement=name,
        )
        if dependency_guidance is None and missing_guidance is not None:
            dependency_guidance = missing_guidance

        out[name] = payload
        if payload.get("source_url"):
            source_urls.append(payload["source_url"])
        env = payload.get("_envelope") or {}
        if env.get("mode") != "live":
            gaps.append(name)

    return {
        "ok": not bool(dependency_guidance),
        "ticker": ticker,
        "statements": out,
        "dependency_guidance": dependency_guidance,
        "source_urls": source_urls,
        "data_gaps": gaps,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
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
    parser.add_argument("--ticker", dest="ticker", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    ticker = (args.ticker or payload.get("ticker") or "").strip().upper()
    statements_raw = payload.get("statements") or []
    if isinstance(statements_raw, str):
        statements = [s.strip() for s in statements_raw.split(",") if s.strip()]
    else:
        statements = [str(s).strip() for s in statements_raw if str(s).strip()]

    try:
        result = run(
            ticker=ticker,
            statements=statements,
            period=str(payload.get("period") or "annual"),
            limit=int(payload.get("limit") or 4),
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
