<!-- nerya-skill-frontmatter-start -->
---
name: team
description: "Use when the user explicitly asks to start or launch an Agent Team (启动智能体团队/组建专家委员会), or when coordinated multi-agent work has genuinely independent lanes."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Team

## Explicit team requests

When the user explicitly asks to start, launch, or run an Agent Team, the
**first tool action after loading this skill must be `team_run`**. Do not spend
the tool budget on `role_list`, `role_get`, `research_run`, `web_search`,
`market_data`, or `subagent_run` first. Build the smallest useful inline role
array from the request; role discovery is only for an ambiguous requested role.
`team_run` is synchronous, so synthesize its returned findings in the same
turn. A role lookup or a single subagent is not a completed team run.

Without an explicit team request, use a team only when bounded lanes are
genuinely independent. Keep coupled or tiny work in the main agent.

## Flow

FOR one independent subtask, define owner, inputs, write scope, and expected
evidence, then run one worker while the main agent keeps integrating.

FOR multiple independent lanes, define the shared objective, roles, stop
condition, and evidence contract.
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
