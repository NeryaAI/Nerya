<!-- nerya-skill-frontmatter-start -->
---
name: finance.private_equity.returns_analysis
description: "Use whenever the operator asks to build quick IRR/MOIC sensitivity tables for PE deal evaluation. Models returns across entry multiple, leverage, exit multiple, growth, and hold period scenarios. Use when sizing up a deal, stress-testing assumptions, or preparing IC returns exhibits. Triggers on \"returns analysis\", \"IRR sensitivity\", \"MOIC table\", \"what's the return at\", \"model the returns\", or \"back of the envelope\". Adapted from financial-services/private-equity/returns-analysis (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Returns Analysis

Use for `finance.private_equity.returns_analysis`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal/portfolio request -> target facts -> diligence/value creation/IC -> decision-ready memo.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
