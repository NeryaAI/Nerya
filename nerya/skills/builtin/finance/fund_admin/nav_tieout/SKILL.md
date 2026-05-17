<!-- nerya-skill-frontmatter-start -->
---
name: finance.fund_admin.nav_tieout
description: "Use for: Tie an LP statement to the fund's NAV pack — recompute the LP's capital account from the NAV components and flag any line that doesn't agree. Use before LP statements are distributed. Adapted from financial-services/fund-admin/nav-tieout (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: medium
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/fund-admin/skills/nav-tieout/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Nav Tieout

Use for `finance.fund_admin.nav_tieout`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> fund accounting issue -> period/entity inputs -> tie/reconcile/trace -> exception commentary.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
