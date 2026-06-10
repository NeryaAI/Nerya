<!-- nerya-skill-frontmatter-start -->
---
name: tasks
description: "Use to create, schedule, inspect, or manage operator tasks, recurring reports, and non-strategy agent/script jobs."
version: 0.1.0
license: MIT
author: Nerya
permissions:
  - workspace-write
tags:
  - tasks
  - schedules
  - automation
---
<!-- nerya-skill-frontmatter-end -->

# Tasks

Use this when the user asks Nerya to do work later, keep working in the
background, create a recurring report, or run an agent/script task outside a
strategy.

## Flow

CLASSIFY the task as one-off background work, recurring agent work, recurring
approved-script work, or just progress inspection.

For one-off background subagent work, use native `subagent_run_async`, then
monitor with `task_summary`, `task_get`, `task_output`, or `task_stop`.

For recurring work, run `scripts/create_task.py`. It writes a schedule with
`session_kind="agent"` or `session_kind="script"` and can route output through
gateway/platform delivery targets such as Telegram.

For recurring agent work, write a durable process-style `generated_prompt`
yourself whenever possible. The prompt should spell out the workflow, source
checks, output format, language, delivery expectations, and safety constraints.
Keep the user's original sentence in `source_request` when useful for audit.

For inspection, run `scripts/list_tasks.py` or use native `task_list`.

VERIFY the task exists, whether it is enabled, the session mode, and delivery
targets. REPORT the id and how to pause/remove it.

## Scripts

- `scripts/create_task.py`: create or update a recurring agent/script task.
- `scripts/list_tasks.py`: list recurring schedules and background task records.

## Lazy References

- `references/full-playbook.md` for payload examples and task-shape rules.
