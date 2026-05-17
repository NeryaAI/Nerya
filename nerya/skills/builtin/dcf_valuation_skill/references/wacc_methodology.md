# WACC Methodology

Methodology distilled from `financial-services/plugins/vertical-plugins/financial-analysis/skills/dcf-model` (Apache-2.0), Steps 4–6 of the upstream playbook. Lazy-load this file when the lightweight Step 3 in `SKILL.md` is not enough — typical triggers are: user asks "what WACC are you using?", company has unusual capital structure (net cash, dual-class equity, large convertibles), or the sensitivity sweep needs WACC inputs sourced from first-principles rather than the sector table.

## When to use this file vs `sector_wacc.md`

- `sector_wacc.md` — first pass: pick a base WACC band from the sector table, apply +/- adjustments, done. Fast, good enough for "screen 50 names".
- `wacc_methodology.md` (this file) — second pass: derive WACC bottom-up from CAPM + observed cost of debt + market-cap weights. Use when the answer must withstand IC-grade scrutiny.

## CAPM cost of equity

```
Cost of Equity = Risk-Free Rate + Beta × Equity Risk Premium
```

| Input | Source | Notes |
|---|---|---|
| Risk-Free Rate | current 10Y Treasury yield (UST10Y) | Use the real-time yield, not last quarter end |
| Beta | 5-year monthly stock beta vs broad market index | Daily 1Y betas are noisier; smooth with 5Y monthly |
| Equity Risk Premium | 5.0–6.0% (market consensus band) | Damodaran updates ~monthly; institutional desks use 5.5% as a working default |

Special case: **negative beta** (rare; mostly gold miners, vol products). The CAPM still holds; do not floor at zero.

## After-tax cost of debt

```
After-Tax Cost of Debt = Pre-Tax Cost of Debt × (1 − Marginal Tax Rate)
```

Pre-tax cost of debt sources, in order of preference:

1. **Yield on the company's longest outstanding bond** (CDS or bond yield) — most accurate.
2. **Implied yield from credit rating** — if rating is BBB, use BBB index yield.
3. **Interest expense ÷ average total debt** — last-resort accounting proxy; underestimates cost when most debt is fixed-rate and old.

Marginal tax rate is the statutory rate (US: 21% federal + state blend ~25–28%), not the effective rate from filings — effective rate is depressed by one-time items.

## Capital structure weights (use **market** values)

```
Market Cap        = Stock Price × Diluted Shares Outstanding
Net Debt          = Total Debt − Cash & Marketable Securities
Enterprise Value  = Market Cap + Net Debt
Equity Weight     = Market Cap / EV
Debt Weight       = Net Debt / EV
WACC              = (Cost of Equity × Equity Weight)
                  + (After-Tax Cost of Debt × Debt Weight)
```

Two non-obvious cases the lightweight Step 3 path glosses over:

- **Net-cash position** (Cash > Total Debt → Net Debt < 0): Debt Weight is *negative*, which lowers WACC because the company is partially equity-funding a debt-like asset (cash). Do not coerce to zero — the negative weight is correct and matches how a buyer would price the firm.
- **Zero-debt company**: WACC collapses to Cost of Equity. No need to invent a synthetic debt structure.

## Typical WACC bands (sanity check)

| Profile | Band |
|---|---|
| Large-cap, stable, defensive | 7–9% |
| Growth (consistent FCF, expanding margin) | 9–12% |
| High-growth / cyclical / single-product | 12–15% |
| Distressed / negative FCF | model with multiple discount rates per stage |

If the bottom-up WACC lands more than 200 bps outside the sector band from `sector_wacc.md`, stop and reconcile before discounting — a mis-specified WACC dominates everything else.

## Constraint: WACC must exceed terminal growth

Gordon Growth requires `WACC > terminal_growth`. If the calculation produces `WACC ≤ g_terminal`, the terminal value is undefined (or negative), the model is broken, and the agent must surface this rather than "round" or "cap". Most often this is a stale risk-free rate or a too-aggressive terminal growth rate; recheck both inputs.

## Provenance

Methodology adapted from Anthropic's `financial-services` reference plugin (commit on `main`, Apache-2.0). The numerical implementation lives in `scripts/dcf_calc.py` — this file documents the *inputs and bands* the agent should choose before calling the calculator.
