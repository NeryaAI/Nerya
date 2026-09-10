<!-- nerya-skill-frontmatter-start -->
---
name: plugin_author
description: "Use when the operator asks Nerya to add a runtime capability that is not a skill, strategy, or config change: author a workspace plugin (new native tool, tool-call audit/transform listener, LLM provider adapter, team template) that plugs into the nerya.harness extension skeleton. Covers the full loop: draft plugin.py against the Plugin ABI, syntax-validate without executing, stage a plugin_proposal under evolution/proposals/ with after/plugins/<id>/, attach an executable validation plan, and hand off to operator approval. Never installs executable code without approval; disabled plugins and rollback paths included."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Plugin Author

Author workspace plugins that extend the Nerya runtime itself — new
native tools, tool-pipeline listeners, LLM providers, team templates.
Plugins are the right surface when the capability must live *inside*
the agent loop; use `evolve_skill_proposal` for playbook-shaped
behaviour and `self_modify` for config/prompt changes.

## Flow — proposal-first, always

1. **Classify**: tool? waterfall listener? LLM provider? team
   template? Read `references/full-playbook.md` for the ABI and pick
   the minimal contribution set.
2. **Draft** `plugin.py` against the Plugin ABI
   (`nerya.harness`). Names are forced to `user:<id>`; plugins cannot
   shadow native tools, builtin templates, or builtin providers.
3. **Validate without executing** — run the skill script:
   `propose_plugin` AST-checks the code (syntax + `PLUGIN`/`setup`
   exposure + no network/exec at module level) and stages the
   proposal in one step. Broken code fails closed: no proposal.
4. **Stage**: the script writes `plugin_proposal` under
   `evolution/proposals/<pid>/after/plugins/<id>/plugin.py`, state
   `pending_review`, with an executable test plan
   (`pytest tests/test_harness_extensions.py` + a workspace
   `validate_plugin` run).
5. **Hand off**: operator approves via the dashboard / evolution
   lane. `plugin_proposal` is never auto-applied (new executable code
   always needs a human).
6. **After apply**: the plugin loads at next kernel boot; boot record
   lands in `journals/plugins.jsonl`. Verify with `validate_plugin`,
   then confirm the contributed tool appears in `tool_index`.

## Hard rules

- Never `write_file` directly into `plugins/` — only the proposal
  lane installs executable code (same contract as `strategies/`).
- No secrets in plugin code; resolve via `vault://` refs at runtime.
- `setup()` must be fast and side-effect-light: it runs at boot.
- Waterfall listeners must always `next()` unless replacing a result;
  listener failures are contained but lose the observation.
- Rollback = reject/disable: `plugins.disabled: [<id>]` in nerya.yml
  (hot config proposal) or delete the directory via proposal.

See `references/full-playbook.md` for the full ABI reference, copy
templates, and the disable/audit playbook.
