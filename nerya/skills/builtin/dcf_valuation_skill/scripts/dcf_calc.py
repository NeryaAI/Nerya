"""Deterministic DCF math — projections, PV, sensitivity matrix.

Pure-Python, zero external deps. The agent should call this rather than
asking the LLM to do the arithmetic so the answer is stable, auditable,
and unit-testable.

CLI usage::

    python -m nerya.skills.builtin.dcf_valuation_skill.scripts.dcf_calc \\
        --json '{
          "fcf_base": 100000000000,
          "growth_rate": 0.10,
          "wacc": 0.09,
          "terminal_growth": 0.025,
          "total_debt": 100000000000,
          "cash": 50000000000,
          "current_investments": 0,
          "outstanding_shares": 15000000000,
          "current_price": 220.50
        }'

Output schema::

    {
      "ok": bool,
      "inputs": {...},
      "projections": [{"year": 1, "fcf": ..., "pv": ...}, ...],
      "terminal_value": float,
      "terminal_pv": float,
      "enterprise_value": float,
      "equity_value": float,
      "fair_value_per_share": float,
      "current_price": float | null,
      "upside_pct": float | null,
      "sensitivity": [[float, ...], ...],
      "sensitivity_axes": {"wacc": [...], "terminal_growth": [...]}
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .....data.equities import EquitiesClient, _build_setup_guidance

# Annual decay factors for years 2..5 (year 1 uses raw growth_rate).
_DECAY: tuple[float, ...] = (1.0, 0.95, 0.90, 0.85, 0.80)


def _require_financial_datasets_key() -> dict[str, Any] | None:
    if EquitiesClient().keys:
        return None
    return {
        "ok": False,
        "error": "Financial Datasets API key is not configured.",
        "dependency_guidance": _build_setup_guidance(
            action="set key for dcf_valuation",
            source_url="https://api.financialdatasets.ai",
        ),
    }


def _project_fcfs(*, fcf_base: float, growth_rate: float,
                  years: int = 5) -> list[float]:
    out: list[float] = []
    fcf = float(fcf_base)
    for i in range(years):
        decay = _DECAY[i] if i < len(_DECAY) else _DECAY[-1]
        g = growth_rate * decay
        fcf = fcf * (1.0 + g)
        out.append(fcf)
    return out


def _pv(value: float, *, rate: float, year: int) -> float:
    return value / ((1.0 + rate) ** year)


def _terminal_value(last_fcf: float, *, terminal_growth: float,
                    wacc: float) -> float:
    if wacc <= terminal_growth:
        # Avoid division by zero / negative
        return 0.0
    return (last_fcf * (1.0 + terminal_growth)) / (wacc - terminal_growth)


def _compute_one(
    *,
    fcf_base: float,
    growth_rate: float,
    wacc: float,
    terminal_growth: float,
    total_debt: float,
    cash: float,
    current_investments: float,
    outstanding_shares: float,
    years: int = 5,
) -> dict[str, Any]:
    fcfs = _project_fcfs(fcf_base=fcf_base, growth_rate=growth_rate, years=years)
    pvs = [_pv(f, rate=wacc, year=i + 1) for i, f in enumerate(fcfs)]
    tv = _terminal_value(fcfs[-1], terminal_growth=terminal_growth, wacc=wacc)
    tv_pv = _pv(tv, rate=wacc, year=years)

    enterprise_value = sum(pvs) + tv_pv
    net_debt = float(total_debt) - float(cash) - float(current_investments)
    equity_value = enterprise_value - net_debt
    fair_value_per_share = (
        equity_value / outstanding_shares if outstanding_shares else 0.0
    )

    return {
        "fcfs": fcfs,
        "pvs": pvs,
        "terminal_value": tv,
        "terminal_pv": tv_pv,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "fair_value_per_share": fair_value_per_share,
    }


def run(
    *,
    fcf_base: float,
    growth_rate: float,
    wacc: float,
    terminal_growth: float = 0.025,
    total_debt: float = 0.0,
    cash: float = 0.0,
    current_investments: float = 0.0,
    outstanding_shares: float = 0.0,
    current_price: float | None = None,
    years: int = 5,
) -> dict[str, Any]:
    if outstanding_shares <= 0:
        return {"ok": False, "error": "outstanding_shares must be > 0"}
    if wacc <= 0 or wacc <= terminal_growth:
        return {"ok": False,
                "error": f"wacc must be > 0 and > terminal_growth (got "
                         f"wacc={wacc}, terminal_growth={terminal_growth})"}
    key_missing = _require_financial_datasets_key()
    if key_missing is not None:
        return key_missing

    base = _compute_one(
        fcf_base=fcf_base, growth_rate=growth_rate, wacc=wacc,
        terminal_growth=terminal_growth, total_debt=total_debt, cash=cash,
        current_investments=current_investments,
        outstanding_shares=outstanding_shares, years=years,
    )

    projections = [
        {"year": i + 1, "fcf": fcf, "pv": pv}
        for i, (fcf, pv) in enumerate(zip(base["fcfs"], base["pvs"]))
    ]

    upside_pct = None
    if current_price and current_price > 0:
        upside_pct = (base["fair_value_per_share"] / current_price - 1.0) * 100.0

    # Sensitivity: 3 WACC × 3 terminal-growth, fair value per share.
    waccs = [round(wacc - 0.01, 4), round(wacc, 4), round(wacc + 0.01, 4)]
    tgs = [0.020, 0.025, 0.030]
    matrix: list[list[float]] = []
    for w in waccs:
        if w <= 0:
            row = [0.0 for _ in tgs]
        else:
            row = []
            for tg in tgs:
                if w <= tg:
                    row.append(0.0)
                    continue
                cell = _compute_one(
                    fcf_base=fcf_base, growth_rate=growth_rate,
                    wacc=w, terminal_growth=tg,
                    total_debt=total_debt, cash=cash,
                    current_investments=current_investments,
                    outstanding_shares=outstanding_shares, years=years,
                )
                row.append(round(cell["fair_value_per_share"], 4))
        matrix.append(row)

    # Validation checks.
    checks: list[dict[str, Any]] = []
    tv_ratio = (base["terminal_pv"] / base["enterprise_value"]
                if base["enterprise_value"] else 0.0)
    checks.append({
        "name": "terminal_value_ratio",
        "value": round(tv_ratio, 4),
        "ok": 0.40 <= tv_ratio <= 0.85,
        "detail": "terminal PV should be 40-85% of EV for mature companies",
    })

    return {
        "ok": True,
        "inputs": {
            "fcf_base": fcf_base, "growth_rate": growth_rate,
            "wacc": wacc, "terminal_growth": terminal_growth,
            "total_debt": total_debt, "cash": cash,
            "current_investments": current_investments,
            "outstanding_shares": outstanding_shares,
            "current_price": current_price, "years": years,
        },
        "projections": projections,
        "terminal_value": base["terminal_value"],
        "terminal_pv": base["terminal_pv"],
        "enterprise_value": base["enterprise_value"],
        "equity_value": base["equity_value"],
        "fair_value_per_share": base["fair_value_per_share"],
        "current_price": current_price,
        "upside_pct": upside_pct,
        "sensitivity": matrix,
        "sensitivity_axes": {"wacc": waccs, "terminal_growth": tgs},
        "checks": checks,
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
            fcf_base=float(payload["fcf_base"]),
            growth_rate=float(payload["growth_rate"]),
            wacc=float(payload["wacc"]),
            terminal_growth=float(payload.get("terminal_growth") or 0.025),
            total_debt=float(payload.get("total_debt") or 0.0),
            cash=float(payload.get("cash") or 0.0),
            current_investments=float(payload.get("current_investments") or 0.0),
            outstanding_shares=float(payload.get("outstanding_shares") or 0.0),
            current_price=(float(payload["current_price"])
                           if payload.get("current_price") is not None
                           else None),
            years=int(payload.get("years") or 5),
        )
    except KeyError as exc:
        sys.stderr.write(f"missing required field: {exc}\n")
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
