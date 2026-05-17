<!-- nerya-skill-frontmatter-start -->
---
name: finance.wealth_management.financial_plan
description: "Use whenever the operator asks to build or update a comprehensive financial plan covering retirement projections, education funding, estate planning, and cash flow analysis. Use for new client onboarding, annual plan reviews, or scenario modeling. Triggers on \"financial plan\", \"retirement plan\", \"can I retire\", \"education funding\", \"estate plan\", \"cash flow analysis\", or \"plan update\". Adapted from financial-services/wealth-management/financial-plan (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: medium
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/wealth-management/skills/financial-plan/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Financial Plan

Use for `finance.wealth_management.financial_plan`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> client portfolio request -> household/account constraints -> analyze -> advisor-reviewed output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
