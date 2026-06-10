<!-- nerya-skill-frontmatter-start -->
---
name: team
description: "Use when a task needs an Agent Team, committee, multi-role research pass, or coordinated low-frequency strategy decision."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Team

Use for coordinated multi-agent work. For a single isolated subtask,
load `agents` first.

## Flow

DEFINE shared objective, roles, stop condition, and evidence contract.
SPLIT independent lanes with non-overlapping write scopes.
RUN parallel only when lanes do not share mutable files.
COLLECT member outputs into one synthesis.
VERIFY final answer in the leader before reporting.
PERSIST useful roles only when they are reusable.

WHEN the operator splits languages (for example "analyze in Chinese,
final report in English"), pass both `analysis_language` and
`output_language` explicitly in the team run payload so member work and
the synthesis honor the split; the final reply itself must be written in
the requested output language, prose only — never paste members' raw
JSON envelopes into the answer.

REPORT the team's thinking, not just artifacts: the final reply must
summarize each member lane's key finding in one or two sentences (what
the technical / fundamentals / risk lanes concluded and why) before the
verdict — an answer that lists only proposal or backtest line items
hides the work the operator asked the team to do.

WHEN the operator sets an unmeetable constraint (for example "answer
within 5 seconds"), acknowledge it explicitly up front — state the
realistic turnaround and deliver the best fast answer — instead of
silently ignoring the constraint.

## Lazy References

- `references/full-playbook.md` for team templates, role selection, and failure handling.
