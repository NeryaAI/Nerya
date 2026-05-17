<!-- nerya-skill-frontmatter-start -->
---
name: agents
description: "Use whenever the work would benefit from an isolated worker \u2014 a focused subagent for a self-contained subtask, or a coordinated team running in parallel against a shared task list. Triggers on \"spawn a subagent\", \"run this in parallel\", \"delegate to a worker\", \"form a team\", \"research these N companies at once\", or any situation with 2+ independent subtasks that can run without shared mutable state. Read this skill before fanning out so subagents inherit the right scope and you don't lose work to context drift."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Agents playbook

This skill governs delegation. It exists because spinning up
subagents is cheap but coordinating them well is hard, and a
miscoordinated team will spend more tokens than a careful single
agent.

## When to delegate

Delegate when **all** of these hold:

1. The subtask is self-contained — its inputs don't keep changing.
2. Its result is small enough to summarise back in one or two
  paragraphs.
3. It does not need access to your in-progress edits.

Do **not** delegate when:

- the subtask depends on state you're still mutating,
- the answer fits inside one read + one short reply, or
- you would have to spend more tokens explaining the task than just
doing it yourself.

## Single subagent vs team

- **Single subagent**: a focused worker for one subtask. Use when
the work is "one thing, deeply".
- **Team**: N workers consuming a shared task list. Use when the
work is "many similar things, breadth-first" — e.g. "research 20
companies", "lint every package".

A team where every worker does something different is a smell —
that's just N single subagents and you should treat it that way.

## Briefing a subagent

A good brief is short, complete, and explicit about the deliverable.
Include:

- one-paragraph context (just enough),
- the *exact* deliverable shape (e.g. "return a JSON object with
keys X / Y / Z"),
- the relevant file paths or URLs,
- any guard-rails ("do not touch files outside `src/foo`").

Do not paste the entire conversation; subagents that drown in
context produce worse results than ones with a small, focused brief.

## Receiving subagent results

- Read the whole reply before continuing — don't grep it.
- If the result is unclear, ask the subagent to clarify (cheaper
than guessing).
- Don't blindly merge the subagent's edits into your plan; treat
them as *evidence*, then decide.

## Bundled scripts


| Script                      | Purpose                                        |
| --------------------------- | ---------------------------------------------- |
| `scripts/spawn_subagent.py` | Spawn a single isolated worker on a brief.     |
| `scripts/team_run.py`       | Start a team against a shared task list.       |
| `scripts/team_status.py`    | Inspect ongoing team progress / pending tasks. |


All read JSON payload via `--json` / `--payload-file` / stdin.

## Failure modes

- **Spawning when you should just answer.** If the task is "what is
X", just answer.
- **Vague briefs.** "Look into this" is not a brief; specify the
deliverable.
- **Forgetting to harvest results.** A team that finishes silently
is wasted compute — always pull and summarise its output.
