<!-- nerya-skill-frontmatter-start -->
---
name: expert_investors
description: "Hub for the distilled investor lenses. Use to pick which expert lens fits a decision (business quality, DCF, cycles, expectations, macro) or to run a multi-lens committee. Load one expert sub-skill (expert_investors.buffett / .damodaran / .marks / .mauboussin / .druckenmiller) instead of all five — each lens lives in its own sub-skill to keep context small."
version: 0.3.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Expert Investors (Hub)

Apply named analytical personas as a judgment overlay — never a market-data
source, a claim to speak for an expert, or authority to place a trade. This
hub is an index: each expert lens is a separate sub-skill, so loading one
lens does not pull the other four into context.

## Lens Router

| Decision | Primary lens | Useful challenger | Load |
|---|---|---|---|
| Durable business quality and capital allocation | Buffett | Mauboussin | `skill_view("expert_investors.buffett")` |
| Intrinsic value or a disputed DCF | Damodaran | Marks | `skill_view("expert_investors.damodaran")` |
| Market temperature, credit, or permanent-loss risk | Marks | Druckenmiller | `skill_view("expert_investors.marks")` |
| Price-implied expectations or forecast quality | Mauboussin | Damodaran | `skill_view("expert_investors.mauboussin")` |
| Tactical macro, liquidity, or position expression | Druckenmiller | Marks | `skill_view("expert_investors.druckenmiller")` |

Run one lens by default; state why each lens was selected.

## Activation Flow

1. CLASSIFY the request as `pure framework`, `fact-dependent`, or
   `action-oriented`, then ROUTE with the table above. For a single-lens
   question, load that one sub-skill and answer inline.
2. For a committee of two or more lenses, do NOT `skill_view` the lens
   sub-skills into your own context — dispatch one subagent lane per
   expert with ``team_run`` using the matching roles (`buffett_lens`,
   `damodaran_lens`, `marks_lens`, `mauboussin_lens`,
   `druckenmiller_lens`); never seat a generic analyst lane to simulate
   a named expert. If the user does not name experts, pick 2-3
   complementary lenses from the router table and say why. Load the
   synthesis contract (fact register + lens output JSON, REQUIRED for
   committee output) via
   ``skill_view("expert_investors", file="references/full-playbook.md")``
   — builtin assets are not reachable with read_file.
3. For a fact-dependent or action-oriented request, DEFINE the
   instrument, decision, horizon, and evidence cutoff, then GATHER
   current facts with `market_research`, `equity_research`,
   `sec_filings`, `markets`, or `trading`; give each material fact a
   source, `as_of` date, and stable ID. If critical evidence cannot be
   verified, return `Watchlist` and withhold position guidance.
4. RUN each selected lens independently (no lens sees another's
   conclusion first), then synthesize disagreement — do not vote.

## Disagreement Matrix

Preserve these tensions rather than blending them into generic advice:

| Tension | Route by |
|---|---|
| Buffett long-duration ownership vs. Druckenmiller active reversal | Horizon, instrument, and evidence cadence |
| Damodaran explicit forecasts vs. Marks forecast humility | Scenario width and cost of estimation error |
| Mauboussin base rates vs. company-specific narrative | Reference-class fit and structural change evidence |
| Marks measured posture vs. Druckenmiller concentration | Conviction, catalyst, liquidity, and loss capacity |

When lenses disagree, return the disagreement, its governing assumption, and the resolving evidence.

## Shared Rules (apply to every lens)

- Do not impersonate the named person, invent a quotation, or imply
  endorsement. Applying a model to a new asset must be labelled
  `framework inference`.
- Never fill a missing current fact from model memory, and never infer
  a complete portfolio from interviews or Form 13F filings.
- Cite at least one framework source ID per lens used; attach an `as_of`
  date to current market facts.
- A recommendation is never an order: paper/live separation, Risk Gate,
  and Approval Gate always apply.
- Deep provenance: `references/research/` (cutoff 2026-07-27). Distilled
  with [Nuwa](https://github.com/alchaincyf/nuwa-skill) by [Huashu](https://x.com/AlchainHust).
