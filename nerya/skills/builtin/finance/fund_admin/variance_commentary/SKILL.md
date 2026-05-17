<!-- nerya-skill-frontmatter-start -->
---
name: finance.fund_admin.variance_commentary
description: "Use whenever the operator asks to write flux commentary for every P&L and balance-sheet line over threshold — current vs prior period and vs budget, with the driver explained from underlying activity. Use for the month-end close package and management reporting. Adapted from financial-services/fund-admin/variance-commentary (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: medium
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/fund-admin/skills/variance-commentary/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Variance Commentary

Use for `finance.fund_admin.variance_commentary`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> fund accounting issue -> period/entity inputs -> tie/reconcile/trace -> exception commentary.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
