<!-- nerya-skill-frontmatter-start -->
---
name: finance.wealth_management.investment_proposal
description: "Use for: Create professional investment proposals for prospective clients. Covers the firm's approach, proposed allocation, expected outcomes, and fee structure. Use when pitching new clients or presenting a new investment strategy. Triggers on \"investment proposal\", \"prospect presentation\", \"pitch new client\", \"proposal for [client]\", or \"new client presentation\". Adapted from financial-services/wealth-management/investment-proposal (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: medium
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/wealth-management/skills/investment-proposal/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Investment Proposal

Use for `finance.wealth_management.investment_proposal`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> client portfolio request -> household/account constraints -> analyze -> advisor-reviewed output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
