# Author a separate review plan after the strategy

Reference template for the authoring Agent, not a universal plan to copy verbatim.
Finish the strategy's execution rules first. Then derive and save its independent
review plan before reporting the candidate complete. It belongs to the SAME
strategy_id and proposal, not a second trading strategy, generic automation task,
or Agent Loop modification.

## Deliverables and customization

Persist the configuration below under `strategy.yml::tuning` and replace every
placeholder in the prompt template with facts from the completed strategy. Save
that prompt at the exact `tuning.subagent.prompt_file` path. This file IS the
separate, editable review plan and is loaded as the review Agent's instructions;
do not create an unconsumed plan file or leave the plan only in chat. In strategy.md,
link to it and summarize the timing, purpose and current disabled/active state.

Choose cadence from signal frequency, holding period and the time needed for new
evidence, not the trading tick interval. Honor the user's timezone and limits.
For example, an intraday strategy might need cost/slippage and repeated-entry
checks; a slower trend strategy needs exit timing, missed trends and drawdown;
an event Agent needs event freshness, skip/dispatch correctness and task outcomes.
An observer needs data quality and output coverage, not a forced return target.
These are examples, not hard-coded rules or promised improvements.

Set lookback large enough to cover the chosen review window at the actual run
frequency. `min_closed_trades` is a global pre-Agent gate: use 0 when health/error
review must still run without trades, and put the minimum profitability sample
in the plan instead. An observation Agent may never have closed trades.

The visible workflow is **review scheduler → evidence script → review Agent
(Proposer)**. The built-in collector uses tuning.lookback and publishes frozen
`performance` with package context, attributable runs/fills/errors and Agent task
evidence. Reuse it; no invented `ctx.review` API or extra executor is needed.

## Configuration example: hourly strategy, daily review

This is a valid reference block, not the timing/metrics for every strategy.
Adapt every choice after implementing the strategy. Review capability is defined
with `enabled:true`, but its schedule stays disabled until separately activated.
Trading `schedule.enabled` also stays false for a new candidate. A whole-strategy
no-AI/no-review instruction instead keeps tuning disabled and documents the opt-out;
a pure Python trading tick alone does not prohibit an independent review plan.

<!-- template:review_config -->
```yaml
tuning:
  enabled: true
  schedule:
    type: cron
    cron: "15 0 * * *"
    timezone: UTC
    enabled: false
  lookback:
    runs: 168
    max_age_hours: 168
    min_closed_trades: 0
  subagent:
    name: strategy_tuner
    prompt_file: subagents/strategy_tuner.agent.md
    tier: medium
  objectives: [risk_adjusted_return, execution_quality]
  proposal_policy:
    allowed_targets: [strategy.yml, main.py, subagents/strategy_tuner.agent.md]
    forbidden_targets: ["accounts/*", limits.yml, "secrets/*", live_trading_enabled]
  guardrails:
    max_patch_files: 3
    max_position_size_change_pct: 25
    require_backtest: true
    require_shadow_run: false
    require_operator_approval: true
  tuning_prompt: "Follow the strategy-specific plan in the configured prompt file; adapt checks to each run's frozen evidence."
```

Use actual helper-file paths in allowed_targets when needed. Preserve account,
funding, risk, mode and activation constraints; allowed_targets does not authorize
relaxing them. For observers choose execution_quality and behavior validation, not
profitability tests. Do not add unsupported manifest fields for the plan.

## Package-local plan template

Replace all `{{...}}` fields using the completed strategy and the user's language.
Do not invent evidence or use the template's example cadence as a universal default.
The current package hash and review run_id are supplied at runtime, never hard-coded
into this file (which itself contributes to the package hash).

<!-- template:review_prompt -->
```markdown
# Review plan · {{strategy_id}}

## Strategy being reviewed
{{strategy_summary}}

## Independent review schedule and evidence window
{{review_schedule}}

## Strategy-specific checks
{{review_focus}}

## Evidence sufficiency and hold conditions
{{evidence_rules}}

## Permitted changes and invariant constraints
{{allowed_changes}}

## Validation and rollback criteria
{{validation_rules}}

## Each review invocation
Load Skill(skill="strategy_author", file="references/review.md"). Use this plan
and the supplied tuning_prompt with the current manifest and frozen performance.
Return a brief public review_plan containing strategy_id, package_hash, run_id,
evidence window, prioritized checks, deferred checks and hold conditions, adapted
to THIS run. Keep planned checks separate from observed findings. Do not create
another planning Agent, competing candidates or a new scheduled task.

Act as the single Proposer. Return review_plan, summary, evidence, proposed_changes,
expected_effect (hypothesis only), validation_plan and risk_flags. Insufficient
evidence or no justified improvement means proposed_changes: []. Materializable
changes require complete after_content or complete config_after/yaml_after, with
evidence-linked rationale. The runner creates the pending-review PatchProposal.
Never place orders, apply changes, enable trading, weaken approvals or mutate the
running package. Any change to future review timing or this plan is also a proposal.
```

## Completion check

Both strategy and review plan must be in the saved, validated candidate. Confirm
that the prompt file exists, has no placeholders and names this strategy's real
rules; its timing/window match tuning; its checks, allowed files and validation
fit the implementation. Keep review roles out of trading subagents/teams unless
trading logic explicitly calls them. Review schedules compile independently from
trading and remain disabled. Do not run a review/optimization loop during creation.

When behavior changes during editing or verification, update the affected plan
sections in the same candidate and validate the final bundle. Preserve custom
restrictions and explicit opt-outs. Report the saved plan and schedule along with
strategy/replay receipts; do not claim a future review has run or a proposal applied.
