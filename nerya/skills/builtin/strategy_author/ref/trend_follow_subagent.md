# Archetype: Trend follow + subagent confirmation

Goal: a long-cadence (4h / daily) tick that runs a trend filter, then —
only when the filter signals — asks a subagent to confirm the regime
before submitting an order. The subagent is the qualitative second
opinion the rule-based filter cannot provide.

## When this archetype fits

- Daily / multi-hour swing strategies on equities, FX, perp.
- The entry rule is *necessary* but not *sufficient*: e.g. "EMA(50) >
  EMA(200)" must be combined with "no major macro headwind".
- USD per trade is large enough that one extra LLM call (~$0.01–0.05)
  is dwarfed by the position cost.

## strategy.yml shape

```yaml
strategy_id: nvda_trend_follow_d1
title: NVDA daily trend follower (EMA crossover + analyst veto)
mode: paper
accounts: [paper_main]
markets: ["nasdaq:NVDA"]
trigger_kinds: [schedule]
triggers:
  - kind: schedule
    schedule:
      cron: "5 14 * * 1-5"        # 14:05 UTC = 09:05 ET (post-open)
      timezone: UTC
    payload:
      tf: "1d"
subagents: [market_analyst]
tuning:
  enabled: true
  cadence: weekly
```

The package ships its own `market_analyst` prompt that overrides the
workspace default — see "Subagent prompt" below.

## main.py shape

```python
"""NVDA daily trend follower.

Filter:        EMA(50) > EMA(200) AND last_close > EMA(50) → bullish
Subagent vote: market_analyst confirms regime (decision in {ENTRY, HOLD})
Order:         enter only if filter bullish AND subagent says ENTRY with
               confidence >= 0.6
Exit:          EMA(50) crosses below EMA(200), OR stop_loss_pct breached
"""

from __future__ import annotations


def run(ctx) -> dict:
    market = "nasdaq:NVDA"
    candles = ctx.market_data.candles(market, tf="1d", limit=400)
    if len(candles) < 220:
        return {"decision": "HOLD", "reason": "warming up"}

    ema50 = ctx.market_data.indicators.ema(candles, period=50)[-1]
    ema200 = ctx.market_data.indicators.ema(candles, period=200)[-1]
    last_close = candles[-1]["close"]
    bullish = ema50 > ema200 and last_close > ema50

    pos = ctx.portfolio.position(market)
    have_pos = pos is not None and abs(pos.qty) > 1e-8

    if have_pos:
        if ema50 < ema200:
            intent = ctx.trading.submit_intent({
                "market": market, "side": "sell", "qty": pos.qty,
                "type": "market",
                "dedup_key": f"nvda_trend:{ctx.now().date().isoformat()}:exit",
            })
            return {"decision": "EXIT",
                    "reason": "ema50<ema200",
                    "intent_id": intent.get("id")}
        unreal_pct = (last_close - pos.avg_price) / pos.avg_price * 100
        if unreal_pct <= -ctx.limits.stop_loss_pct:
            intent = ctx.trading.submit_intent({
                "market": market, "side": "sell", "qty": pos.qty,
                "type": "market",
                "dedup_key": f"nvda_trend:{ctx.now().date().isoformat()}:stop",
            })
            return {"decision": "EXIT",
                    "reason": f"stop hit pnl%={unreal_pct:.1f}",
                    "intent_id": intent.get("id")}
        return {"decision": "HOLD",
                "reason": "in position, filter bullish",
                "metrics": {"unreal_pct": unreal_pct}}

    if not bullish:
        return {"decision": "HOLD",
                "reason": "filter bearish",
                "metrics": {"ema50": ema50, "ema200": ema200}}

    verdict = ctx.subagents.dispatch(
        "market_analyst",
        payload={
            "market": market,
            "tf": "1d",
            "ask": "Confirm bullish regime; decision must be ENTRY or HOLD with confidence 0..1.",
            "candles": candles[-60:],
            "ema50": ema50,
            "ema200": ema200,
        },
    )
    decision = verdict.get("decision", "HOLD")
    confidence = float(verdict.get("confidence", 0.0))
    if decision != "ENTRY" or confidence < 0.6:
        return {"decision": "HOLD",
                "reason": f"subagent vetoed: decision={decision} conf={confidence:.2f}",
                "subagent": verdict}

    qty = ctx.portfolio.size_by_risk(
        market, risk_usd=ctx.limits.max_single_order_usd,
        stop_pct=ctx.limits.stop_loss_pct,
    )
    intent = ctx.trading.submit_intent({
        "market": market, "side": "buy", "qty": qty, "type": "market",
        "dedup_key": f"nvda_trend:{ctx.now().date().isoformat()}:entry",
    })
    return {
        "decision": "ENTRY",
        "reason": f"filter+subagent agree (conf={confidence:.2f})",
        "intent_id": intent.get("id"),
        "subagent": verdict,
    }
```

## Subagent prompt (`subagents/market_analyst.agent.md`)

```markdown
# NVDA Daily Regime Confirmer

You are a single-asset market analyst for NVDA on the daily timeframe.

## Inputs
- `market`, `tf`
- `candles`: last 60 daily bars (OHLCV)
- `ema50`, `ema200` snapshots
- `ask`: the question

## Output (JSON only)
```json
{
  "decision": "ENTRY" | "HOLD",
  "confidence": 0..1,
  "drivers": ["..."],
  "veto_reason": "string or null"
}
```

## Method
1. Walk the last 60 bars; note any swing-low / swing-high structure.
2. Check earnings / macro headlines via `news_social.recent({market, days:7})`.
3. Output ENTRY only if structure + macro both bullish.
```

## Backtest harness (required, data is available)

```python
# tests/test_main.py
from main import run

def test_replay_with_subagent_stub(make_ctx):
    ctx = make_ctx(window_days=365, tf="1d",
                   subagent_stub={"decision": "ENTRY", "confidence": 0.7})
    stats = ctx.backtest_replay(run, fee_bps=1.0, slippage_bps=2.0)
    assert stats["sharpe"] >= 0.3, "no edge with optimistic subagent"
    assert stats["max_drawdown_pct"] <= ctx.limits.max_drawdown_pct

def test_replay_subagent_pessimistic(make_ctx):
    """When subagent always vetoes, strategy never trades."""
    ctx = make_ctx(window_days=365, tf="1d",
                   subagent_stub={"decision": "HOLD", "confidence": 0.9})
    stats = ctx.backtest_replay(run)
    assert stats["wins"] + stats["losses"] == 0
```

## limits.yml

```yaml
max_single_order_usd: 5000
max_total_exposure_usd: 15000
daily_loss_usd: 500
max_drawdown_pct: 12.0
stop_loss_pct: 8.0
min_confidence: 0.6
max_slippage_bps: 50
max_stale_seconds: 600       # daily tick — generous staleness OK
approval_threshold_usd: 1000
kill_switch: false
```

## Common gotchas

- **Calling the subagent on every tick.** Cheap-filter first, subagent
  second. The subagent is a *gate*, not a primary signal.
- **Letting the subagent set position size.** Position size = limits +
  portfolio sizing helper, not LLM judgement.
- **Ignoring `confidence`.** A subagent that says ENTRY at confidence
  0.31 is hedging — treat it as HOLD.
- **No exit branch.** Trend strategies live or die on the *exit* — the
  EMA cross-down + stop are not optional.
