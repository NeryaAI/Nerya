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

from nerya.skills.builtin.backtest.scripts.indicators import ema


def run(ctx) -> dict:
    market = "nasdaq:NVDA"
    candles = ctx.market.candles(market, timeframe="1d", limit=400)
    if len(candles) < 220:
        return {"decision": "HOLD", "reason": "warming up"}

    ema50 = ema(candles, 50)[-1]
    ema200 = ema(candles, 200)[-1]
    last_close = candles[-1]["close"]
    bullish = ema50 > ema200 and last_close > ema50

    pos = ctx.portfolio.position(market)
    have_pos = bool(pos)

    if have_pos:
        if ema50 < ema200:
            intent = ctx.trading.close_position(
                market=market, side="long", confidence=0.8,
                reasoning_ref=f"nvda_trend:{ctx.clock.now_iso()[:10]}:exit",
            )
            return {"decision": "EXIT",
                    "reason": "ema50<ema200",
                    "intent_id": intent.get("intent_id")}
        unreal_pct = (last_close - pos["avg_price"]) / pos["avg_price"] * 100
        if unreal_pct <= -8.0:
            intent = ctx.trading.close_position(
                market=market, side="long", confidence=0.8,
                reasoning_ref=f"nvda_trend:{ctx.clock.now_iso()[:10]}:stop",
            )
            return {"decision": "EXIT",
                    "reason": f"stop hit pnl%={unreal_pct:.1f}",
                    "intent_id": intent.get("intent_id")}
        return {"decision": "HOLD",
                "reason": "in position, filter bullish",
                "metrics": {"unreal_pct": unreal_pct}}

    if not bullish:
        return {"decision": "HOLD",
                "reason": "filter bearish",
                "metrics": {"ema50": ema50, "ema200": ema200}}

    envelope = ctx.subagents.run(
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
    verdict = envelope.get("output", envelope)
    decision = verdict.get("decision", "HOLD")
    confidence = float(verdict.get("confidence", 0.0))
    if decision != "ENTRY" or confidence < 0.6:
        return {"decision": "HOLD",
                "reason": f"subagent vetoed: decision={decision} conf={confidence:.2f}",
                "subagent": verdict}

    intent = ctx.trading.open_position(
        market=market, side="long", sizing=ctx.config.params["sizing"],
        protection=ctx.config.params.get("protection"), confidence=confidence,
        reasoning_ref=f"nvda_trend:{ctx.clock.now_iso()[:10]}:entry",
    )
    return {
        "decision": "ENTRY",
        "reason": f"filter+subagent agree (conf={confidence:.2f})",
        "intent_id": intent.get("intent_id"),
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

## Verification

Unit-test positive confirmation, veto, missing data, repeated signals, saved
percentage entries and settled-quantity exits. A fixture Agent verdict tests
branches only, never historical model decisions or profitability. Use native
`strategy_backtest` for supported saved-candidate replay; do not invent
`ctx.backtest_replay`. If historical Agent execution is not_run, label the
result as dispatch/input verification, not a zero-return or profitable strategy.

## Limits and verification

Read `position-sizing.md`. Persist the requested stop in params.protection and
respect the portfolio risk budget; an 8% stop on a 90% position risks roughly
7.2% NAV before costs/gaps, not 1%. Explicit lower risk budgets reduce allocation.
Never lift actual limits or import archetype dollar caps. Subagent stubs only
test dispatch/veto branches; their returns are NOT historical Agent performance.
Do not require a positive fixture Sharpe ratio or optimize against it.

## Common gotchas

- **Calling the subagent on every tick.** Cheap-filter first, subagent
  second. The subagent is a *gate*, not a primary signal.
- **Letting the subagent set position size.** Position size = limits +
  portfolio sizing helper, not LLM judgement.
- **Ignoring `confidence`.** A subagent that says ENTRY at confidence
  0.31 is hedging — treat it as HOLD.
- **No exit branch.** Trend strategies live or die on the *exit* — the
  EMA cross-down + stop are not optional.
