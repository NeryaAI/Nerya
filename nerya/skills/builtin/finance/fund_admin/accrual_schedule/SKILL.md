<!-- nerya-skill-frontmatter-start -->
---
name: finance.fund_admin.accrual_schedule
description: "Use whenever the operator asks to build the period-end accrual schedule — for each accrual, compute the entry, cite the support, and draft the JE. Use during month-end close; the JE is a draft for controller approval, not a posting. Adapted from financial-services/fund-admin/accrual-schedule (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Accrual Schedule

Use for `finance.fund_admin.accrual_schedule`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> fund accounting issue -> period/entity inputs -> tie/reconcile/trace -> exception commentary.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
