# Self Modify — Full Playbook

The compact SKILL.md carries the routing rules. This document expands
rationale, exact tool sequences, and worked examples.

## Design contract

`self_modify` adds **no new mutation powers**. Complete surface matrix:

| Surface | Channel | Effect |
|---|---|---|
| Review / reflection / tuning cadence | `POST /triggers/schedules/*`, `POST /evolution/reflection_schedule` | hot, no proposal |
| Runtime feature flags | `POST /runtime/flags/set` + `/refresh` | hot, no proposal |
| Kill switch (engage only) | `kill_switch_set` | immediate; release is operator-only |
| Paper accounts | `account_upsert` | immediate; paper mode only |
| Durable behaviour facts | `memory_remember` | immediate |
| LLM tiers / models / providers (`llm.*`) | `evolve_core_config_patch` target `nerya.yml` | `core_config_patch` proposal |
| Market defaults (`workspace_preferences.*`) | same, target `nerya.yml` | proposal |
| Harness caps / planner manifest (`agent.*`) | same, target `nerya.yml` | proposal |
| Memory / research / mcp / integrations blocks | same, target `nerya.yml` | proposal |
| Agent + workspace defaults, prompt wiring | targets `agents.yml`, `workspace.yml` | proposal |
| Decision-logic policies | targets `policies/planner.yml`, `policies/tier_policy.yml` | proposal |
| News feeds | target `news_feeds.yml` | proposal |
| Message channels (secrets as `*_ref`) | target `messages/channels.yml` | proposal |
| Trigger routes (non-protected keys) | target `triggers/routes.yml` | proposal |
| Skill allow-list | target `skills/enabled.yml` | proposal (capability change) |
| Workspace prompts (`agents/*.md`, `subagents/*.agent.md`) | draft under proposal + `prompt_patch` | proposal, auto-apply-eligible |
| Strategy params / strategy prompts | `strategy_tuning_generate` | `strategy_tuning_proposal` |
| New behaviour / workflow | `evolve_skill_proposal` | `skill_proposal` |
| Protected scope | — | `advisory reject: protected_scope` |

Protected (both proposal-creation and apply reject): strategy
`limits.yml`, `runtime.live_trading_enabled`, kill-switch release,
`trading.*`, `risk*`, accounts, vault, approval/signer policy, trigger
route rate/payload caps, `evolution.auto_apply.*`.

Protected scopes are enforced twice (proposal creation and apply) by
`nerya.evolution.patch_proposal.PROTECTED_SCOPES`; do not attempt any
workaround, including embedding the change in a schedule payload,
shell command, or strategy package file.

## Worked example: change the LLM model for a tier

1. `GET /llm/config` (or read `nerya.yml`) — capture the current `llm`
   block.
2. Build the full post-change `nerya.yml` object with only the tier's
   `provider` / `model` changed; keep every other key verbatim.
3. `evolve_core_config_patch` with `target="nerya.yml"`. Provider keys
   never go in the proposal — reference `vault://` entries only.
4. Attach an `eval_scenario` validation step so the loop is replayed
   against the routing change before apply.

## Worked example: enable a skill

1. Read `skills/enabled.yml` (whitelist; absent file = all builtins).
2. Propose the updated list via `evolve_core_config_patch` with
   `target="skills/enabled.yml"`. Hub ids inherit: enabling
   `expert_investors` also enables every `expert_investors.*` leaf.
3. Never edit the file live — it is a capability surface and
   `write_file` redirects it to the proposal tool.

## Worked example: adjust the review cadence

1. `GET /triggers/schedules` — find the schedule id (e.g.
   `workspace_reflection_dream` or `strategy_<id>_tuning`).
2. `POST /triggers/schedules/update` with the new `cron` /
   `every_seconds` / `timezone`.
3. Confirm with `GET /triggers/schedules/status`. No proposal, no
   restart; the scheduler re-reads `triggers/schedules.yml` per tick.

## Worked example: reword a subagent prompt

1. Read the current prompt (`subagents/<name>.agent.md`).
2. Draft the full new file body under
   `evolution/proposals/draft_<topic>/after/subagents/<name>.agent.md`
   using `write_file`.
3. Call `evolve_core_config_patch` (target `agents.yml` companions) or
   — for strategy-scoped prompts — `strategy_tuning_generate`.
4. Author an eval scenario module exporting `SCENARIOS` that replays
   the loop with the scripted backend and asserts the same tool calls,
   then attach a validation step:
   `{"type": "eval_scenario", "command": "python -m nerya.evals --module <module>"}`.
5. `POST /evolution/validation/run` with `dry_run=false`; verify every
   required step is `passed` with an `evidence_ref`.
6. Wait for Inbox approval — or, if the operator enabled the
   auto-apply tier and the diff fits the lane, `POST
   /evolution/auto_apply/tick` applies it and opens a 24h observation
   window.

## Auto-apply tier internals (nerya/evolution/auto_apply.py)

Hard-coded, not configurable:

- kinds: `prompt_patch`, `core_config_patch`
- path allowlist: `agents/*.md`, `subagents/*.agent.md`,
  `strategies/*/prompts/*.agent.md`, `news_feeds.yml`,
  `policies/planner.yml`, `policies/tier_policy.yml`
- limits: ≤ 4 files, ≤ 200 changed diff lines
- validation plan must be fully passed (required steps green with
  evidence refs) regardless of proposal kind
- opt-in flag `evolution.auto_apply.enabled` is a PROTECTED scope —
  only the operator can flip it, and the agent cannot even stage a
  proposal that touches `evolution.auto_apply.*`

Rollback trigger: any post-apply observation with status `failed`,
`regressed`, or `degraded` inside the 24h window rolls the proposal
back from `evolution/artifacts/<pid>/before/` and journals
`proposal.auto_rolled_back` with `actor=auto_apply_tier`.

## Failure honesty

If validation fails, report the failing step and stop — do not weaken
the plan to make it pass. If an applied change degrades behaviour,
record a negative `evolve_post_apply_observation` and roll back; never
paper over a regression with a second stacked proposal.
