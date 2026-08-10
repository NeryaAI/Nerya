<!-- nerya-skill-frontmatter-start -->
---
name: self_modify
description: "Use when Nerya should modify its own configuration or behaviour through coding: review/reflection cadence, schedules, runtime feature flags, LLM tier/model routing, prompt wording, agent policies, news feeds, message channels, trigger routes, market defaults, harness parameters, skill allow-list, strategy parameters, or new behaviours packaged as skills. Routes every change to the correct channel (hot schedule/flag APIs, core-config proposals, tuning generators, skill proposals), attaches an executable validation plan (pytest / static check / backtest / eval_scenario), and explains the auto-apply tier. Protected scopes (risk limits, live trading, kill-switch loosening, accounts, vault, approval policy, the auto-apply lane itself) are never editable: answer with an explicit advisory reject."
version: 0.2.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Self Modify

Use when the operator (or a reflection tick) asks Nerya to change its own
configuration, prompts, parameters, or behaviour. Orchestration playbook
over existing safe surfaces — it adds no new mutation powers; every
durable change is a hot API call or an evolution proposal, never a
bypass of proposal → approval → apply. See
`references/full-playbook.md` for the complete surface matrix.

## Channel routing — classify FIRST, use exactly one channel

HOT (immediate, no proposal):
- Review / reflection / trading cadence: `POST /triggers/schedules/*`
  (add/update/remove/pause/resume) and `POST /evolution/reflection_schedule`.
- Runtime feature flags: `POST /runtime/flags/set` + `/runtime/flags/refresh`.
- Safety tightening: `kill_switch_set` (engage direction is always allowed;
  releasing it is operator-only).
- Paper accounts: `account_upsert` (paper mode only).
- Durable behaviour facts: `memory_remember`.

PROPOSAL via `evolve_core_config_patch` (draft the full post-change file
under `evolution/proposals/` with `write_file` first — that prefix is
writable):
- `nerya.yml` non-protected keys: LLM tiers/models/providers (`llm.*`),
  market defaults (`workspace_preferences.*`), harness caps (`agent.*`),
  memory/research/mcp/integration blocks, dashboard/api hosts.
- `agents.yml`, `workspace.yml` — agent & workspace defaults, prompt wiring.
- `policies/planner.yml`, `policies/tier_policy.yml` — decision logic.
- `news_feeds.yml`, `messages/channels.yml` (secrets as `*_ref` only),
  `triggers/routes.yml` (non-protected keys).
- `skills/enabled.yml` — enable/disable skills (capability change: never
  edit live; hub ids inherit to `hub.*` sub-skills).

PROPOSAL via dedicated generators:
- Strategy parameters / strategy subagent prompts: `strategy_tuning_generate`
  (never `write_file` on `strategies/<id>/`); evidence attaches automatically.
- New behaviour / repeated workflow: `evolve_skill_proposal` stages a
  `skill_proposal` with a complete SKILL.md under `after/skills/<id>/`.

ADVISORY REJECT (protected — no reroute via schedule payloads, shell, or
strategy files): risk limits / `limits.yml`, `runtime.live_trading_enabled`,
kill-switch release, `trading.*`, `risk*`, accounts, vault, approval
policy, route rate/payload caps, `evolution.auto_apply.*`. Answer
`advisory reject: protected_scope` and point to the dashboard approval
path that owns the scope.

## Validation before submission

EVERY proposal MUST carry an executable validation plan: `unit_test`
(`python -m pytest <path>`), `static_check` (strategy code),
`backtest` (trading behaviour), `eval_scenario` (`python -m nerya.evals
--module <module>` — replay scripted agent-loop scenarios so prompt and
policy changes prove the loop still behaves). Then run
`POST /evolution/validation/run` with `dry_run=false`; unpassed required
steps block apply regardless of approval.

## Auto-apply tier (bounded autonomy)

Prose-level lane: `prompt_patch` / `core_config_patch` touching only
prompts, policies, or news feeds, ≤ 4 files, ≤ 200 changed lines,
validation fully passed, and operator-set `evolution.auto_apply.enabled:
true`. Applied by `POST /evolution/auto_apply/tick`, observed 24h,
auto-rolled-back on failing observations. NEVER attempt to widen the
lane: kinds/allowlist/thresholds are hard-coded and protected.

## After apply

RECORD outcomes with `evolve_post_apply_observation` (positive and
negative). If a change made things worse, say so plainly and roll back
via `POST /evolution/rollback` instead of stacking a correction on top.
