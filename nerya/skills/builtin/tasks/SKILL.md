<!-- nerya-skill-frontmatter-start -->
---
name: tasks
description: "Use to create, schedule, inspect, or manage operator tasks, recurring reports, and non-strategy agent/script jobs."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Tasks

Use this when the user asks Nerya to do work later, keep working in the
background, create a recurring report, or run an agent/script task outside a
strategy.

When the user calls it a strategy, needs new script logic, or requires a numeric condition to be checked before calling AI, load `strategy_author` instead. Observation-only and “do not trade” strategies still belong there. Do not substitute an always-running Agent for a script-first condition gate.

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

For recurring schedule inspection, use `scripts/list_tasks.py` or `/triggers/schedules/status` with the exact returned `task_id`. Native `task_list`/`task_get` inspect background execution records, not the schedule registry; an empty background list does not prove schedule creation failed. Do not recreate a schedule until the correct registry has been checked.

Interpret a Cron expression in its supplied timezone. For 09:00 Asia/Shanghai use `0 9 * * *` with `timezone: Asia/Shanghai`; do not also subtract eight hours. Verify the computed local time, enabled state, session mode and supported delivery targets. Leave delivery targets empty for a dashboard-only result rather than inventing a dashboard gateway. REPORT the returned id and how to pause/resume it.

## Scripts

- `scripts/create_task.py`: create or update a recurring agent/script task.
- `scripts/list_tasks.py`: list recurring schedules and background task records.

## Lazy References

- `references/full-playbook.md` for payload examples and task-shape rules.
