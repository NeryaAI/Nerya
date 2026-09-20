---
name: finance.financial_analysis
description: "Financial models and deliverables: three statements, comps, LBO, competitive analysis, spreadsheet creation/cleaning/audit and presentation creation/refresh/review."
version: 0.2.0
license: MIT
author: Nerya
---

# Financial Analysis and Deliverables

Use one workflow for building, updating and checking financial models or
client materials. Start from the actual files, assumptions and audience.
For intrinsic valuation use `dcf_valuation`; for evidence collection use `research`.

## Workflow

Inspect the source structure before changing formulas or layouts. Separate
inputs, formulas and outputs; retain units, currency, periods and provenance.
Preserve existing formats and formulas unless their replacement is requested.
Run the relevant reconciliation, formula and presentation checks; explain
changed assumptions and unresolved issues. Deliver files only when requested
or necessary, and do not represent static numbers as live formulas.

## Read only the matching reference

Read with `Skill(skill="finance.financial_analysis", file="<path>")`.
Use the returned `next_offset` for a long method; never preload all references.

| Task | Reference path |
| --- | --- |
| Linked financial statements | `3_statement_model/references/full-playbook.md` |
| Public comparable companies | `comps_analysis/references/full-playbook.md` |
| Leveraged buyout | `lbo_model/references/full-playbook.md` |
| Competitive positioning | `competitive_analysis/references/full-playbook.md` |
| Workbook creation | `xlsx_author/references/full-playbook.md` |
| Data cleaning | `clean_data_xls/references/full-playbook.md` |
| Formula/model audit | `audit_xls/references/full-playbook.md` |
| Presentation creation | `pptx_author/references/full-playbook.md` |
| Reusable presentation template | `ppt_template_creator/references/full-playbook.md` |
| Refresh existing presentation | `deck_refresh/references/full-playbook.md` |
| Investment-banking presentation QA | `ib_check_deck/references/full-playbook.md` |

Legacy leaf names and their scripts are preserved. Referenced methods retain
original licenses. Script execution still uses the permission-gated tools;
reading a playbook never authorizes shell execution or external disclosure.
