<!-- nerya-skill-frontmatter-start -->
---
name: finance.investment_banking.strip_profile
description: "Use for: |. Adapted from financial-services/investment-banking/strip-profile (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Strip Profile

Use for `finance.investment_banking.strip_profile`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal-material request -> company/process context -> build/check pack -> banker-ready output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
