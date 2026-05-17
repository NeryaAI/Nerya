<!-- nerya-skill-frontmatter-start -->
---
name: agents
description: "Use when a task needs isolated worker context, bounded delegation, or parallel work across independent subtasks."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Agents

Use this skill to decide whether to keep work local, delegate one
bounded subtask, or form a coordinated team.

## Flow

IF the next step is blocking, coupled, or tiny:
DO it in the main agent.

IF a side task is independent and has a clear output:
DEFINE owner, inputs, files, and expected evidence.
SPAWN one worker.
KEEP integrating locally.

IF there are 2+ independent lanes:
LOAD the `team` skill before fan-out.
ASSIGN non-overlapping write scopes.
VERIFY results centrally before final answer.

## Lazy References

- `references/full-playbook.md` for detailed delegation patterns,
  worker prompts, and failure modes.
