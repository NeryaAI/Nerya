# Bounded Batch Research Contract

Read this reference for Proposal directions, batch Trial execution, retry, result summaries, or ComponentEvidence aggregation.

## Proposal and approval

- Keep at most three active directions per frozen Baseline.
- Require one falsifiable hypothesis, an exact Baseline and subject ID, rule/code diff, evidence, ParameterSpace, train/validation split, cost model, Objective, Constraints, Trial/time estimate, failure conditions, stopping conditions, and rollback.
- Use `draft -> waiting_approval -> approved -> executing -> evaluated -> accepted/rejected/expired`.
- Require explicit approval of the exact Proposal subject. Approval creates an immutable Candidate; it never updates the Baseline.

## Search and execution

- Use deterministic grid or seeded random search only. Do not enable Hyperopt/Optuna.
- Enforce both ExperimentPlan and ResearchSession budgets. Never include locked-test data in search.
- Let the deterministic Worker run Trials through StrategyEvaluator/TrialExecutor; never assign one Agent action per Trial.
- Default concurrency to one. Apply any memory-based recommendation only below the Worker hard cap, and keep locked test/final promotion serial.
- Preserve completed Trial records on failure, cancellation, timeout, RSS failure, or retry. Retry only unfinished parameter signatures and do not reserve the same Session budget again.
- Write detailed metrics to a batch Parquet sink; keep status, key metrics, resource use, errors, and project-relative Artifact keys in SQLite.

## Evidence and claims

- Prefer stable validation ranges over an isolated best point and keep train/validation metrics separate.
- Treat deterministic fixtures as wiring evidence only; never call their metrics strategy results.
- Aggregate components by normalized logic, component type, Market Profile, and timeframe. Do not create a factor per parameter value.
- Preserve diagnostic evidence from rejected strategies, but require configured incremental and trade-count evidence before `component_candidate`. Never auto-promote to validated.
