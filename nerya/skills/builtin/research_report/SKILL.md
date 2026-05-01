<!-- nerya-skill-frontmatter-start -->
---
name: research_report
description: "Use to write professional market, stock, sector, strategy, backtest, or flash research reports with ratings, evidence tables, risk warnings, and clear investment conclusions."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Research Report Writing

This skill formats analysis into an operator-ready investment research
report. It should be used after data collection and analysis, not as a
substitute for evidence.

## Report Types

| Type | Use When | Typical Length |
|---|---|---|
| Single-name deep dive | equity, token, ETF, issuer | 1500-4000 words |
| Sector or theme report | industry, narrative, policy theme | 1200-3000 words |
| Macro strategy note | rates, FX, cross-asset allocation | 1000-2500 words |
| Backtest report | strategy validation or factor review | 800-2000 words |
| Flash note | news or event response | 400-1000 words |

## Standard Structure

```markdown
# [Title]

> Rating: Buy | Overweight | Hold | Underweight | Sell
> Time horizon: intraday | swing | 3-6 months | 12 months
> Analyst: Nerya
> Date: YYYY-MM-DD

## Executive Summary
- 3-5 key points, each backed by evidence.

## Core Thesis
Explain the main view, why it matters now, and what would invalidate it.

## Evidence
### Market / Technical
### Fundamentals / Valuation
### News / Sentiment / Catalysts
### Macro / Flow / Positioning

## Risk Warnings
1. Risk, trigger, likely impact, and monitoring signal.

## Recommendation
Action, sizing range, entry/exit logic, stop or invalidation, review date.

## Data Appendix
Tables of key metrics and source timestamps.

Disclaimer: AI-generated research for reference only; not investment advice.
```

## Rating Scale

| Rating | Meaning |
|---|---|
| Buy | strong positive expected return with acceptable risk |
| Overweight | constructive but size gradually or wait for trigger |
| Hold | no clear edge or already fairly priced |
| Underweight | trim exposure or avoid new entries |
| Sell | exit or avoid due to negative risk/reward |

For trading strategies:

| Rating | Condition |
|---|---|
| High allocation priority | robust return, drawdown controlled, validated out of sample |
| Allocatable | positive edge with known limitations |
| Watch | promising but needs more validation |
| Not recommended | poor risk/reward or unreliable evidence |

## Evidence Rules

- Every table needs an `As of` or `Period` column.
- Separate actuals from estimates with `A` and `E` suffixes.
- State source quality: primary, vendor, scraped, fallback, or
  operator-supplied.
- If the report includes a target price or expected return, explain the
  method: valuation multiple, DCF, scenario model, technical objective,
  or historical analogue.
- Risk warnings must include triggers, not generic disclaimers.

## Backtest Report Addendum

When writing a strategy or factor report, include:

| Metric | Strategy | Benchmark | Comment |
|---|---:|---:|---|
| Cumulative return | | | |
| Annualized return | | | |
| Sharpe | | | |
| Max drawdown | | | |
| Calmar | | | |
| Win rate | | | |
| Trades | | | |

Then add sections for leakage checks, cost sensitivity, worst periods,
and promotion blockers.
