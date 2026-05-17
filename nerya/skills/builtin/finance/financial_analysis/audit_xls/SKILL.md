<!-- nerya-skill-frontmatter-start -->
---
name: finance.financial_analysis.audit_xls
description: "Use whenever the operator asks to audit a spreadsheet for formula accuracy, errors, and common mistakes. Scopes to a selected range, a single sheet, or the entire model (including financial-model integrity checks like BS balance, cash tie-out, and logic sanity). Triggers on \"audit this sheet\", \"check my formulas\", \"find formula errors\", \"QA this spreadsheet\", \"sanity check this\", \"debug model\", \"model check\", \"model won't balance\", \"something's off in my model\", \"model review\". Adapted from financial-services/financial-analysis/audit-xls (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/financial-analysis/skills/audit-xls/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Audit Xls

Use for `finance.financial_analysis.audit_xls`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> model/deck/data request -> source workbook/files -> audit/build/check -> explain deltas.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
