---
name: finance.fund_admin
description: "Fund close and controls: NAV tie-out, general-ledger reconciliation, break tracing, accruals, roll-forwards and variance commentary."
version: 0.2.0
license: MIT
author: Nerya
---

# Fund Administration

Reconcile a named fund, reporting period and set of source books. Confirm the
valuation date, base currency, scope and materiality threshold. Match records
using stable identifiers; never force a balance with unexplained plug entries.
Separate timing differences, missing records, valuation differences and true
errors. Return reconciled totals, exceptions with source rows, proposed fixes
and outstanding reviewer decisions. Commentary must explain quantified causes.
Do not post entries, change official NAV or expose investor data on your own.

## Read only the matching reference

Use `Skill(skill="finance.fund_admin", file="<path>")` and follow
`next_offset` for long references rather than loading all procedures.

| Task | Reference path |
| --- | --- |
| NAV control | `nav_tieout/references/full-playbook.md` |
| General-ledger reconciliation | `gl_recon/references/full-playbook.md` |
| Investigate a break | `break_trace/references/full-playbook.md` |
| Accrual schedule | `accrual_schedule/references/full-playbook.md` |
| Period roll-forward | `roll_forward/references/full-playbook.md` |
| Explain variance | `variance_commentary/references/full-playbook.md` |

Legacy leaf IDs remain callable. Detailed methods keep their original
attribution and licenses; runtime permissions override any example procedure.
