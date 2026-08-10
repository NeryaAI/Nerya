<!-- nerya-skill-frontmatter-start -->
---
name: finance.financial_analysis.deck_refresh
description: "Use for: Updates a presentation with new numbers — quarterly refreshes, earnings updates, comp rolls, rebased market data. Use whenever the user asks to \"update the deck with Q4 numbers\", \"refresh the comps\", \"roll this forward\", \"swap in the new earnings\", \"change all the $485M to $512M\", or any request to swap figures across an existing deck without rebuilding it. Adapted from financial-services/financial-analysis/deck-refresh (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Deck Refresh

Use for `finance.financial_analysis.deck_refresh`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> model/deck/data request -> source workbook/files -> audit/build/check -> explain deltas.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
