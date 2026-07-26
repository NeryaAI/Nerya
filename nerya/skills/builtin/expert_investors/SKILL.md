<!-- nerya-skill-frontmatter-start -->
---
name: expert_investors
description: "Use when asked what Buffett, Damodaran, Howard Marks, Mauboussin, or Druckenmiller would emphasize; to analyze an investment like a named expert; to compare investor frameworks; or to run an investor committee on business quality, DCF, expectations, cycles, macro liquidity, sizing, or downside risk. Produces source-backed framework inferences, not generic market data, impersonation, or autonomous trading."
version: 0.2.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Expert Investors

Apply named analytical personas as a judgment overlay. Never treat this
skill as a market-data source, a claim to speak for an expert, or authority
to place a trade.

## Activation Flow

1. CLASSIFY the request as `pure framework`, `fact-dependent`, or
   `action-oriented`.
2. LOAD `references/investor-lenses.md`. Also load
   `references/full-playbook.md` for multiple lenses, portfolio or sizing
   questions, structured committee memos, and every paper/live proposal.
3. For a fact-dependent or action-oriented request, DEFINE the instrument,
   decision, horizon, and evidence cutoff. GATHER current facts with
   `market_research`, `equity_research`, `sec_filings`, `markets`, or
   `trading`; give each material fact a source, `as_of` date, and stable ID.
   If critical evidence cannot be verified, return `Watchlist` and withhold
   position guidance.
4. SELECT the smallest useful set: one lens by default, two for a challenge,
   and no more than three unless the user explicitly requests the full panel.
5. RUN each lens independently. Keep its horizon, assumptions, framework
   inferences, invalidation conditions, and source IDs distinct.
6. SYNTHESIZE disagreement by naming the governing assumption and evidence
   that would resolve it; never average incompatible horizons or vote.
7. RETURN implications, adverse and tail cases, invalidation, data gaps,
   confidence, and risk limits. A recommendation is a proposal, not an order.

## Boundaries

- Use reasoning patterns, not first-person impersonation.
- Framework source IDs support methods, never current prices, filings,
  holdings, liquidity conditions, or portfolio facts.
- If an exact quotation cannot be verified, paraphrase it and label the
  application `framework inference`.
- Never infer a complete portfolio from interviews or Form 13F filings.
- Never fill a missing current fact from model memory; expose the gap.
- Do not present valuation as a point fact or a macro view as certainty.
- Require Risk Gate and Approval Gate for any later live-trading action.

## Reference Routing

- Always load `references/investor-lenses.md` for lens selection, mental
  models, failure boundaries, and framework source IDs.
- Load `references/full-playbook.md` for debate, position guidance, portfolio
  decisions, structured output, and paper/live proposals.
- Load only the needed provenance file under `references/research/`:
  `01-writings.md`, `02-conversations.md`, `03-expression-dna.md`,
  `04-external-views.md`, `05-decisions.md`, or `06-timeline.md`.
