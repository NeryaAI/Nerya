---
name: research
description: "External evidence and market research: web/pages, news/social, stocks and tokens; explain saved replay coverage or 为什么回测不完整 from producer records, and proactively search 链上数据 for Jupiter/Solana, Uniswap/LP, pools and historical gaps."
version: 0.5.0
license: MIT
author: Nerya
metadata:
  nerya:
    script_policy:
      read_only:
        - fetch_url.py
        - web_search.py
        - search_fetch.py
        - news_search.py
        - social_search.py
---

# Research

One workflow: establish the question, collect bounded evidence, analyse it,
then deliver the requested brief or report. Do not turn a research request
into strategy authoring, trading, or an unsolicited artifact.

## Delivery contract

For gap inspection on an existing candidate/pool/window without a requested new
brief or report, load `references/onchain-data-discovery.md` first. Reuse a delivery
only through trusted continuation; otherwise return read-only findings citing
the original evidence, without creating a separate `research_brief`. This branch
does not require market visuals or extra collection to obtain completion checks.
An explicitly requested new brief/report uses the standalone contract below,
even when it reuses the same pool/window or must preserve an older replay.

The installed host enforces the minimum evidence in
`references/delivery-contract.json`. Until producers emit those checks,
report incomplete/partial; model-authored receipts cannot satisfy the floor.

At the start of a multi-step research run, call `delivery_start` with
`skill_id: "research"`, workflow `research_brief`, and these stages:
`scope`, `collect`, `coverage`, `synthesis`, `deliver`. Register sourced
evidence/report artifacts with `delivery_update`. For research based on installed
public `data_api` producers, save a report citing each source URL, then call
`research_verify(report_path=...)`. The producer locates matching successful
calls in this command; keep tool IDs and journal lookup out of the workflow.
Register its returned `evidence` and
`report` artifact refs/revisions; read its producer-owned checks, never author
them yourself. The receipt covers declared sources, retrieval time and saved
report bytes, not factual accuracy, exhaustive research or historical replay. Provider or
source gaps are `limitations` and may produce a partial delivery, never a
passed check. Before the final response call `delivery_finalize`; its
persisted completion projection is authoritative. On a pending or failed
 source, keep the delivery resumable and do not repeat an unchanged request.
The first call must use only the advertised fields: `skill_id`, `workflow_id`,
`requested_scope.summary`, `requested_scope.limitations`, and stage `id`/`label`
plus any required artifact/check lists. Do not add generated keys or model
metadata to the request. Use the exact workflow key `research_brief`; after a
successful start, reuse its returned `delivery_revision` and stage definitions.

## Core flow

For a truncated table or "已保存的完整表格", follow "Recover a truncated
display" in `references/onchain-data-discovery.md`: recover its durable raw
result before collecting again. `data_api(op="snapshots")` lists admitted
replay inputs; it is not the store for ordinary research table results.

For a status-only follow-up, explain saved producer receipts in the user's
language without new collection or execution. For historical coverage, read the
exact run's `metrics.json`: `modeled_coverage` and excluded `gaps` describe the
simulation; `observation_coverage` describes raw acquisition, not modeled loss.
Explain exclusions using `gaps[].start/end/seconds` and the saved
`provenance.assumptions.max_gap_seconds`; raw UTC day counts alone do not
establish modeled gaps, and boundary-supported endpoints may cover raw offsets.
Without those records, leave the numerical cause unverified. Keep any next
action within the admitted source/window; a proposed source's compatibility
remains unverified until its producer admits it.

For on-chain data or an unavailable protocol/history source, load
`references/onchain-data-discovery.md` and actively search primary sources
and acquire compatible history through available read-only producers before
reporting an unresolved gap. A missing local adapter, empty catalog, provider
error or zero local cache is a trigger for this discovery workflow, never the
conclusion that the chain or protocol has no history. Let the Agent choose the
next compatible source and producer autonomously within the admitted scope;
ask the operator only at an access, cost, permission or material scope
boundary. Use its historical-source reference to choose
between price candles, transaction events and historical contract state; resolve
ordinary source choices autonomously within the admitted scope.
As supporting discovery within another
Skill's task, reuse its delivery; source research is not a separate market brief
and does not require unsolicited price cards or a new research delivery.

Reuse provided captures and URLs before broad discovery. For fresh external
evidence, use one `research_run` request covering the complete question when
that tool is available. The collector uses exposed web tools directly and
must not delegate recursively. Fetch supplied URLs first. Read a returned
capture only when its summary lacks the detail needed for the claim.

For a market brief, add one `market_data` `summarize_market` call for the named
market. Do not substitute prices for requested fundamentals/news. Synthesize
the thesis, evidence, risks, invalidation and confidence in the same turn.
Additional collection should close a specific material gap. Follow the evidence
until the requested question is answered or a concrete access/cost prerequisite
remains; reuse successful captures instead of repeating their queries.

Use dated primary documents. Navigation shells, blocked pages, empty results
and HTTP success without relevant content are not evidence. Separate facts,
estimates and inference; retain source URL, publication/as-of date and fetch
time. Quantitative claims need a sourced value or an explicit data gap.
Credential or provider failures are limitations, not permission to invent
figures, repeat a failing loop or silently change the requested source.

## Choose research depth

Keep the core flow for an ordinary brief. For a broad investigation, a requested
source comparison or unresolved material contradiction, read
`references/deep-research.md`. Use one complete research brief and a second pass
only on gaps; do not launch a crowd of shallow workers. For research that must
persist across events, read `references/evidence-lifecycle.md`. Evidence versions,
source dates and decision revisions are separate from model confidence. The
user chooses the deliverable format; do not force a DOCX or another artifact.

## Interactive research deliverables

For a market brief or an explicitly requested market visual, publish
the relevant instruments and observed price data in that turn. Source-only briefs,
historical-gap findings and status follow-ups use the requested text/report format;
they do not require unrelated price reads or chart publication. The dashboard
renders a name/price/change/sparkline card at the end of that reply, not below
the composer. Reuse existing sourced evidence when still appropriate, keeping
its original as-of date; fetch fresh evidence when current data was requested.
Provide observed candlesticks for the right asset drawer
when available, and publish useful comparisons/flow studies to the left research
workspace. These interactive results are part of the requested research, not an
unsolicited standalone report. Do not leave chart code, ASCII diagrams or a
promise to plot as the deliverable.

Load `Skill(skill="research", file="references/visual-deliverables.md")`, then
execute `scripts/publish_visuals.py` with the collected evidence. Confirm its
`ok`, `receipt.chart_ids` and artifact readback before claiming publication.
Unavailable candles/news/flows are explicit data gaps, never simulated fills.

## On-demand references

Read only the matching file with `Skill(skill="research", file="<path>")`.
Long references return `next_offset` for continuation.

- Market/stock/token brief: `references/market-brief.md`.
- On-chain identity, unavailable history or source recovery: `references/onchain-data-discovery.md`.
- Historical API selection and evidence limits: `references/historical-data-sources.md`.
- RSS, social evidence, freshness and feed setup: `references/news-and-social.md`.
- Report structure, rating and output QA: `references/reports.md`.
- Blocked pages, PDF extraction, engines and helper scripts: `references/collection.md`.
- Additional background and libraries: `references/full-playbook.md`, `references/libraries.md`.

`market_research`, `news_social` and `research_report` remain compatibility
entry points. Specialist valuation, filings and named frameworks are loaded
only when the assignment needs that method, not for every short brief.

## Optional methods

Use `Skill(skill="<name>")` for the relevant available method only:

- `equity_research`: company fundamentals, filings, valuation and coverage updates.
- `expert_investors`: select a Buffett, Damodaran, Marks, Mauboussin or Druckenmiller lens.
- `finance-creators`: only explicitly requested Serenity, Unusual Whales or Kobeissi views.
- `finance.financial_analysis`, `finance.fund_admin`, `finance.investment_banking`,
  `finance.operations`, `finance.private_equity`, `finance.wealth_management`:
  professional modelling, close, transaction, KYC, investment or advisory workflows.

Load one lens inline for a single perspective. Independent comparisons use
separate instances with the exact lens profiles and their existing restricted
policies. Reuse one collected evidence set; preserve disagreement. A method
selection grants no extra tools, source access or execution authority.
