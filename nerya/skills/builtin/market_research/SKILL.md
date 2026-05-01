<!-- nerya-skill-frontmatter-start -->
---
name: market_research
description: "Use for professional market or single-name research. Combines technicals, fundamentals, news, sentiment, macro, flows, and risk into an evidence-backed view before any trade recommendation or report."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Market Research Playbook

This skill turns raw data into a disciplined market view. It is adapted
for Nerya's skill-first runtime from the analyst-team pattern used by
TradingAgents and the research-desk presets used by Vibe-Trading.

## Analyst Stack

Use the stack that matches the asset:

| Dimension | Use When | Output |
|---|---|---|
| Technical | Any liquid traded instrument | trend, momentum, volatility, levels, invalidation |
| Fundamentals | equities, ETFs, credit, protocols, commodities with supply data | quality, growth, valuation, catalysts |
| News / sentiment | headline-driven or event-driven markets | impact, credibility, time horizon, crowding |
| Macro | rates, FX, commodities, indices, long-horizon equity views | cycle stage, policy impulse, cross-asset pressure |
| Flow / positioning | crypto, ETFs, futures, crowded equities | funding, basis, OI, ETF flow, on-chain movement |
| Risk | every actionable view | sizing, stops, tail scenarios, data gaps |

## Workflow

1. Load `market_data_routing` first if source choice is not obvious.
2. Pull only the data needed for the decision horizon. Intraday views
   need current market state; strategic reports need longer history.
3. Build the four core reads:
   - technical regime: trend, momentum, volatility, volume/liquidity,
   - fundamental or protocol state: quality, growth, valuation,
   - event context: news, sentiment, catalysts, calendar,
   - risk context: downside scenario, invalidation, position sizing.
4. Cross-check contradictions. A bullish headline with deteriorating
   price-volume action should be labelled mixed, not forced bullish.
5. State the view as a thesis, not a slogan. Include what would change
   the view.

## Output Contract

Return a structured memo:

```json
{
  "asset": "symbol or market",
  "time_horizon": "intraday|swing|medium_term|strategic",
  "stance": "bullish|bearish|neutral|mixed",
  "confidence": 0.0,
  "technical": {"summary": "...", "levels": {"support": [], "resistance": []}},
  "fundamental": {"summary": "...", "valuation": "..."},
  "event_sentiment": {"summary": "...", "catalysts": []},
  "macro_flow": {"summary": "...", "pressure": "tailwind|headwind|neutral"},
  "risk": {"invalidation": "...", "positioning": "...", "tail_risks": []},
  "evidence": [{"claim": "...", "source": "...", "as_of": "..."}],
  "data_gaps": []
}
```

## Professional Standards

- Separate observed facts, model inference, and recommendation.
- Use explicit time horizons. A bullish 12-month valuation view can
  coexist with a bearish intraday setup.
- Do not recommend a trade without invalidation, expected holding
  horizon, and data freshness.
- Use "unknown" for unavailable numbers. Do not backfill from memory.
- When evidence is weak or stale, lower confidence instead of adding
  more prose.
