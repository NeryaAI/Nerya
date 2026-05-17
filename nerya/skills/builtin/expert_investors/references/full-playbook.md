<!-- nerya-skill-frontmatter-start -->
---
name: expert_investors
description: "Lazy-load for investment-research tasks that ask for named expert lenses, value-investing checklists, growth-quality review, macro allocation, or portfolio/risk sizing. Do not use for ordinary chat or fast market checks."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Expert Investor Lenses

Use this skill only when the user explicitly asks for investment
research, portfolio review, asset allocation, valuation, or an expert
investor lens such as Buffett-style value investing. Do not preload these
lenses into default prompts or team templates.

## Lazy-Load Rule

1. Start with the existing team or analyst role that fits the task.
2. Load this skill only after the task needs an expert lens.
3. Load `references/investor-lenses.md` only when you need the detailed
   checklists.
4. Treat named investors as analytical styles, not impersonation. Use
   labels such as "Buffett-style value lens" or "macro allocator lens".

## Workflow

1. Identify the requested asset, horizon, and decision type.
2. Pick one to three lenses; do not run every lens by default.
3. Separate facts, inference, and recommendation.
4. Require data freshness, valuation assumptions, invalidation, and
   position/risk limits before any actionable conclusion.
5. If the evidence is stale or unavailable, mark the result as a watchlist
   item rather than a buy/sell decision.

## Output Contract

Return a structured memo:

```json
{
  "asset": "symbol or portfolio",
  "horizon": "intraday|swing|6_12m|strategic",
  "lenses_used": ["value_compounder", "growth_quality"],
  "facts": [{"claim": "...", "source": "...", "as_of": "..."}],
  "inferences": [{"claim": "...", "depends_on": "..."}],
  "rating": "Buy|Overweight|Hold|Underweight|Sell|Watchlist",
  "position_guidance": {"size_range": "...", "horizon": "..."},
  "invalidation": ["..."],
  "review_triggers": ["..."],
  "data_gaps": ["..."],
  "confidence": 0.0
}
```

## References

- `references/investor-lenses.md` - distilled checklists for value,
  growth quality, macro allocation, quant risk, and expert-panel debates.
