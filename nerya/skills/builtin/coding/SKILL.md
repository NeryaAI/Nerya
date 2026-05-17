<!-- nerya-skill-frontmatter-start -->
---
name: coding
description: "Use for workspace code reading, editing, search, shell commands, focused plans, background processes, verification, and Nerya source changes."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Coding

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

IF the task extends connectors, data sources, wallets, skills, or UI:
LOAD `references/extending-nerya.md`.

IF a custom chart is needed from dynamic code:
LOAD `references/dynamic-chart-recipe.md`.

## Scripts

- `scripts/reload_subsystem.py` reloads providers, skills, or models.

## Lazy References

- `references/full-playbook.md` for the old detailed coding playbook.
- `references/nerya-core-change-guide.md`.
- `references/extending-nerya.md`.
- `references/dynamic-chart-recipe.md`.
