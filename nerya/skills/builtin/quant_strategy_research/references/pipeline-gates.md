# Pipeline and Gate Contract

Read this reference before gate evaluation, strategy/component outcome classification, regime validation, Pine reconciliation or stress-job creation.

## Profiles

- `smoke`: wiring and correctness only; never produces a strategy candidate.
- `fast_screen`: bounded train/validation screening, viability and cheap cost sensitivity.
- `full_validation`: only after viability; adds regime/Pine evidence, Walk-forward, locked test, full stress and dry-run preparation.

Profiles live in `configs/pipelines/*.yaml`. Thresholds belong to a profile, market family and strategy objective. Do not turn them into universal market rules.

## Gate meanings

- `correctness`: data timing, no lookahead, cost arithmetic, execution semantics and representative trades are correct.
- `incremental`: one isolated change adds measurable evidence versus the frozen baseline.
- `viability`: a standalone strategy satisfies configured validation net return, Profit Factor, expectancy, trade-count and drawdown thresholds.
- `robustness`: the viable strategy survives declared execution, sensitivity, regime and full-stress checks.
- `locked_test`: a small frozen candidate is evaluated once; later edits contaminate that interval.
- `dry_run`: validated evidence and an explicit simulation approval exist; live trade remains unavailable.

An incremental pass does not imply viability. "Less loss" may be diagnostic evidence or a component hypothesis, never a tradable-strategy candidate by itself.

## Stress levels

- `cheap_cost_sensitivity`: allowed only after fast screen; use it as a low-cost kill test.
- `full`: allowed only with the same subject's passed viability result and explicit subject approval.

The Worker must re-check the full-stress gate. Do not rely only on the Agent or HTTP payload.

## Perpetual execution model

Use `conservative_crypto_perpetual_v1` for perpetual baseline/stress execution semantics. Keep leverage=1 unless an immutable research config explicitly requests a higher value. Check precision, minimum order rules, isolated initial/maintenance margin, mark-price liquidation, entry/exit fees, slippage and funding. On a same-bar conflict use liquidation before protective stop before take profit. Historical/account leverage tiers that are unavailable remain conservative assumptions and cannot support a leverage-safety claim.

## Components

Record source strategy, lineage, component type, target Market Profile, incremental metrics, out-of-sample status and failure conditions. A failed source strategy can yield `diagnostic_improvement` or `component_candidate`; it cannot automatically yield a validated factor/component.

## Regimes

Record detector version, target/suitable/conditional/blocked/unknown sets, per-regime trades/net return/PF/expectancy/drawdown and transition policy. Labels must be available at decision time. Ninety days is screening evidence only.

- `regime_diagnostic`: may run before viability; evidence remains diagnostic, screening or insufficient-history and cannot promote a strategy.
- `regime_validation`: requires the same subject's passed viability GateEvaluation and ex-ante labels.

## Correctness diagnostics and budgets

Freqtrade lookahead/recursive analysis is correctness evidence only. Run it through the safety wrapper, never on rejected strategies or locked-test data, and record unsupported external-engine status explicitly.

Load both the ResearchSession budget and ExperimentPlan budget. The session caps hypotheses, aggregate Trials, compute minutes and locked-test uses. Every Worker execution, whether one-shot or watch-queue, separately enforces time, RSS, concurrency and Parquet batching; exceeding either budget is a recorded block/failure, not permission to silently continue.

## Pine order

- Natural language/Python: complete TradingView/Pine reconciliation after fast screen and viability.
- Pine source: early semantic/repainting/MTF checks plus a few golden trades; complete diagnostic after viability.
