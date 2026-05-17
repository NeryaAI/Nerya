# Sensitivity Layout

Layout discipline distilled from `financial-services/.../dcf-model` (Apache-2.0, Step 10 + `<correct_patterns>`). Lazy-load when the default 3×3 sensitivity in `dcf_calc.py` is not enough — typical triggers are: presenting to an IC, the user asks for a "fan chart of valuations", or the model output will be archived as evidence for a thesis.

## Why upgrade from 3×3 to 5×5 (or 7×7)

The default `dcf_calc.py` emits a **3×3** sensitivity grid (WACC ±100 bps × terminal growth 2.0/2.5/3.0%). That is enough to answer "is the answer fragile?" but not enough to *visualize* the valuation surface. When the deliverable is going in front of a decision-maker, switch to an **odd-dimension grid (5×5 standard, 7×7 for high-conviction names)** and stack three of them.

Odd dimensions are non-negotiable: with an even grid there is no center cell, so the base case has nowhere to anchor, and a reader cannot tell at a glance which scenario is "the model's actual answer."

## Axis construction rule

```
axis_values = [base − 2·step, base − step, base, base + step, base + 2·step]
```

`step` is chosen so the outer edge brackets a "credible but uncomfortable" assumption. Defaults:

| Axis | base | step (5×5) | step (7×7) |
|---|---|---|---|
| WACC | model's WACC | 50 bps | 25 bps |
| Terminal growth | 2.5% | 50 bps | 25 bps |
| Revenue growth (Y1) | model's Y1 growth | 200 bps | 100 bps |
| EBIT margin (terminal year) | model's terminal margin | 100 bps | 50 bps |
| Beta | model's beta | 0.10 | 0.05 |
| Risk-free rate | current UST10Y | 50 bps | 25 bps |

The **center cell** of each table MUST output the model's actual implied per-share value. If it does not, the table is broken (typically the axis values were not set to the model's actuals — fix that before reading anything else from the grid).

## Three-table stack (the institutional layout)

Stack three sensitivity tables vertically, in this order:

1. **WACC × Terminal Growth** — the dominant valuation lever. If the answer is fragile here, nothing else matters.
2. **Revenue Growth (Y1) × EBIT Margin (terminal)** — operating-leverage view. Tests whether the thesis depends on simultaneously hitting both top-line and margin expansion.
3. **Beta × Risk-Free Rate** — cost-of-capital decomposition. Mostly relevant when the WACC band is contested or the company is rate-sensitive (REITs, utilities, financials).

Each table independently shows base-case-centered output. A reader scans the diagonals to see how quickly the answer changes with simultaneous moves in two assumptions.

## Center-cell anchor + visual cue

In every table, anchor the center cell visually:

- **Bold** the center value.
- Optionally tint it (Nerya markdown reports use `**$XX.XX**` and a footnote; in xlsx outputs, the upstream skill recommends fill `#BDD7EE`).
- Footnote: "center cell = base case; output equals model's implied price."

If a reader cannot find the base case in under five seconds, the table fails its job.

## What this skill does NOT do

- Does **not** emit `.xlsx` files. The upstream skill builds Office-grade Excel via Office JS or openpyxl; Nerya's contract is "agent emits a JSON model + a deliverable artifact path", with the consumer (often `messaging.pipeline`) responsible for any Excel rendering.
- Does **not** implement Excel's `Data → What-If Analysis → Data Table` feature. Even when an xlsx artifact is the final deliverable, sensitivity cells are written as explicit recalculation formulas — by the consumer, not by this skill.
- Does **not** introduce conditional formatting (`#BDD7EE` fills, green-scale gradients). Those are presentation concerns and live in the consumer side.

## Relationship to `dcf_calc.py`

`scripts/dcf_calc.py` already supports an arbitrary `sensitivity_axes` parameter shape (it returns a `sensitivity_axes: {wacc, terminal_growth}` field). To produce the 5×5 grids described above, the agent should:

1. Compute the base WACC + base terminal growth.
2. Build the symmetric `axis_values` lists per the rule above.
3. Call `dcf_calc.py` once per cell, OR (preferred) extend the calculator to accept full lists and emit a 5×5 matrix in one call.

For the second-axis (Y1 growth × terminal margin) and third-axis (beta × RFR) tables, the agent currently has to call the calculator multiple times with adjusted base inputs — these are not first-class parameters in `dcf_calc.py`. Promoting them to first-class is a future PatchProposal.

## Provenance

Layout discipline adapted from Anthropic's `financial-services` reference plugin (Apache-2.0). The 5×5 / odd-grid / center-anchor convention is upstream's; the explicit non-goals (no xlsx writing, no Office JS, no Data Table) are Nerya's deliberate constraint to preserve agent-loop / headless friendliness.
