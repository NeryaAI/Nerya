"""End-to-end live-tick verification: trigger one paper-mode tick of the
generated v6_scalp_btc_1m strategy through the SDK (the same code path
POST /strategies/runtime/run_tick uses) and confirm the full trading
pipeline executes — TradeIntent built → RiskGate evaluated → executor
produced a fill → PositionBook applied → orders/fills/positions rows
written.

Run:
    cd Nerya && python scripts/verify_v6_live_tick.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.core.config import load_config  # noqa: E402
from nerya.sdk import InternalClient  # noqa: E402


def _db_snapshot(db_path: str) -> dict:
    cn = sqlite3.connect(db_path)
    cur = cn.cursor()
    out: dict = {}
    for tbl in ("orders", "fills", "positions", "position_shares", "risk_evaluations", "executor_runs", "order_events"):
        try:
            cnt = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            out[tbl] = cnt
        except sqlite3.OperationalError:
            out[tbl] = "(missing)"
    cn.close()
    return out


def _tail_rows(db_path: str, table: str, limit: int = 3) -> list:
    cn = sqlite3.connect(db_path)
    cn.row_factory = sqlite3.Row
    cur = cn.cursor()
    rows: list = []
    try:
        for r in cur.execute(f"SELECT * FROM {table} ORDER BY ROWID DESC LIMIT ?", (limit,)):
            rows.append({k: r[k] for k in r.keys()})
    except Exception as exc:
        rows.append({"_error": str(exc)})
    cn.close()
    return rows


def main() -> int:
    print("=" * 80)
    print("V6 live-tick verification — v6_scalp_btc_1m (paper, in-process)")
    print("=" * 80)

    config = load_config()
    client = InternalClient.from_config(config)

    db_path = str(config.paths.db)
    sid = "v6_scalp_btc_1m"

    print(f"\nDB: {db_path}")
    print(f"Strategy: {sid}")
    print()

    pre = _db_snapshot(db_path)
    print("Pre-tick row counts:")
    for k, v in pre.items():
        print(f"  {k:<22} {v}")

    # Run one tick. Mirror what /strategies/runtime/run_tick passes.
    t0 = time.time()
    try:
        record = client.strategy.run_tick(
            sid,
            trigger_payload={
                "source": "v6_e2e_smoke",
                "kind": "operator.run_now",
                "market": "binance:BTCUSDT",
                "timeframe": "1m",
            },
            operator="v6_smoke",
            note="v6 e2e live-tick smoke",
        )
    except Exception as exc:
        print(f"\n! run_tick raised: {type(exc).__name__}: {exc}")
        import traceback; traceback.print_exc()
        return 1

    elapsed = time.time() - t0
    print(f"\nrun_tick returned in {elapsed:.2f}s.")
    print(f"  outcome: {record.get('outcome') or record.get('status')}")
    if isinstance(record, dict):
        for k in ("strategy_id", "tick_id", "intent_id", "order_id", "fill_id", "status",
                  "outcome", "skipped_reason", "error", "decision"):
            if k in record:
                print(f"  {k:<18} {record.get(k)}")
        # Surface the trace shape if present
        for k in ("trace", "events", "result"):
            if k in record and record[k]:
                snippet = json.dumps(record[k], default=str, ensure_ascii=False)[:600]
                print(f"  {k}[:600]: {snippet}")

    print("\nPost-tick row counts (delta):")
    post = _db_snapshot(db_path)
    delta_summary = {}
    for k in pre:
        try:
            d = int(post[k]) - int(pre[k])
            delta_summary[k] = d
            print(f"  {k:<22} {pre[k]:>5} -> {post[k]:<5}  delta={d:+d}")
        except (TypeError, ValueError):
            print(f"  {k:<22} {pre[k]} -> {post[k]}")

    # Show the freshest rows in the tables that gained rows.
    for tbl in ("orders", "fills", "positions", "position_shares", "risk_evaluations", "order_events"):
        d = delta_summary.get(tbl) or 0
        if d > 0:
            print(f"\nLatest {min(3, d)} rows from {tbl}:")
            for r in _tail_rows(db_path, tbl, limit=min(3, d)):
                snippet = json.dumps(r, default=str, ensure_ascii=False)
                print(f"  {snippet[:240]}{'...' if len(snippet)>240 else ''}")

    out_path = Path(os.path.expanduser("~/.nerya/journals/v6_live_tick_verify.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "elapsed_s": elapsed,
        "record": record,
        "pre_counts": pre,
        "post_counts": post,
        "deltas": delta_summary,
    }, indent=2, default=str, ensure_ascii=False))
    print(f"\nReport written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
