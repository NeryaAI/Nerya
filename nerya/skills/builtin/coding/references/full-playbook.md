<!-- nerya-skill-frontmatter-start -->
---
name: coding
description: "Use whenever the agent needs to read, edit, search, or run code in the workspace, draft a multi-step plan before touching files, manage long-running background scripts, or extend Nerya itself (new exchange / data source / wallet / dashboard widget) with a hot-reload so the running system picks the change up. Triggers on \"fix this bug\", \"refactor\", \"add a function\", \"run my script\", \"watch this loop\", \"add support for X exchange / data source / wallet\", \"tweak the dashboard\", or any request involving source edits, shell commands, multi-file coordination, or live-system extension."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Coding playbook

This skill is the agent's hands inside the workspace. It does **not** add
new tools — every primitive (`read_file`, `edit_file`, `run_shell`,
`make_plan`, `update_plan`, `start_background_task`, …) is already
exposed natively. The skill teaches *how* to use them well so that
edits land safely, plans stay honest, and long-running scripts don't
get orphaned.

## When to read this skill

Pull this skill into context whenever:

- the user asks for a change that spans more than one file,
- you're about to run a command whose effect could be hard to undo,
- you need to start, supervise, or stop a background process, or
- you're tempted to skip planning on a task that has more than two
unknowns.

If the task is "rename one variable in one file", just do it; no
playbook needed.

## Core loop

1. **Understand before editing.** Read the files you'll touch and at
  least skim their imports. If the codebase is unfamiliar, search for
   the symbols involved before guessing at semantics.
2. **Plan when uncertain.** Use `make_plan` whenever the work has 3+
  distinct steps, or when an early step's outcome decides what the
   later steps look like. A plan is a contract with the user; if you
   change direction, update it.
3. **Edit minimally.** Prefer `edit_file` over rewrites; preserve
  indentation and surrounding lines. Don't reformat code you didn't
   need to touch.
4. **Run, but cautiously.** `run_shell` is for system commands (git,
  package managers, build tools, ad-hoc Python). Use specialised
   tools (read/edit/search) for file ops — they're faster and won't
   corrupt encoding.
5. **Verify before claiming done.** If the task said "fix the bug",
  actually run the failing case (or read the relevant test). If you
   can't run it, say so — never assume.

## Editing safely

- **One concern per edit.** Multiple unrelated changes in a single
edit make review harder and increase the chance of botched merges.
- **Read before write.** Always confirm the current contents of the
file before issuing a destructive edit; don't trust your memory of
what's there.
- **Keep diffs reviewable.** Avoid mass-renames or formatter passes
unless that *is* the task.
- **Comments are for intent, not narration.** Don't add `// increment counter` next to `i += 1`. Do explain non-obvious trade-offs and
invariants.

## Running shell commands

- Quote paths that contain spaces.
- Don't chain destructive commands behind `&&`; run them one at a
time so you can inspect output between steps.
- For anything that streams (servers, watchers, long builds), launch
it as a background task rather than blocking the foreground.
- If a command fails, read the *full* error output before retrying;
silent retries are a smell.

## Background scripts

When the user asks for a process that should keep running (a watcher,
a polling loop, a local server):

1. Start it with `start_background_task` so it gets a stable id and
  its stdout/stderr are captured.
2. Note the task id in your reply so the user can refer to it.
3. When the task is no longer needed, stop it explicitly — don't rely
  on it dying when the session ends.
4. To check on a long-running task, read its log; don't keep polling
  the process itself.

## Common failure modes to avoid

- **Editing without reading.** Leads to clobbered files. Always read
first.
- **Skipping the plan when it would help.** If you find yourself
doing exploratory work that branches in three directions, stop and
write a plan; it almost always pays back.
- **Treating `run_shell` as a hammer.** If a dedicated tool exists,
use it.
- **Leaving background tasks running** after the user has moved on.

## Nerya code changes

IF the user asks to change Nerya itself:
LOAD `references/nerya-core-change-guide.md`.

IF the change adds an exchange, data source, wallet, dashboard widget,
or workspace extension:
LOAD `references/extending-nerya.md`.

IF custom dynamic chart code is needed:
LOAD `references/dynamic-chart-recipe.md`.

## Bundled scripts

- `scripts/reload_subsystem.py` — reload providers / skills / models
  without restarting the running kernel. Use after dropping a new
  connector under `nerya/connectors/<vendor>.py` (built-in track) or
  `workspace/providers/<id>/provider.py` (workspace track).
