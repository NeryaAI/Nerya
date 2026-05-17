<!-- nerya-skill-frontmatter-start -->
---
name: finance.equity_research.morning_note
description: "Use whenever the operator asks to draft concise morning meeting notes summarizing overnight developments, trade ideas, and key events for coverage stocks. Designed for the 7am morning meeting format — tight, opinionated, actionable. Triggers on \"morning note\", \"morning meeting\", \"what happened overnight\", \"trade idea\", \"morning call prep\", or \"daily note\". Adapted from financial-services/equity-research/morning-note (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/equity-research/skills/morning-note/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Morning Note

Use for `finance.equity_research.morning_note`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> coverage question -> ticker/company scope -> filings/earnings/catalysts -> thesis/risk/update.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
