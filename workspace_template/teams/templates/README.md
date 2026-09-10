# Team templates

Operator-authored Agent Team topologies in YAML — the same
file-driven pattern as `subagents/*.agent.md`. Loaded by
`nerya/teams/templates.py` (`load_workspace_templates`).

The shape is exactly `TeamTemplate.asdict()` output; see the module
docstring in `nerya/teams/templates.py` for the full field reference.
`overnight_watch_team.yml` in this directory is a working example.

Rules:

- Workspace template ids must not collide with builtin ids
  (`market_analysis_team`, `investment_committee_team`,
  `strategy_design_team`) — collisions are skipped with a warning.
- Members/tasks reference subagent names from
  `workspace/subagents/*.agent.md`.
- Edits are picked up on next read (mtime cache); call
  `reload_workspace_templates()` to force a re-parse.
