<!-- nerya-skill-frontmatter-start -->
---
name: finance.investment_banking.buyer_list
description: "Use whenever the operator asks to build and organize a universe of potential acquirers for sell-side M&A processes. Identifies strategic and financial buyers, assesses fit, and prioritizes outreach. Use when preparing for a sell-side mandate, building a buyer universe, or evaluating potential partners. Triggers on \"buyer list\", \"buyer universe\", \"potential acquirers\", \"who would buy this\", \"strategic buyers\", or \"financial sponsors\". Adapted from financial-services/investment-banking/buyer-list (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Buyer List

Use for `finance.investment_banking.buyer_list`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal-material request -> company/process context -> build/check pack -> banker-ready output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
