# Archetype: Scalping cron

Goal: a one-minute (or sub-minute) tick that reads top-of-book, applies
a deterministic rule, and either submits a percentage-sized order or holds.
Latency-sensitive; no subagent calls inside the tick.

## When this archetype fits

- High-cadence venues with cheap reads (Binance perp, A-share via
  level-1 quote).
- The decision is a **mechanical rule**: one or two indicators, no
  qualitative reasoning required.
- The requested risk budget and liquidity support the position size and cadence.

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
params:
  sizing: {method: pct_nav, pct_nav: 0.90}  # one slot; see position-sizing.md
policy:
  max_open_positions: 1
  max_single_order_usd: 0
  max_daily_notional_usd: 0
backtest:
  max_open_trades: 1
  stake_amount: {mode: unlimited}
tuning:
  enabled: false
```

## main.py shape

```python
"""1m RSI(14) mean-reversion scalp on BTCUSDT.

Entry rule:  RSI(14) < 25 AND last_close > VWAP_30m → LONG with saved NAV sizing
Exit rule:   RSI(14) > 70 OR  unrealised_pct >= 0.4  → CLOSE
Hold:        every other case
"""

from __future__ import annotations

from nerya.skills.builtin.backtest.scripts.indicators import rsi


def run(ctx) -> dict:
    market = "binance:BTCUSDT"
    candles = ctx.market.candles(market, timeframe="1m", limit=120)
    if len(candles) < 60:
        return {"decision": "HOLD", "reason": "warming up"}

    rsi_value = rsi(candles, 14)[-1]
    vwap30 = (sum(float(c["close"]) for c in candles[-30:]) / 30)
    last_close = candles[-1]["close"]

    pos = ctx.portfolio.position(market)
    have_pos = bool(pos)

    if not have_pos and rsi_value is not None and rsi_value < 25 and last_close > vwap30:
        intent = ctx.trading.open_position(
            market=market,
            side="long",
            sizing=ctx.config.params["sizing"],
            protection=ctx.config.params.get("protection"),
            confidence=0.8,
            reasoning_ref=f"scalp:{ctx.clock.now_iso()[:16]}",
        )
        return {
            "decision": "ENTRY",
            "reason": f"rsi={rsi_value:.1f} < 25 and px>{vwap30:.0f}",
            "intent_id": intent.get("intent_id"),
            "metrics": {"rsi": rsi_value, "vwap30": vwap30, "px": last_close},
        }

    if have_pos:
        unreal = (last_close - pos["avg_price"]) / pos["avg_price"] * 100
        if (rsi_value is not None and rsi_value > 70) or unreal >= 0.4:
            intent = ctx.trading.close_position(
                market=market,
                side="long",
                confidence=0.8,
                reasoning_ref=f"scalp:{ctx.clock.now_iso()[:16]}:exit",
            )
            return {
                "decision": "EXIT",
                "reason": f"rsi={rsi_value:.1f} or pnl%={unreal:.2f}",
                "intent_id": intent.get("intent_id"),
                "metrics": {"rsi": rsi_value, "unreal_pct": unreal},
            }

    return {"decision": "HOLD",
            "reason": "no edge", "metrics": {"rsi": rsi_value}}
```

## Verification

Unit fixtures must exercise entry, hold, exit, duplicate suppression and the
saved percentage sizing. Then use the native `strategy_backtest` call for the
saved candidate with real historical data and requested fees. There is no
`ctx.backtest_replay` helper to invent inside the strategy. Report trade counts,
exposure and net performance; do not make a positive Sharpe ratio a unit-test
requirement or silently change thresholds to manufacture passing trades.

## Limits and verification

Read `position-sizing.md`. Preserve operator/account limits; do not copy small
USD caps or approval thresholds from an archetype. Declare an appropriate stop
in params.protection before use. The signal above is illustrative, not a mandate
to combine rare filters or promise a positive Sharpe ratio. Unit fixtures test
entry/hold/exit and percentage sizing; only recorded historical replay provides
performance evidence. A losing replay is not permission to tune until it passes.

## Common gotchas

- **Calling subagents inside the tick.** A 60s tick that waits 8s for
  a subagent has burned 13% of its budget; just use rules.
- **Forgetting `dedup_key`.** Without it, retries / late triggers
  double-submit. Always include the minute-of-tick.
- **Logging the whole candle history.** Log only the indicators you
  actually decided on; everything else fattens the journal.
