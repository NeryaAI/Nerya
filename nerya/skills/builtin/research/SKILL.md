---
name: research
description: "External evidence and market research: web/pages, current news/social, stock and crypto-token analysis, fundamentals, tokenomics, on-chain context, and evidence-backed reports."
version: 0.2.0
license: MIT
author: Nerya
---

# Research

One workflow: establish the question, collect bounded evidence, analyse it,
then deliver the requested brief or report. Do not turn a research request
into strategy authoring, trading, or an unsolicited artifact.

## Core flow

Reuse provided captures and URLs before broad discovery. For fresh external
evidence, use one `research_run` request covering the complete question when
that tool is available. The collector uses exposed web tools directly and
must not delegate recursively. Fetch supplied URLs first. Read a returned
capture only when its summary lacks the detail needed for the claim.

For a market brief, add one `market_data` `summarize_market` call for the named
market. Do not substitute prices for requested fundamentals/news. Synthesize
the thesis, evidence, risks, invalidation and confidence in the same turn.
Additional collection must close a specific material gap, not repeat a
successful query. For a normal brief, fetch at most two exact primary URLs
for such gaps, then report remaining limitations and finish.

Use dated primary documents. Navigation shells, blocked pages, empty results
and HTTP success without relevant content are not evidence. Separate facts,
estimates and inference; retain source URL, publication/as-of date and fetch
time. Quantitative claims need a sourced value or an explicit data gap.
Credential or provider failures are limitations, not permission to invent
figures, repeat a failing loop or silently change the requested source.

## On-demand references

Read only the matching file with `Skill(skill="research", file="<path>")`.
Long references return `next_offset` for continuation.

- Market/stock/token brief: `references/market-brief.md`.
- RSS, social evidence, freshness and feed setup: `references/news-and-social.md`.
- Report structure, rating and output QA: `references/reports.md`.
- Blocked pages, PDF extraction, engines and helper scripts: `references/collection.md`.
- Additional background and libraries: `references/full-playbook.md`, `references/libraries.md`.

`market_research`, `news_social` and `research_report` remain compatibility
entry points. Specialist valuation, filings and named frameworks are loaded
only when the assignment needs that method, not for every short brief.
