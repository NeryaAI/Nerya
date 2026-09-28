<!-- nerya-skill-frontmatter-start -->
---
name: quant_factor_library
metadata:
  nerya:
    catalog_parent: quant_research
description: "Govern, validate, and promote quantitative factors in a local factor library. Use when the user asks to register, validate, evaluate, deprecate, or promote factors; manage factor status lifecycle (candidate -> validated -> production -> degraded/retired); or enforce factor governance with leak-free multi-window out-of-sample evidence, cost stress, and correlation deduplication."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Quant Factor Library

Use this skill when the user works with quantitative factors: registering new candidates,
running validation pipelines, evaluating factor quality, managing lifecycle transitions,
or enforcing governance rules (no lookahead, multi-window OOS, cost-adjusted promotion).

## Flow

1. CLASSIFY intent: register, validate, evaluate, promote, deprecate, or audit.
2. READ the factor schema and governance rules before creating or modifying any factor.
3. RUN the narrowest Nerya tool or script that produces the requested evidence.
4. SUMMARIZE factor metrics, validation results, and lifecycle recommendations.
5. SAVE artifacts only when they help reproduce or audit the result.

## Factor lifecycle

```
candidate -> validated -> production
    |           |          |
    v           v          v
rejected     degraded    retired
```

- `candidate`: initial research, no validation evidence yet.
- `validated`: passed out-of-sample and stress tests with leak-free multi-window evidence.
- `production`: human-approved for live strategy use. Automatic promotion is forbidden.
- `degraded`: recent decay detected, pending review.
- `retired`: no longer in use, preserved for audit.
- `rejected`: failed validation, record preserved.

## Using Nerya tools for factor research

- Load `Skill(skill="quant_research")` for factor research, statistical analysis, leakage checks, and signal validation.
- Load `Skill(skill="analysis")` for data profiling, charts, and performance attribution.
- Load `Skill(skill="backtest")` for running backtests with realistic fees and execution.
- Load `Skill(skill="quant-strategy-loop")` for bounded train/calibrate -> replay -> review cycles.

## Governance rules

A factor may NOT be promoted to production based solely on historical returns.
At minimum, promotion requires:

- No future data or time leakage (lookahead-free construction).
- Multi-window out-of-sample evidence.
- Incremental value after deducting real transaction costs.
- Stability under parameter perturbation.
- Independence from a small number of anomalous trades.
- Independent information vs. existing production factors (correlation dedup).
- Interpretable market logic.

`production` status MUST be human-approved. No automatic task may execute promotion.

## Lazy References

Read only the selected reference with `Skill(skill="quant_factor_library", file="<path>")`.

- `references/factor-governance.md` for factor lifecycle, schema, and governance rules.
- `references/pipeline-gates.md` for validation pipeline profiles and gate meanings.
- `references/factor-schema.md` for the full factor schema specification.
