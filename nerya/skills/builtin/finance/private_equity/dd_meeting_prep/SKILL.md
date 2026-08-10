<!-- nerya-skill-frontmatter-start -->
---
name: finance.private_equity.dd_meeting_prep
description: "Use for: Prepare for due diligence meetings — management presentations, expert network calls, customer references, and advisor sessions. Generates targeted question lists, benchmarks to reference, and red flags to probe. Use before any diligence meeting or call. Triggers on \"prep for management meeting\", \"diligence call prep\", \"expert call questions\", \"customer reference questions\", or \"meeting prep for [company]\". Adapted from financial-services/private-equity/dd-meeting-prep (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Dd Meeting Prep

Use for `finance.private_equity.dd_meeting_prep`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal/portfolio request -> target facts -> diligence/value creation/IC -> decision-ready memo.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
