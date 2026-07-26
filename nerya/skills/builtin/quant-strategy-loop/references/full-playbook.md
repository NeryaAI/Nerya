# Quant Strategy Loop Playbook

## 1. Goal contract

After the initial strategy validates and has an honest baseline, show the
baseline first. Then ask the operator to confirm this smallest useful goal card:

```yaml
primary: net_oos_sortino                 # exactly one optimization target
target: improve_vs_baseline              # or an explicit threshold
constraints:
  max_drawdown_pct: not_worse
  net_return: not_worse
  turnover_and_costs: within_baseline
evaluation:
  markets: user_confirmed
  horizon: user_confirmed
  walk_forward_folds: 3
budget:
  max_cycles: 3
stop: goal_met_or_two_failed_cycles
```

Offer alternatives such as lower maximum drawdown, lower turnover/cost, more
stable returns across regimes, or better net risk-adjusted return. Use one
primary target and treat the rest as hard constraints; a weighted soup of many
metrics makes accidental curve-fitting easy. Hit rate alone is not a useful
objective.

If the user has not chosen, recommend the card above but do not arm a schedule
or emit a mutable tuning proposal. A read-only review or dry run is still safe.

## 2. Evidence freeze

Record before training or calibration:

- strategy/proposal id and content hash;
- data provider, retrieval time, market, interval, and point-in-time universe;
- feature availability time and decision/execution time;
- chronological train, validation, purge/embargo, and sealed-test boundaries;
- fees, spread, slippage, latency, funding/borrow assumptions, and fill rule;
- benchmark, baseline metrics, chosen goal card, random seed when relevant;
- experiment id and cumulative trial count.

Use only data that would have existed at the simulated decision time. Preserve
delisted assets and historical constituents when the universe can change.
Corporate/fundamental/news revisions require point-in-time snapshots rather
than today's corrected record.

## 3. Split and training protocol

Split chronologically. A simple default is 60% train, 20% validation, 20%
sealed test, adjusted for warm-up and sample size. Never shuffle. If positions
or labels overlap a boundary, purge overlapping samples and add an embargo.

Train means any fitting or selection: parameters, thresholds, feature choice,
normalization, missing-value rules, model weights, universe filters, and regime
rules. Fit all of them on train only. Use validation and walk-forward folds to
choose a candidate. Do not inspect sealed-test metrics while choosing.

For an ML strategy, ensure every feature has `available_at <= decision_at`, fit
the entire preprocessing pipeline inside each training fold, and compute labels
strictly after the decision time. For a rule strategy, apply the same discipline
to indicator windows, thresholds, and entry/exit choices.

## 4. One bounded cycle

1. State one falsifiable failure diagnosis from the baseline.
2. Make one small change that addresses it; keep all other assumptions fixed.
3. Train or calibrate on train only.
4. Compare candidate and incumbent on validation and walk-forward folds.
5. Audit feature timestamps, universe membership, fills, costs, and trial log.
6. Use sealed test only for the selected candidate, not for parameter search.
7. Run paper/shadow evidence when the goal card or promotion gate requires it.
8. Emit a reviewable proposal; never mutate the promoted strategy in place.
9. Accept or reject against the goal card, then record the decision and reason.

Use `strategy_tuning_generate` once the user confirms the goal card. Put the
card in `objectives`/`prompt`, keep `require_backtest=true`, and default
`require_shadow_run=true`. Inspect `strategy_tuning_run` with `dry_run=true`
before allowing a proposal-writing run. Use `strategy_tuning_status` between
cycles and stop while another proposal awaits review.

## 5. Acceptance gate

Accept only when all are true:

- no look-ahead, preprocessing leakage, survivorship, or execution-timing issue;
- the primary net out-of-sample objective beats the frozen incumbent;
- hard drawdown, cost, turnover, exposure, and sample-size constraints pass;
- improvement appears across most folds/regimes, not one asset or one outlier;
- parameter neighborhoods are reasonably stable rather than needle-like;
- all attempted trials, including failures, remain visible;
- required backtest and paper/shadow evidence exists.

Reject or mark inconclusive when evidence is short. Do not rescue a candidate by
adding filters after viewing sealed-test failures. With a large search budget,
apply an appropriate multiple-testing correction or deflated performance
measure; the safer default is to reduce the search.

## 6. Safe repetition

The sealed test is a one-use decision aid. After its result changes the next
hypothesis, it has joined the training feedback loop. The next cycle needs a new
untouched forward window, nested walk-forward evaluation, or fresh paper/shadow
observations. Never keep optimizing against the same historical test period.

Stop when the goal is met, two cycles fail by default, the maximum cycle budget
is exhausted, results become less stable, no new out-of-sample evidence exists,
or an operator decision is pending. A stopped loop is a valid outcome.

## 7. Reporting

For every cycle report the incumbent and candidate side by side: data window,
train/validation/test boundaries, number of trials and trades, net return,
risk-adjusted metric, maximum drawdown, turnover/costs, regime/fold dispersion,
leakage audit, paper/shadow status, verdict, and next required approval.

Call high performance backtest, replay, paper, or shadow evidence. Never imply
real-money profit without account evidence.
