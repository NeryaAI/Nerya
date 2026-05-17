<!-- nerya-skill-frontmatter-start -->
---
name: finance.investment_banking.teaser
description: "Use whenever the operator asks to draft anonymous one-page company teasers for sell-side M&A processes. Creates a compelling summary without revealing the company's identity, designed to gauge buyer interest before NDA execution. Triggers on \"teaser\", \"blind teaser\", \"anonymous profile\", \"one-pager for process\", or \"draft teaser for sell-side\". Adapted from financial-services/investment-banking/teaser (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/investment-banking/skills/teaser/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Teaser

Use for `finance.investment_banking.teaser`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal-material request -> company/process context -> build/check pack -> banker-ready output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
