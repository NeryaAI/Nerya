<!-- nerya-skill-frontmatter-start -->
---
name: finance.operations.kyc_doc_parse
description: "Use for: Parse an investor or client onboarding packet into structured KYC fields — identity, ownership, control, source of funds, and document inventory. Use as the first step of KYC screening; output feeds the rules engine. Adapted from financial-services/operations/kyc-doc-parse (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# KYC Doc Parse

Use for `finance.operations.kyc_doc_parse`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> KYC/onboarding request -> document/rule source -> parse/check/escalate -> structured decision support.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
