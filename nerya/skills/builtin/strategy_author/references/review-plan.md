# Independent review plan

Deliver this with each new strategy, including deterministic trading strategies.
Keep the default execution body **evidence collector → Proposer Agent**. Do not
add a second collector, extra reviewers, a team or a second workflow engine.

## Save without enabling

The following is an illustrative contract, not a universal trading default:

```yaml
tuning:
  enabled: false
  schedule:
    type: cron
    cron: "0 9 * * 1"
    timezone: Asia/Shanghai
    enabled: false
  lookback:
    runs: 200
    max_age_hours: 168
    min_closed_trades: 10
  review_plan:
    scope: "This strategy, selected package hash, paper mode and requested window only."
    focus: "Investigate missed signals, realized slippage and exit timing using attributable runs."
    validation_plan: "Compare current and candidate on the same held-out window, fees and data; inspect coverage and leakage."
    next_review: "Observe enough closed trades before judging changes; review execution cost next if slippage dominates."
```

Choose values from the actual strategy rather than copying these examples.
High-frequency, slow-trend, observation-only and event-driven strategies need
different evidence windows and minimum samples. A closed-trade gate is not a
sensible requirement for a task that intentionally never trades. For an explicit
no-AI request, preserve `tuning.enabled:false`; the plan may describe manual
review, and no Agent step or enabled schedule may be introduced.

`review_plan` is a bounded text mapping. The executable selections remain in
`lookback`, `schedule`, `guardrails` and `proposal_policy`. A note saying “risk
limit 5%” does not configure a risk limit. Preserve protected settings and do not
weaken approval or financial authorization. Saving a candidate neither applies
it nor changes the active schedule; inspect actual installed schedules after a
separately authorized promotion, using returned IDs.

## Every review is attributable

Read the frozen `performance.evidence_scope`: requested window, package hash,
mode, selected runs / Agent tasks, excluded counts and ledger attribution.
Selected records do not prove complete market coverage. State missing data,
partial history and lookback truncation rather than silently treating them as a
complete sample. Do not mix current files with an older run's evidence.

Return one focused change or no change. Keep observed results, expected effects
and proposed validation separate. Performance below buy-and-hold is not an
execution failure. If no useful improvement is supported, explain what additional
evidence would justify the next review, without manufacturing an optimization.

When a change is warranted, update the next review focus in the same complete
`strategy.yml` candidate, and specify an observation window and stop/rollback
conditions. The next plan is a proposal; never rewrite this run's frozen plan.

## Reuse

A reusable strategy or task carries its definition, parameter descriptions and
evidence requirements, not live account grants, secrets, past session IDs or
delivery recipients. Bind those afresh and keep schedules disabled. Stable inputs,
source revisions and run lineage should survive a parameterized rerun; a new run
is not a retry of a notification or an instruction to repeat a financial action.
