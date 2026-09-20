---
name: finance.wealth_management
description: "Client advisory preparation: financial plans, investment proposals, reviews, reports, portfolio rebalancing and tax-loss-harvesting analysis."
version: 0.2.0
license: MIT
author: Nerya
---

# Wealth Advisory Preparation

Establish client objectives, constraints, horizon, liquidity needs, authorized
account scope and dated holdings. State missing suitability or tax inputs.
Compare alternatives against the same assumptions; show fees, risk, liquidity
and trade-offs. Separate planning scenarios from guarantees. Return an
explainable proposal or review with assumptions, evidence and required human
decisions. Never infer permission to trade, contact the client or transmit
private holdings. Tax-specific decisions require current jurisdiction-specific
rules and qualified review, not a generic template.

## Read only the matching reference

Use `Skill(skill="finance.wealth_management", file="<path>")` and
`next_offset` to continue the chosen reference when needed.

| Task | Reference path |
| --- | --- |
| Financial plan | `financial_plan/references/full-playbook.md` |
| Investment proposal | `investment_proposal/references/full-playbook.md` |
| Client meeting preparation | `client_review/references/full-playbook.md` |
| Client reporting | `client_report/references/full-playbook.md` |
| Allocation/rebalancing proposal | `portfolio_rebalance/references/full-playbook.md` |
| Tax-loss opportunities | `tax_loss_harvesting/references/full-playbook.md` |

Legacy leaf IDs and original licenses remain intact. Actual execution belongs
to `trading` and its risk/approval gates, never to a planning reference.
