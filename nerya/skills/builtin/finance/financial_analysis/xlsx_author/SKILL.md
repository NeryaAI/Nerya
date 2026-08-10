<!-- nerya-skill-frontmatter-start -->
---
name: finance.financial_analysis.xlsx_author
description: "Use for: Produce a .xlsx file on disk (headless) instead of driving a live Excel workbook — for managed-agent sessions with no open Office app. Adapted from financial-services/financial-analysis/xlsx-author (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Xlsx Author

Use for `finance.financial_analysis.xlsx_author`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> model/deck/data request -> source workbook/files -> audit/build/check -> explain deltas.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
