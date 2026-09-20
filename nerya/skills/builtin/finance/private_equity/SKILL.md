---
name: finance.private_equity
description: "Private investment lifecycle: sourcing, screening, diligence, IC memos, returns, unit economics, AI readiness, portfolio monitoring and value creation."
version: 0.2.0
license: MIT
author: Nerya
---

# Private Investment Lifecycle

Start with mandate, company, transaction stage, supplied materials and decision
needed. Reuse the existing data room and diligence log rather than restarting
collection. Distinguish management assertions from independently supported
facts. Quantify assumptions and downside; connect diligence gaps to valuation
and decision conditions. Return recommendation, evidence, risks, owner-assigned
next steps and the requested memo/model. Do not make investment commitments,
contact targets or change portfolio operations without explicit authorization.

## Read only the matching reference

Use `Skill(skill="finance.private_equity", file="<path>")`; follow
`next_offset` to continue the selected method rather than loading the library.

| Stage/question | Reference path |
| --- | --- |
| Find targets | `deal_sourcing/references/full-playbook.md` |
| Initial investment screen | `deal_screening/references/full-playbook.md` |
| Diligence workplan | `dd_checklist/references/full-playbook.md` |
| Management meeting | `dd_meeting_prep/references/full-playbook.md` |
| Investment committee memo | `ic_memo/references/full-playbook.md` |
| Return scenarios | `returns_analysis/references/full-playbook.md` |
| Unit economics | `unit_economics/references/full-playbook.md` |
| AI readiness | `ai_readiness/references/full-playbook.md` |
| Portfolio KPI review | `portfolio_monitoring/references/full-playbook.md` |
| Operating value creation | `value_creation_plan/references/full-playbook.md` |

Use `finance.financial_analysis` for LBO/model QA. Compatibility leaf IDs,
assets and original reference licenses are preserved.
