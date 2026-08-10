<!-- nerya-skill-frontmatter-start -->
---
name: finance.wealth_management.tax_loss_harvesting
description: "Use for: Identify tax-loss harvesting opportunities across taxable accounts. Finds positions with unrealized losses, suggests replacement securities, and tracks wash sale windows. Triggers on \"tax-loss harvesting\", \"TLH\", \"harvest losses\", \"tax losses\", \"unrealized losses\", or \"year-end tax planning\". Adapted from financial-services/wealth-management/tax-loss-harvesting (Apache-2.0)."
version: 0.0.1
license: Apache-2.0
author: Anthropic
---
<!-- nerya-skill-frontmatter-end -->

# Tax Loss Harvesting

Use for `finance.wealth_management.tax_loss_harvesting`. Keep this file as the routing surface; load the full method only when the task matches.

## Flow

MATCH -> client portfolio request -> household/account constraints -> analyze -> advisor-reviewed output.
VERIFY -> inputs, requested deliverable, data freshness, and review boundary.
LOAD -> `references/full-playbook.md` when method details are needed.
EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.
CHECK -> calculations, citations, assumptions, and unresolved risks.
RETURN -> concise output plus files created or evidence used.

## Lazy References

- `references/full-playbook.md` for the upstream detailed workflow.
