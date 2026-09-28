# Factor Governance

## Factor status

- `candidate`: candidate research; no validation evidence yet.
- `validated`: passed out-of-sample and stress tests.
- `production`: human-approved for use in production strategies.
- `degraded`: recent decay detected, pending review.
- `retired`: no longer in use.
- `rejected`: failed validation but record preserved.

## Required fields per factor

Every factor must record:

- Unique ID, name, category, and version.
- Formula and code path.
- Data requirements, lookback length, and available-at timing.
- Direction, normalization method, and missing-value handling.
- Applicable assets, timeframes, and market regimes.
- In-sample, out-of-sample, walk-forward, and cost-stress results.
- Turnover, decay speed, and capacity.
- Correlation with existing factors.
- Creation source, modification reason, and status history.

## Promotion conditions

A factor must NOT be promoted based solely on historical returns.
At minimum, promotion requires:

- No future data or time leakage.
- Multi-window out-of-sample evidence.
- Incremental value after deducting real costs.
- Stability under parameter perturbation.
- Independence from a small number of anomalous trades.
- Independent information vs. existing factors.
- Interpretable market logic.

`production` status MUST be human-approved; automatic tasks cannot execute promotion.

## Validation pipeline profiles

- `smoke`: wiring and correctness only; never produces a validated factor.
- `fast_screen`: bounded train/validation screening, viability, and cheap cost sensitivity.
- `full_validation`: only after viability; adds regime evidence, walk-forward, locked test, and full stress.

## Factor deduplication

Before registering a new factor, check correlation with existing production factors.
A new factor must demonstrate independent information, not just a reskinned variant.
