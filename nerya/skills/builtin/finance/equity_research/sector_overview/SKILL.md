<!-- nerya-skill-frontmatter-start -->
---
name: finance.equity_research.sector_overview
description: "Use for: Create comprehensive industry and sector landscape reports covering market dynamics, competitive positioning, key players, and thematic trends. Use for client requests, sector initiations, thematic research pieces, or internal knowledge building. Triggers on \"sector overview\", \"industry report\", \"market landscape\", \"sector analysis\", \"industry deep dive\", or \"thematic research\". Adapted from financial-services/equity-research/sector-overview (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Sector Overview

Use for `finance.equity_research.sector_overview`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> coverage question -> ticker/company scope -> filings/earnings/catalysts -> thesis/risk/update.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
