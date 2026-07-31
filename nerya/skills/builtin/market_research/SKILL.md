<!-- nerya-skill-frontmatter-start -->
---
name: market_research
description: "Use for professional market or single-name research combining price, fundamentals, news, sentiment, macro, flows, and risk."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Market Research

Use as the main research coordinator. It should gather evidence before
producing a view.

## Flow

DELEGATE public-web collection with `research_run` when that tool is available,
then analyse the returned captures in the current expert lane.
LOAD `market_data_routing` when source choice is unclear.
FETCH facts with `markets` and `research`.
FOR company primary sources (IR pages, annual reports, filings,
product/news pages), fetch the exact URL with `web_fetch` — its fallback
chain renders JS-heavy pages through the configured browser engine.
IF the document requires navigation, login, or clicks, load `browser`
and drive an interactive session instead.
IF company fundamentals matter, load `equity_research`.
IF factor or signal validation matters, load `quant_research`.
IF a named durable-investor framework is requested, load `expert_investors`.
IF Serenity (including the white-haired finance creator), Unusual Whales, or
The Kobeissi Letter is requested, load `finance-creators`.
SYNTHESIZE thesis, evidence, risks, invalidation, and confidence.
LOAD `research_report` only when the user wants a report artifact.

## Lazy References

- `references/full-playbook.md` for the prior detailed workflow.
