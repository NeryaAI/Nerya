<!-- nerya-skill-frontmatter-start -->
---
name: finance.private_equity.deal_sourcing
description: "Use for: PE deal sourcing workflow — discover target companies, check CRM for existing relationships, and draft personalized founder outreach emails. Use when sourcing new deals, prospecting companies in a sector, or reaching out to founders. Triggers on \"find companies\", \"source deals\", \"draft founder email\", \"check if we've seen this company\", or \"outreach to founder\". Adapted from financial-services/private-equity/deal-sourcing (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Deal Sourcing

Use for `finance.private_equity.deal_sourcing`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal/portfolio request -> target facts -> diligence/value creation/IC -> decision-ready memo.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
