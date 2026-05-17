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

## Lazy References

- `references/full-playbook.md` for team templates, role selection, and failure handling.
