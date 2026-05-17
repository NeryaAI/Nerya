<!-- nerya-skill-frontmatter-start -->
---
name: finance.equity_research.earnings_preview
description: "Use whenever the operator asks to build pre-earnings analysis with estimate models, scenario frameworks, and key metrics to watch. Use before a company reports quarterly earnings to prepare positioning notes, set up bull/bear scenarios, and identify what will move the stock. Triggers on \"earnings preview\", \"what to watch for [company] earnings\", \"pre-earnings\", \"earnings setup\", or \"preview Q[X] for [company]\". Adapted from financial-services/equity-research/earnings-preview (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/equity-research/skills/earnings-preview/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Earnings Preview

Use for `finance.equity_research.earnings_preview`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> coverage question -> ticker/company scope -> filings/earnings/catalysts -> thesis/risk/update.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
