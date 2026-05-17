"""List recent SEC filings for a ticker via Financial Datasets API.

CLI usage::

    python -m nerya.skills.builtin.sec_filings_skill.scripts.list_filings \\
        --json '{"ticker": "AAPL", "form": "10-K", "limit": 5}'

Output schema::

    {
      "ok": bool,
      "ticker": "AAPL",
      "form": "10-K",
      "count": 5,
      "filings": [
        {"accession_number": "...", "form": "10-K",
         "filing_date": "...", "report_period": "...",
         "url": "..."}, ...
      ],
      "source_url": "...",
      "_envelope": {...}
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .....data.equities import EquitiesClient


def run(*, ticker: str, form: str | None = None,
        limit: int = 10) -> dict[str, Any]:
    if not ticker:
        return {"ok": False, "error": "ticker is required"}
    client = EquitiesClient()
    payload = client.filings(ticker, form=form, limit=limit)
    env = payload.get("_envelope") or {}
    dependency_guidance = None
    if isinstance(env, dict) and (
        env.get("missing_key")
        or "Financial Datasets API key is not configured" in str(env.get("error") or "")
    ):
        dependency_guidance = env.get("setup_guidance")
        if isinstance(dependency_guidance, dict):
            payload["dependency_guidance"] = dependency_guidance
        return {
            "ok": False,
            "ticker": ticker.upper(),
            "form": form,
            "dependency_guidance": dependency_guidance,
            "error": str(env.get("error") or "dependency missing"),
            "_envelope": payload.get("_envelope"),
            "source_url": payload.get("source_url"),
        }

    data = payload.get("data") or {}

    items_raw = data.get("filings") or data.get("items") or data
    if isinstance(items_raw, dict) and "filings" in items_raw:
        items_raw = items_raw["filings"]
    if not isinstance(items_raw, list):
        items_raw = []

    filings: list[dict[str, Any]] = []
    for it in items_raw[:limit]:
        if not isinstance(it, dict):
            continue
        filings.append({
            "accession_number": it.get("accession_number") or it.get("accession"),
            "form": it.get("form") or it.get("type"),
            "filing_date": it.get("filing_date") or it.get("filed_at"),
            "report_period": it.get("report_period") or it.get("period_of_report"),
            "url": it.get("url") or it.get("link") or "",
            "primary_document": it.get("primary_document"),
        })

    return {
        "ok": True,
        "ticker": ticker.upper(),
        "form": form,
        "count": len(filings),
        "filings": filings,
        "source_url": payload.get("source_url"),
        "_envelope": payload.get("_envelope"),
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
    parser.add_argument("--form", dest="form", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    ticker = (args.ticker or payload.get("ticker") or "").strip().upper()
    form = args.form or payload.get("form") or None
    if isinstance(form, str) and not form.strip():
        form = None

    try:
        result = run(
            ticker=ticker,
            form=form,
            limit=int(payload.get("limit") or 10),
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
