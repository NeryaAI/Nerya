---
name: quant-strategy-loop
description: "Use to train or calibrate, review, and safely iterate a Nerya quantitative strategy in bounded cycles after its first valid baseline; requires user-confirmed iteration goals and guards against overfitting, look-ahead leakage, survivorship bias, data snooping, and premature live promotion."
---

# Quant Strategy Loop

Use for the full train/calibrate -> replay -> review -> propose -> repeat loop.
Keep every cycle paper/proposal-first and preserve the operator's strategy thesis.

## Flow

BASELINE: Finish the first strategy with `strategy_validate` and
`strategy_backtest` (`allow_mock=false`). Read its artifacts and
`strategy_tuning_snapshot`; label evidence as backtest, replay, paper, or shadow.

GOAL GATE: After the baseline exists, show it to the user and ask them to preset
one primary objective, hard constraints, evaluation horizon/universe, maximum
cycles, and stop conditions. Offer the compact goal card from
`references/full-playbook.md`. Do not create or schedule a mutable tuning loop
from guessed goals; without confirmation, stop at an advisory or dry run.

FREEZE: Before cycle 1, record timestamped data sources, point-in-time universe,
chronological train/validation/sealed-test boundaries, costs, execution timing,
trial count, baseline hash, and the confirmed goal card.

ITERATE: Change one hypothesis or parameter family per cycle. Fit preprocessing,
features, models, and thresholds on train only; select on validation only. Use
walk-forward folds and a purge/embargo when labels overlap.

REVIEW: Replay with realistic fees, spread, slippage, latency, and next-tradable-
event execution. Reject leakage, unstable parameters, one-regime wins, inadequate
sample size, or a train-to-out-of-sample collapse. Compare to the unchanged
baseline and a naive benchmark.

PROPOSE: Use `strategy_tuning_generate` with the confirmed goals,
`require_backtest=true`, and `require_shadow_run=true`. Run
`strategy_tuning_run` as `dry_run=true` first. Emit a proposal only after the
review passes; never approve or promote it automatically.

LOOP: Accept a candidate only when the primary out-of-sample objective improves
and every hard constraint holds. Keep the incumbent otherwise. Once sealed test
or paper results influence a decision, do not optimize against them again;
advance to genuinely unseen forward data. Pause when no new evidence exists, a
stop condition fires, or a proposal awaits review.

## Non-negotiable guards

- Never shuffle time-series splits or fit scalers/imputation on all data.
- Never use future-known fields, revised data, today's constituents, or same-bar
  fills when the signal uses that bar's close.
- Never search the sealed test set, hide failed trials, or report gross returns
  without costs. Track every attempted change.
- Never treat mock/synthetic data, a waiver, or backtest profit as live evidence.
- Keep live trading behind Risk Gate, Approval Gate, paper/shadow evidence, and
  explicit operator approval.

## Lazy Reference

- `references/full-playbook.md` for the goal card, split protocol, acceptance
  gates, and bounded cycle checklist.
