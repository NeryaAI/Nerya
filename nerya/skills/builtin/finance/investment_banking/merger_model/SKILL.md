<!-- nerya-skill-frontmatter-start -->
---
name: finance.investment_banking.merger_model
description: "Use whenever the operator asks to build accretion/dilution analysis for M&A transactions. Models pro forma EPS impact, synergy sensitivities, and purchase price allocation. Use when evaluating a potential acquisition, preparing merger consequences analysis for a pitch, or advising on deal terms. Triggers on \"merger model\", \"accretion dilution\", \"M&A model\", \"pro forma EPS\", \"merger consequences\", or \"deal impact analysis\". Adapted from financial-services/investment-banking/merger-model (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Merger Model

Use for `finance.investment_banking.merger_model`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> deal-material request -> company/process context -> build/check pack -> banker-ready output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
