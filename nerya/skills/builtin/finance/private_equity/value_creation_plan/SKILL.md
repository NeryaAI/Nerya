<!-- nerya-skill-frontmatter-start -->
---
name: finance.private_equity.value_creation_plan
description: "Use for: Structure post-acquisition value creation plans with revenue, cost, and operational levers mapped to an EBITDA bridge. Includes 100-day priorities, KPI targets, and accountability frameworks. Use when planning post-close execution, preparing operating partner materials, or building a board-ready value creation roadmap. Triggers on \"value creation plan\", \"100-day plan\", \"post-close plan\", \"EBITDA bridge\", \"operating plan\", or \"value creation levers\". Adapted from financial-services/private-equity/value-creation-plan (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/private-equity/skills/value-creation-plan/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Value Creation Plan

Use for `finance.private_equity.value_creation_plan`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal/portfolio request -> target facts -> diligence/value creation/IC -> decision-ready memo.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
