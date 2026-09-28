# Pipeline and Gate Contract

Read this reference before gate evaluation, factor outcome classification, regime validation, or stress-job creation.

## Profiles

- `smoke`: wiring and correctness only; never produces a validated factor.
- `fast_screen`: bounded train/validation screening, viability, and cheap cost sensitivity.
- `full_validation`: only after viability; adds regime evidence, walk-forward, locked test, and full stress.

Profiles live in pipeline config files. Thresholds belong to a profile, market family, and factor objective. Do not turn them into universal market rules.

## Gate meanings

- `correctness`: data timing, no lookahead, cost arithmetic, execution semantics, and representative trades are correct.
- `incremental`: one isolated change adds measurable evidence versus the frozen baseline.
- `viability`: a standalone factor satisfies configured validation net return, Profit Factor, expectancy, trade-count, and drawdown thresholds.
- `robustness`: the viable factor survives declared execution, sensitivity, regime, and full-stress checks.
- `locked_test`: a small frozen candidate is evaluated once; later edits contaminate that interval.

An incremental pass does not imply viability. "Less loss" may be diagnostic evidence or a component hypothesis, never a validated factor by itself.

## Stress levels

- `cheap_cost_sensitivity`: allowed only after fast screen; use it as a low-cost kill test.
- `full`: allowed only with the same subject's passed viability result and explicit subject approval.

## Factor-specific rules

- A failed source strategy can yield `diagnostic_improvement` or `component_candidate`; it cannot automatically yield a validated factor.
- Record source strategy, lineage, component type, target market profile, incremental metrics, out-of-sample status, and failure conditions.
- Correlation with existing production factors must be checked before promotion.
