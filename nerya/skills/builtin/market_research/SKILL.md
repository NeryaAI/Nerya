<!-- nerya-skill-frontmatter-start -->
---
name: market_research
description: "Use for professional market, stock, or crypto-token research (代币/加密资产分析), combining price, company fundamentals or tokenomics, valuation, news, sentiment, macro, flows or on-chain activity, catalysts, and risk."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Market Research

Use as the main research coordinator. Gather a bounded evidence set, then
produce the view in the same turn.

## Flow

CALL `market_data` once with `summarize_market` for price structure and
technical context.
CALL `research_run` once with the complete fundamentals, news, sentiment,
macro, flow, tokenomics, or on-chain evidence question.
AFTER both return, synthesize thesis, evidence, risks, invalidation, and
confidence. Do not repeat equivalent `market_data`, `research_run`,
`web_search`, `web_search_fetch`, or `read_file` calls. Use `web_fetch` for at
most two exact primary-source URLs only when the returned capture identifies a
material gap. Report any remaining gap and finish the answer.

LOAD a specialist skill only when the user names a framework or requests a
workflow beyond this one-turn research brief.
LOAD `research_report` only when the user wants a report artifact.

## Lazy References

- `references/full-playbook.md` for the prior detailed workflow.
