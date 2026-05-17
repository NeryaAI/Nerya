"""Force one open_position + close_position via the SDK to verify the
full v6 trade pipeline (TradePlan -> RiskGate -> ExecutionEngine ->
PositionBook -> orders/fills/positions DB rows) actually fires end to
end in paper mode.

Run:
    cd Nerya && python scripts/verify_v6_force_open.py
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


def _counts(db_path: str) -> dict[str, int | str]:
    cn = sqlite3.connect(db_path)
    cur = cn.cursor()
    out: dict[str, int | str] = {}
    for tbl in ("orders", "fills", "positions", "position_shares",
                "risk_evaluations", "executor_runs", "order_events",
                "protection_rules"):
        try:
            out[tbl] = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            out[tbl] = "(missing)"
    cn.close()
    return out


def _tail(db_path: str, table: str, limit: int = 3) -> list[dict]:
    cn = sqlite3.connect(db_path)
    cn.row_factory = sqlite3.Row
    cur = cn.cursor()
    rows: list[dict] = []
    try:
        for r in cur.execute(f"SELECT * FROM {table} ORDER BY ROWID DESC LIMIT ?", (limit,)):
            rows.append({k: r[k] for k in r.keys()})
    except Exception as exc:
        rows.append({"_error": str(exc)})
    cn.close()
    return rows


def main() -> int:
    print("=" * 80)
    print("V6 force open + close — direct SDK trade pipeline smoke")
    print("=" * 80)

    config = load_config()
    client = InternalClient.from_config(config)
    db_path = str(config.paths.db)

    sid = "v6_scalp_btc_1m"
    market = "binance:BTCUSDT"
    account = "paper_main"

    pre = _counts(db_path)
    print("\nPre-counts:")
    for k, v in pre.items():
        print(f"  {k:<22} {v}")

    # --- 1. open_position ---------------------------------------------------
    print(f"\n[1/2] open_position long {market} via {sid} on {account}")
    try:
        open_resp = client.trading.open_position(
            strategy_id=sid,
            account_id=account,
            market=market,
            side="long",
            sizing={"method": "fixed_usd", "fixed_usd": 50.0},
            protection={
                "stop_loss": {"type": "pct", "value": 0.005},
                "take_profit": {"type": "pct", "value": 0.008},
            },
            confidence=0.7,
            reasoning_ref="v6_smoke_force_open",
            source="sdk",
        )
    except Exception as exc:
        print(f"  ! open_position raised: {type(exc).__name__}: {exc}")
        import traceback; traceback.print_exc()
        return 1
    print(f"  resp keys: {list(open_resp.keys())}")
    print(f"  ok: {open_resp.get('ok')}  decision: {open_resp.get('decision')}")
    if "executor" in open_resp:
        ex = open_resp["executor"] or {}
        print(f"  executor.kind={ex.get('kind')}  order_ids={ex.get('order_ids')}")
    if "error" in open_resp and open_resp.get("error"):
        print(f"  error: {open_resp['error']}")

    mid = _counts(db_path)
    print("\nPost-open delta:")
    for k in pre:
        try:
            d = int(mid[k]) - int(pre[k])
            if d:
                print(f"  {k:<22} {pre[k]:>5} -> {mid[k]:<5}  delta={d:+d}")
        except (TypeError, ValueError):
            pass

    # --- 2. close_position --------------------------------------------------
    print(f"\n[2/2] close_position {market}")
    try:
        close_resp = client.trading.close_position(
            strategy_id=sid,
            account_id=account,
            market=market,
            side="long",
            confidence=0.7,
            reasoning_ref="v6_smoke_force_close",
            source="sdk",
        )
    except Exception as exc:
        print(f"  ! close_position raised: {type(exc).__name__}: {exc}")
        import traceback; traceback.print_exc()
        return 1
    print(f"  resp keys: {list(close_resp.keys())}")
    print(f"  ok: {close_resp.get('ok')}  decision: {close_resp.get('decision')}")
    if "executor" in close_resp:
        ex = close_resp["executor"] or {}
        print(f"  executor.kind={ex.get('kind')}  order_ids={ex.get('order_ids')}")

    post = _counts(db_path)
    print("\nFinal delta (cumulative since pre):")
    for k in pre:
        try:
            d = int(post[k]) - int(pre[k])
            print(f"  {k:<22} {pre[k]:>5} -> {post[k]:<5}  delta={d:+d}")
        except (TypeError, ValueError):
            print(f"  {k:<22} {pre[k]} -> {post[k]}")

    # --- 3. show what landed in DB ------------------------------------------
    for tbl in ("orders", "fills", "positions", "position_shares", "protection_rules"):
        if isinstance(post.get(tbl), int) and post[tbl] > pre.get(tbl, 0):
            print(f"\nLatest 3 rows from {tbl}:")
            for r in _tail(db_path, tbl, 3):
                snippet = json.dumps(r, default=str, ensure_ascii=False)
                print(f"  {snippet[:300]}{'...' if len(snippet) > 300 else ''}")

    out_path = Path(os.path.expanduser("~/.nerya/journals/v6_force_open_verify.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "open_resp": open_resp,
        "close_resp": close_resp,
        "pre": pre, "mid": mid, "post": post,
    }, indent=2, default=str, ensure_ascii=False))
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
