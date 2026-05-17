"""Probe BSC on-chain data primitives:
  1. fetch_whale_events('bsc')
  2. fetch_token_klines('bsc', CAKE address)
  3. get_onchain_price('bsc', CAKE address)

Each call should produce a non-empty result with envelope.error == "".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.data.onchain import fetch_whale_events
from nerya.data.onchain_klines import fetch_token_klines
from nerya.data.onchain_price import get_onchain_price


# Well-known BSC tokens (BEP-20 addresses):
# CAKE (PancakeSwap)
CAKE = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
# FLOKI (Floki Inu, BSC)
FLOKI = "0xfb5B838b6cfEEdC2873aB27866079AC55363D37E"
# BABYDOGE
BABYDOGE = "0xc748673057861a797275CD8A068AbB95A902e8de"


def probe_whale() -> None:
    print("\n=== 1) fetch_whale_events('bsc') ===")
    t0 = time.time()
    try:
        events = fetch_whale_events(
            "bsc",
            limit=20,
            min_native_amount=10.0,  # 10 BNB ~ ~$6k notional
            blocks_to_scan=80,        # ~4 min of BSC at 3s blocks
        )
    except Exception as exc:
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return
    dt = time.time() - t0
    print(f"  events returned: {len(events)} (in {dt:.1f}s)")
    if not events:
        print("  (empty list)")
        return
    env = events[0].get("_envelope") if isinstance(events[0], dict) else None
    print(f"  envelope: {env}")
    print("  sample events (first 3):")
    for ev in events[:3]:
        print(
            f"    block={ev.get('block')}  amount={ev.get('amount'):.2f} {ev.get('token')}  "
            f"from={(ev.get('wallet') or '')[:14]}..  to={(ev.get('to') or '')[:14]}..  "
            f"hash={(ev.get('tx_hash') or '')[:14]}.."
        )


def probe_klines() -> None:
    print("\n=== 2) fetch_token_klines('bsc', CAKE) ===")
    t0 = time.time()
    try:
        candles = fetch_token_klines("bsc", CAKE, interval="15m", limit=24)
    except Exception as exc:
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return
    dt = time.time() - t0
    env = None
    if isinstance(candles, list) and candles and isinstance(candles[0], dict):
        env = candles[0].get("_envelope")
    elif hasattr(candles, "_envelope"):
        env = candles._envelope
    rows = list(candles)
    print(f"  candles returned: {len(rows)} (in {dt:.1f}s)")
    if env:
        print(f"  envelope: {env}")
    print("  last 3 candles:")
    for row in rows[-3:]:
        if isinstance(row, dict):
            print(
                f"    ts={row.get('ts')}  o={row.get('open')}  h={row.get('high')}  "
                f"l={row.get('low')}  c={row.get('close')}  v={row.get('volume')}"
            )


def probe_price() -> None:
    print("\n=== 3) get_onchain_price('bsc', CAKE) ===")
    t0 = time.time()
    try:
        p = get_onchain_price("bsc", CAKE)
    except Exception as exc:
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return
    dt = time.time() - t0
    print(f"  price_usd      : {p.price_usd}")
    print(f"  liquidity_usd  : {p.liquidity_usd}")
    print(f"  pair_address   : {(p.pair_address or '')[:14]}..")
    print(f"  venue          : {p.venue}")
    print(f"  envelope       : {p.envelope}")
    print(f"  duration       : {dt:.1f}s")

    print("\n   --- FLOKI for comparison ---")
    p2 = get_onchain_price("bsc", FLOKI)
    print(f"  FLOKI price_usd: {p2.price_usd}  liq={p2.liquidity_usd}")
    p3 = get_onchain_price("bsc", BABYDOGE)
    print(f"  BABYDOGE price : {p3.price_usd}  liq={p3.liquidity_usd}")


def main() -> int:
    probe_price()      # cheapest, run first
    probe_klines()
    probe_whale()      # most expensive (BSC RPC scan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
