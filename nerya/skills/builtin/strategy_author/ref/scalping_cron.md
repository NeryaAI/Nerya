# Archetype: Scalping cron

Goal: a one-minute (or sub-minute) tick that reads top-of-book, applies
a deterministic rule, and either submits a small order or holds.
Latency-sensitive; no subagent calls inside the tick.

## When this archetype fits

- High-cadence venues with cheap reads (Binance perp, A-share via
  level-1 quote).
- The decision is a **mechanical rule**: one or two indicators, no
  qualitative reasoning required.
- USD per trade is small enough that a missed fill is acceptable.

If any of these break (e.g. you'd want news context, or the venue
quote is expensive to pull), this is *not* the right archetype —
look at `trend_follow_subagent.md` or `news_track_filter.md`.

## strategy.yml shape

```yaml
strategy_id: btcusdt_zh_scalp
title: BTCUSDT Asia-session scalp (RSI mean-reversion)
mode: paper
accounts: [paper_main]
markets: ["binance:BTCUSDT"]
trigger_kinds: [schedule]
triggers:
  - kind: schedule
    schedule:
      cron: "*/1 * * * *"          # every minute
      timezone: UTC
      jitter_ms: 250
    payload:
      tf: "1m"
subagents: []                      # NO subagents — adds latency
tuning:
  enabled: false
```

## main.py shape

```python
"""1m RSI(14) mean-reversion scalp on BTCUSDT.

Entry rule:  RSI(14) < 25 AND last_close > VWAP_30m  → LONG 0.001 BTC
Exit rule:   RSI(14) > 70 OR  unrealised_pct >= 0.4  → CLOSE
Hold:        every other case
"""

from __future__ import annotations


def run(ctx) -> dict:
    market = "binance:BTCUSDT"
    candles = ctx.market_data.candles(market, tf="1m", limit=120)
    if len(candles) < 60:
        return {"decision": "HOLD", "reason": "warming up"}

    rsi = ctx.market_data.indicators.rsi(candles, period=14)[-1]
    vwap30 = ctx.market_data.indicators.vwap(candles[-30:])
    last_close = candles[-1]["close"]

    pos = ctx.portfolio.position(market)
    have_pos = pos is not None and abs(pos.qty) > 1e-8

    if not have_pos and rsi < 25 and last_close > vwap30:
        intent = ctx.trading.submit_intent({
            "market": market,
            "side": "buy",
            "qty": 0.001,
            "type": "market",
            "dedup_key": f"scalp:{ctx.now().isoformat(timespec='minutes')}",
        })
        return {
            "decision": "ENTRY",
            "reason": f"rsi={rsi:.1f} < 25 and px>{vwap30:.0f}",
            "intent_id": intent.get("id"),
            "metrics": {"rsi": rsi, "vwap30": vwap30, "px": last_close},
        }

    if have_pos:
        unreal = (last_close - pos.avg_price) / pos.avg_price * 100
        if rsi > 70 or unreal >= 0.4:
            intent = ctx.trading.submit_intent({
                "market": market,
                "side": "sell",
                "qty": pos.qty,
                "type": "market",
                "dedup_key": f"scalp:{ctx.now().isoformat(timespec='minutes')}:exit",
            })
            return {
                "decision": "EXIT",
                "reason": f"rsi={rsi:.1f} or pnl%={unreal:.2f}",
                "intent_id": intent.get("id"),
                "metrics": {"rsi": rsi, "unreal_pct": unreal},
            }

    return {"decision": "HOLD",
            "reason": "no edge", "metrics": {"rsi": rsi}}
```

## Backtest harness (required)

```python
# tests/test_main.py
from main import run

def test_replay_paper(make_ctx):
    ctx = make_ctx(window_days=14, tf="1m")
    stats = ctx.backtest_replay(run, fee_bps=2.0, slippage_bps=1.0)
    assert stats["wins"] + stats["losses"] >= 5, "too few trades"
    assert stats["max_drawdown_pct"] <= ctx.limits.max_drawdown_pct
    assert stats["sharpe"] >= 0.0, "no edge"
```

## limits.yml

```yaml
max_single_order_usd: 200
max_total_exposure_usd: 500
daily_loss_usd: 25
max_drawdown_pct: 5.0
min_confidence: 0.0          # rule-based; no LLM confidence
max_slippage_bps: 30
max_stale_seconds: 5         # 1m tick + 5s slack
approval_threshold_usd: 50   # below this auto-fills in paper mode
kill_switch: false
```

## Common gotchas

- **Calling subagents inside the tick.** A 60s tick that waits 8s for
  a subagent has burned 13% of its budget; just use rules.
- **Forgetting `dedup_key`.** Without it, retries / late triggers
  double-submit. Always include the minute-of-tick.
- **Logging the whole candle history.** Log only the indicators you
  actually decided on; everything else fattens the journal.
