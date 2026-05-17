<!-- nerya-skill-frontmatter-start -->
---
name: finance.investment_banking.process_letter
description: "Use whenever the operator asks to draft process letters and bid instructions for sell-side M&A processes. Covers initial indication of interest (IOI) instructions, final bid procedures, and management meeting logistics. Triggers on \"process letter\", \"bid instructions\", \"IOI letter\", \"bid procedures\", \"final round letter\", or \"management meeting invite\". Adapted from financial-services/investment-banking/process-letter (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/investment-banking/skills/process-letter/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Process Letter

Use for `finance.investment_banking.process_letter`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal-material request -> company/process context -> build/check pack -> banker-ready output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
