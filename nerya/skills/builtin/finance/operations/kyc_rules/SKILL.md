<!-- nerya-skill-frontmatter-start -->
---
name: finance.operations.kyc_rules
description: "Use for: Apply the firm's KYC/AML rules grid to a parsed onboarding record — assign a risk rating, list every rule outcome with the rule cited, and flag what's missing or escalation-worthy. Use after kyc-doc-parse; this skill decides nothing, it scores and routes. Adapted from financial-services/operations/kyc-rules (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: high
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/operations/skills/kyc-rules/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# KYC Rules

Use for `finance.operations.kyc_rules`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> KYC/onboarding request -> document/rule source -> parse/check/escalate -> structured decision support.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
