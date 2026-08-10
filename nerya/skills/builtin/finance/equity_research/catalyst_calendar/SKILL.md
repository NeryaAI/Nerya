<!-- nerya-skill-frontmatter-start -->
---
name: finance.equity_research.catalyst_calendar
description: "Use whenever the operator asks to build and maintain a calendar of upcoming catalysts across a coverage universe — earnings dates, conferences, product launches, regulatory decisions, and macro events. Helps prioritize attention and position ahead of events. Triggers on \"catalyst calendar\", \"upcoming events\", \"what's coming up\", \"earnings calendar\", \"event calendar\", or \"catalyst tracker\". Adapted from financial-services/equity-research/catalyst-calendar (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Catalyst Calendar

Use for `finance.equity_research.catalyst_calendar`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> coverage question -> ticker/company scope -> filings/earnings/catalysts -> thesis/risk/update.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
