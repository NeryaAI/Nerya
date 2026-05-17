<!-- nerya-skill-frontmatter-start -->
---
name: finance.private_equity.ic_memo
description: "Use whenever the operator asks to draft a structured investment committee memo for PE deal approval. Synthesizes due diligence findings, financial analysis, and deal terms into a professional IC-ready document. Use when preparing for investment committee, writing up a deal, or creating a formal recommendation. Triggers on \"write IC memo\", \"investment committee memo\", \"deal write-up\", \"prepare IC materials\", or \"recommendation memo\". Adapted from financial-services/private-equity/ic-memo (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
risk_class: low
adapted_from:
  upstream: financial-services
  upstream_path: plugins/vertical-plugins/private-equity/skills/ic-memo/SKILL.md
  imported_at: 2026-05-09T18:16:49+00:00
  imported_by: finance_skills_importer/0.0.1
category: "finance"
---
<!-- nerya-skill-frontmatter-end -->

# Ic Memo

Use for `finance.private_equity.ic_memo`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal/portfolio request -> target facts -> diligence/value creation/IC -> decision-ready memo.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
