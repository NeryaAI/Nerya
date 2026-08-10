<!-- nerya-skill-frontmatter-start -->
---
name: finance.equity_research.earnings_analysis
description: "Use for: Create professional equity research earnings update reports (8-12 pages, 3,000-5,000 words) analyzing quarterly results for companies already under coverage. Fast-turnaround format focusing on beat/miss analysis, key metrics, updated estimates, and revised thesis. Includes 1-3 summary tables and 8-12 charts. Use when user requests \"earnings update\", \"quarterly update\", \"earnings analysis\", \"Q1/Q2/Q3/Q4 results\", or post-earnings report. Adapted from financial-services/equity-research/earnings-analysis (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Earnings Analysis

Use for `finance.equity_research.earnings_analysis`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> coverage question -> ticker/company scope -> filings/earnings/catalysts -> thesis/risk/update.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
