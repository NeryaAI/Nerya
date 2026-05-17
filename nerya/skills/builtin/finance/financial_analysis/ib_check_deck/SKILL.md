<!-- nerya-skill-frontmatter-start -->
---
name: finance.financial_analysis.ib_check_deck
description: "Use for: Investment banking presentation quality checker. Reviews a pitch deck or client-ready presentation for (1) number consistency across slides, (2) data-narrative alignment, (3) language polish against IB standards, (4) visual and formatting QC. Use whenever the user asks to review, check, QC, proof, or do a final pass on a deck, pitch, or client materials — including requests like \"check my numbers\", \"reconcile figures across slides\", \"is this client-ready\", or \"what am I missing before I send this out\". Adapted from financial-services/financial-analysis/ib-check-deck (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Ib Check Deck

Use for `finance.financial_analysis.ib_check_deck`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> model/deck/data request -> source workbook/files -> audit/build/check -> explain deltas.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
