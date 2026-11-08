<!-- nerya-skill-frontmatter-start -->
---
name: coding
description: "Build native dashboard panels and pages (面板、行情、账户概览; BTC/ETH quotes and portfolio) through workspace_ui_inspect and workspace_ui_propose. Also use for workspace code reading, editing, search, shell commands, verification, and Nerya source changes."
version: 0.2.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Coding

## Native panels first

This Skill has no task-delivery workflow contract. Do not call `delivery_start`,
`delivery_update` or `delivery_finalize` for coding, or invent a workflow id.
Use the inspected applied-page link or producer-returned proposal/preview and
validation result as handoff evidence; this is not a task-delivery completion receipt.

For a panel, dashboard widget, account overview, page or navigation request,
including “帮我做一个展示 BTC、ETH 行情和账户概览的面板。”, load
`references/native-panels.md`, then call `workspace_ui_inspect` and follow the
task intent: CREATE proposes a new uniquely identified page from the read-only
widget catalog even when a similar page exists; UPDATE changes the exact
requested target; READ may reuse a matching applied page with its actual link.
Existing layouts may inform construction, but do not fulfill a CREATE request.
This also applies when an assistant delegates panel construction to this task.
For layout changes, the deliverable is a validated, pending-review native UI
proposal with its id and diff; only READ or an already-satisfied UPDATE needs
no new proposal.
For requested prices, 24h changes and trends, both new proposals and reused
pages require read-only source verification before handoff, as described in
the native-panels reference.
Missing data does not prevent drafting the layout, but prevents claiming a
working dynamic panel. Approval and activation remain operator actions.
Use server-backed widgets; never embed credentials or replace this workflow
with a large standalone HTML stream or direct `ui/workspace.yml` write.

For a separately delegated summarization, translation or drafting call on supplied
content, load `Skill(skill="llm")` when available. Ordinary reasoning and writing
stay in the current conversation; they do not require another model call.

Use for concrete code or shell work. This skill stays separate from
`evolve`: coding changes behavior now; evolve drafts proposal-first
capability growth.

## Flow

IF the task touches code:
READ nearby files and search existing patterns first.
PLAN only when the work has multiple dependent steps.
EDIT narrowly and preserve unrelated changes.
RUN the smallest command that proves the change.
REPORT changed files, verification, and any unverified gap.

IF the task changes Nerya itself:
LOAD `references/nerya-core-change-guide.md`.

IF the task extends connectors, data sources, wallets, skills, or adds a UI
capability missing from the inspected native widget catalog:
LOAD `references/extending-nerya.md`.

IF a custom chart is needed from dynamic code:
LOAD `references/dynamic-chart-recipe.md`.

## Scripts

- `scripts/reload_subsystem.py` reloads providers, skills, or models.

## Lazy References

- `references/native-panels.md` for panel construction and data/approval boundaries.
- `references/full-playbook.md` for the old detailed coding playbook.
- `references/nerya-core-change-guide.md`.
- `references/extending-nerya.md`.
- `references/dynamic-chart-recipe.md`.
