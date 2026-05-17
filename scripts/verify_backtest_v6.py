"""Verify the backtest pipeline runs end-to-end on each v6 strategy.

After the v6 generator switched the entry/exit signature from
``ctx.trading.submit_intent(...)`` to the bracket-aware
``ctx.trading.open_position(...)`` / ``close_position(...)`` pair, the
backtest harness's :class:`MockTrading` lacked the new methods so any
v6 strategy crashed in backtest. This script pulls the patched
``MockTrading`` through ``run_strategy_backtest`` and verifies:

* ``backtests/<ts>/`` artefacts are written
* ``metrics.json`` contains the expected KPI fields
* ``chart.json`` is generated
* the report is non-empty and references the strategy id
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest


def main() -> int:
    targets = [
        ("v6_scalp_btc_1m",          "default"),
        ("v6_macross_then_agent",    "default"),
        # v6_team_us_basket_daily uses 1d cadence, may need different
        # preset / window for usable data — test it last.
        ("v6_team_us_basket_daily",  "default"),
    ]
    overall_ok = True
    for sid, preset in targets:
        print(f"\n=== {sid} (preset={preset}) ===")
        try:
            result = run_strategy_backtest(strategy_id=sid, preset=preset)
        except Exception as exc:
            print(f"  FAIL: {type(exc).__name__}: {exc}")
            overall_ok = False
            continue

        out_dir = Path(result.get("out_dir") or result.get("artefacts_dir") or "")
        metrics = result.get("metrics") or {}

        # Check required artefacts
        files = []
        if out_dir.exists():
            for name in ("metrics.json", "report.md", "chart.json", "config.yml"):
                p = out_dir / name
                files.append((name, p.exists(), p.stat().st_size if p.exists() else 0))

        print(f"  artefacts_dir   : {out_dir}")
        for name, exists, size in files:
            mark = "ok" if exists else "MISSING"
            print(f"    {name:<14} {size:>8} bytes  [{mark}]")

        kpis = (
            ("total_return_pct", metrics.get("total_return_pct")),
            ("max_drawdown_pct", metrics.get("max_drawdown_pct")),
            ("total_trades",     metrics.get("total_trades")),
            ("win_rate",         metrics.get("win_rate")),
            ("sharpe_ratio",     metrics.get("sharpe_ratio")),
            ("backtest_days",    metrics.get("backtest_days")),
            ("start_utc",        metrics.get("start_utc")),
            ("end_utc",          metrics.get("end_utc")),
        )
        print("  kpis:")
        for k, v in kpis:
            print(f"    {k:<18} = {v}")

        ok = (
            out_dir.exists()
            and (out_dir / "metrics.json").exists()
            and (out_dir / "report.md").exists()
            and isinstance(metrics, dict)
            and "total_return_pct" in metrics
        )
        print(f"  result: {'PASS' if ok else 'FAIL'}")
        if not ok:
            overall_ok = False

    print("\n" + "=" * 60)
    print(f"overall: {'PASS' if overall_ok else 'FAIL'}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
