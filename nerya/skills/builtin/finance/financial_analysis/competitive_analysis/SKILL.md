<!-- nerya-skill-frontmatter-start -->
---
name: finance.financial_analysis.competitive_analysis
description: "Use for: Framework for building competitive landscape decks — market positioning, competitor deep-dives, comparative analysis, strategic synthesis. Use when the user asks for a competitive landscape, competitor analysis, peer comparison, market positioning assessment, strategic review, or investment memo deck. Also. Triggers on \"who are the competitors to X\", \"benchmark X against peers\", \"build a market map\", or any request to systematically evaluate competitive dynamics across an industry. Adapted from financial-services/financial-analysis/competitive-analysis (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Competitive Analysis

Use for `finance.financial_analysis.competitive_analysis`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> model/deck/data request -> source workbook/files -> audit/build/check -> explain deltas.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
