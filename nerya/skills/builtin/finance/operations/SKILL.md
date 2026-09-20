---
name: finance.operations
description: "KYC review support: extract identity/document fields, apply provided rules and report missing, inconsistent or escalated evidence."
version: 0.2.0
license: MIT
author: Nerya
---

# KYC Operations

Confirm the authorized review scope, applicable supplied rule version and
minimum required documents. Extract only necessary fields with document/page
provenance; mark illegible or absent values instead of inferring identities.
Apply the supplied rules to extracted evidence and distinguish pass, fail and
manual review. Return a redacted exception report and reviewer questions.
Do not treat a model classification as final legal/compliance approval, invent
jurisdictional rules, transmit identity documents or persist sensitive data
outside the authorized scope.

## Read only the matching reference

Use `Skill(skill="finance.operations", file="<path>")`.

| Task | Reference path |
| --- | --- |
| Document extraction | `kyc_doc_parse/references/full-playbook.md` |
| Rule-based review | `kyc_rules/references/full-playbook.md` |

Legacy leaf names still resolve; references retain their original attribution.
Use returned `next_offset` to continue long references. Tool policy and human
review boundaries remain authoritative.
