<!-- nerya-skill-frontmatter-start -->
---
name: quant_research
description: "Use for factor research, statistical data analysis, backtest diagnostics, performance attribution, leakage checks, and validation of trading signals before promotion."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Quant Research Playbook

Use this skill when the market question needs numbers rather than only
commentary: factor tests, signal validation, backtest review,
performance attribution, regime slicing, or risk statistics.

## Validation Ladder

1. Data sanity:
   - row counts, date coverage, duplicate timestamps,
   - missing values by column and by period,
   - adjusted/unadjusted price mode,
   - look-ahead risk from same-bar labels or future joins.
2. Signal sanity:
   - distribution, outliers, turnover,
   - correlation with known factors,
   - decay by forward horizon,
   - stability by market regime and asset group.
3. Predictive tests:
   - IC / rank IC and hit rate,
   - quantile return spread,
   - long-short spread after costs,
   - walk-forward or purged split where applicable.
4. Portfolio tests:
   - exposure concentration,
   - drawdown, volatility, Sharpe, Calmar,
   - turnover and slippage sensitivity,
   - capacity and liquidity limits.
5. Promotion evidence:
   - baseline comparison,
   - failure cases,
   - shadow or paper-run requirement,
   - rollback and monitoring metrics.

## IC / IR Interpretation

| Metric | Weak | Useful | Strong | Red Flag |
|---|---:|---:|---:|---|
| mean IC | < 0.02 | > 0.03 | > 0.05 | > 0.10 without explanation |
| IC positive ratio | < 52% | > 55% | > 60% | unstable sign |
| IR | < 0.3 | > 0.5 | > 1.0 | sample too small |
| quantile monotonicity | absent | partial | clear | V-shape without hypothesis |

Negative IC can be useful if the strategy explicitly reverses the
signal. Report the direction instead of discarding it blindly.

## Required Diagnostics

For every factor or strategy review, produce:

```json
{
  "dataset": {"rows": 0, "start": "", "end": "", "missing": {}},
  "leakage_checks": [{"check": "...", "ok": true, "detail": "..."}],
  "signal_stats": {"coverage": 0.0, "turnover": 0.0, "ic": null, "ir": null},
  "backtest": {"return": null, "sharpe": null, "max_drawdown": null, "trades": null},
  "cost_sensitivity": [{"cost_bps": 0, "metric": "..."}],
  "verdict": "promote|shadow|revise|reject",
  "next_validation": []
}
```

## Anti-Patterns

- Do not evaluate a signal against same-period returns.
- Do not compare strategies over different windows.
- Do not celebrate Sharpe without drawdown and trade count.
- Do not call a factor "robust" without regime or walk-forward checks.
- Do not optimize parameters on the same window used for the final
  performance claim.
