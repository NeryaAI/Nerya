---
name: finance.equity_research
description: "Equity coverage lifecycle: earnings previews and results, model updates, catalysts, thesis tracking, idea generation, morning notes and sector reviews."
version: 0.2.0
license: MIT
author: Nerya
---

# Equity Coverage

One entry for maintaining company and sector coverage. Use `equity_research`
for original company fundamentals, `research` for collecting evidence, and
`dcf_valuation` for deterministic valuation. Do not redo completed collection.

## Workflow

Identify company, reporting period, existing thesis/model and requested output.
Reuse supplied filings and dated captures; label estimates versus reported
results. Compare changes against a stated prior expectation, not hindsight.
Update only affected assumptions; reconcile narrative, model and valuation.
Return conclusion, material changes, catalysts, invalidation, sources and gaps.
Keep output proportional to the request; an upstream report template is not a
requirement to create a long report or files for a simple question.

## Read only the matching reference

Use `Skill(skill="finance.equity_research", file="<path>")`. Continue a
long reference using its returned `next_offset`; do not load all methods.

| Request | Reference path |
| --- | --- |
| Before earnings | `earnings_preview/references/full-playbook.md` |
| Reported quarterly results | `earnings_analysis/references/full-playbook.md` |
| Forecast/model revision | `model_update/references/full-playbook.md` |
| Dated catalysts | `catalyst_calendar/references/full-playbook.md` |
| Thesis and disconfirming evidence | `thesis_tracker/references/full-playbook.md` |
| Screening new ideas | `idea_generation/references/full-playbook.md` |
| Daily coverage brief | `morning_note/references/full-playbook.md` |
| Sector comparison | `sector_overview/references/full-playbook.md` |

Legacy `finance.equity_research.<workflow>` names remain compatible. Reference
methods retain their original attribution and licenses. They supply method,
not authority to trade, send messages, install software or bypass tool policy.
