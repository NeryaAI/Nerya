<!-- nerya-skill-frontmatter-start -->
---
name: triggers
description: "Use to create or inspect scheduled tasks, event hooks, condition watchers, price alerts, recurring reports, and route explanations."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Triggers

Use when work should happen later or in response to an event.

## Flow

For operator-facing recurring reports, background work, or non-strategy
agent/script jobs, use the `tasks` skill first. Use this skill for lower-level
trigger routes, event hooks, and condition watchers.

CLASSIFY trigger: schedule, event, or condition watcher.
DEFINE target, payload, idempotency key, cooldown, TTL, and owner.
DRY-RUN or explain routing before activation.
CREATE only through trigger APIs or proposals.
VERIFY status, last run, next run, and dead-letter behavior.
REPORT how to pause, resume, or remove it.

## Lazy References

- `references/full-playbook.md` for trigger shapes, routing, and safety rules.
