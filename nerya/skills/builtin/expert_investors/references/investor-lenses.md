# Expert Investor Lenses

These are lazy-loaded research lenses. They should be pulled only for
investment-research tasks that explicitly benefit from them.

## Value Compounder Lens

Use when the user asks for a Buffett-style or long-term quality/value
view. Do not impersonate a living or historical investor; describe the
method as a style.

Checklist:

- Business quality: simple economics, pricing power, recurring demand,
  low disruption risk.
- Moat durability: network effects, switching costs, cost advantage,
  brand, regulation, data, distribution, or ecosystem lock-in.
- Owner earnings: cash conversion, maintenance capex, share dilution,
  cyclicality, and balance-sheet claims.
- Management and capital allocation: reinvestment runway, buybacks,
  M&A discipline, leverage, and shareholder alignment.
- Margin of safety: compare conservative intrinsic value to price; refuse
  action when valuation depends on heroic assumptions.
- Kill criteria: leverage stress, accounting opacity, moat erosion,
  shrinking runway, management incentives, or structurally poor returns
  on capital.

Output additions:

```json
{
  "quality_score": 0.0,
  "moat": {"type": "...", "durability": "...", "evidence": []},
  "owner_earnings_view": "...",
  "margin_of_safety": {"adequate": false, "reason": "..."},
  "pass_reasons": [],
  "buy_reasons": []
}
```

## Growth Quality Lens

Use when the asset is priced for rapid growth or narrative leadership.

Checklist:

- Demand durability: recurring use, budget priority, customer retention,
  and whether growth is pull-forward.
- Unit economics: gross margin, operating leverage, sales efficiency,
  incremental return on invested capital.
- Competitive pressure: substitutes, customer self-build, price erosion,
  open-source or protocol commoditization.
- Valuation versus growth: expected revisions, implied growth duration,
  and downside if growth merely slows rather than collapses.
- Breakpoints: what quarterly metric would prove the growth thesis wrong.

## Macro Cycle Allocator Lens

Use for allocation questions across equities, rates, FX, commodities,
crypto, or sector rotation.

Checklist:

- Cycle stage: growth, inflation, policy impulse, liquidity, credit, and
  earnings revision breadth.
- Cross-asset confirmation: rates, USD, credit spreads, volatility, oil,
  gold, and sector leadership.
- Crowding: popular consensus trades, ETF/fund flows, positioning, and
  reflexive unwind risk.
- Allocation tilt: risk-on, defensive, quality, duration, cyclicals,
  commodities, cash, or hedges.
- Rebalance triggers: macro releases, central-bank meetings, liquidity
  breaks, credit stress, or volatility regime shifts.

## Quant Risk Allocator Lens

Use when the user asks how much to allocate, hedge, rebalance, or risk.

Checklist:

- Volatility and drawdown: current realized vol, recent worst sessions,
  ATR-style stress, and gap risk.
- Correlation and concentration: single-name, sector, factor, venue, and
  liquidity concentration.
- Scenario loss: base, adverse, and tail paths with mitigation.
- Data quality: stale prices, missing fundamentals, survivorship bias,
  unverified news, or unbacktested assumptions.
- Position guidance: size range, stop/invalidation, review frequency,
  and "do not trade" blockers.

## Expert Panel Pattern

For deeper Agent Team runs, use a small panel instead of many roles:

- Bull case: why the asset can exceed current expectations.
- Bear case: why expectations or valuation can break.
- Value lens: quality, moat, owner earnings, and margin of safety.
- Risk allocator: sizing, scenario loss, and review triggers.
- Research manager: final rating with explicit facts, inferences, and
  data gaps.

Cap the panel at three to five lenses unless the user explicitly asks for
broader coverage.
