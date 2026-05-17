<!-- nerya-skill-frontmatter-start -->
---
name: dcf_valuation
description: "Use to perform a discounted cash flow (DCF) valuation analysis and estimate intrinsic value per share. Triggers when user asks for fair value, intrinsic value, DCF, valuation, 'what is X worth', price target, undervalued/overvalued analysis, or wants to compare current price to fundamental value. Adapted from dexter (MIT) for the deterministic calculator; WACC + sensitivity methodology references adapted from financial-services (Apache-2.0). Requires equity_research skill data + dcf_calc.py."
version: 0.1.0
license: MIT
author: Nerya
requires_integration: financial_datasets
---
<!-- nerya-skill-frontmatter-end -->

# DCF Valuation Skill

Adapted from `dexter` (https://github.com/virattt/dexter, MIT).

## Workflow Checklist

Track progress in your response:

```
DCF Analysis Progress:
- [ ] Step 1: Gather financial data
- [ ] Step 2: Calculate FCF growth rate
- [ ] Step 3: Estimate discount rate (WACC)
- [ ] Step 4: Project future cash flows (Years 1-5 + Terminal)
- [ ] Step 5: Calculate present value and fair value per share
- [ ] Step 6: Run sensitivity analysis
- [ ] Step 7: Validate results
- [ ] Step 8: Present results with caveats
```

## Data sourcing fallback (when `financial_datasets` is unavailable)

The bundled `fetch_financials.py` and `fetch_market_data.py` are the
primary path. If a single field comes back `degraded` / empty, source
it as follows:

| field | fallback | how |
|---|---|---|
| `free_cash_flow`, capex, operating cash flow | yahoo MCP cashflow statement | `mcp_call(namespace="yahoo", tool="get_financial_statement", args={"ticker":"<T>", "financial_type":"cashflow_stmt"})` |
| `market_cap`, `enterprise_value`, `outstanding_shares`, `total_debt`, `cash_and_equivalents`, `debt_to_equity`, beta, P/E | yahoo MCP `get_stock_info` (single call returns ~50 metric fields) | `mcp_call(namespace="yahoo", tool="get_stock_info", args={"ticker":"<T>"})` |
| forward EPS / analyst estimates | yahoo MCP `get_recommendations` | `mcp_call(namespace="yahoo", tool="get_recommendations", args={"ticker":"<T>"})` |
| current price | **native** `market_data` (last close from latest bar) | `market_data(venue="yahoo", market="<T>", interval="1d", count=2)` |
| price history for beta / volatility | **native** `market_data` | `market_data(venue="yahoo", market="<T>", interval="1d", count=252)` |
| risk-free rate (10y treasury) | fred MCP if enabled, else hard-code 4.0% with a flag in `data_gaps` | `mcp_call(namespace="fred", tool="...", args={"series_id":"DGS10"})` |
| sector / industry | yahoo MCP `get_stock_info` (`sector`, `industry` fields) | same call as above |

Routing rules:
- **OHLC always native** (`market_data` venue=yahoo). Never call `mcp_call(yahoo, get_historical_stock_prices, ...)` — that tool is denied at registry-load (Phase L).
- **Fundamentals MCP-first** when the financial_datasets path is degraded. yahoo MCP is free and exhaustive for US large/mid caps.

## Step 1: Gather Financial Data

If `equity_research` already pulled the data and saved an artifact, pass
its path. Otherwise call:

```
python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_financials \
  --json '{"ticker": "<TICKER>",
            "statements": ["cashflow", "snapshot", "balance",
                           "analyst_estimates", "company_facts"],
            "period": "annual", "limit": 5}'
```

Extract from the result:

- **Cash flow** (`cashflow.data`): `free_cash_flow`,
  `net_cash_flow_from_operations`, `capital_expenditure`.
  Fallback: if `free_cash_flow` missing, compute
  `net_cash_flow_from_operations - capital_expenditure`.
- **Snapshot** (`snapshot.data`): `market_cap`, `enterprise_value`,
  `free_cash_flow_growth`, `revenue_growth`,
  `return_on_invested_capital`, `debt_to_equity`,
  `free_cash_flow_per_share`.
- **Balance** (`balance.data`): `total_debt`, `cash_and_equivalents`,
  `current_investments` (default 0 if missing), `outstanding_shares`.
- **Analyst estimates** (`analyst_estimates.data`): forward EPS by year.
- **Company facts** (`company_facts.data`): `sector`, `industry`.
- **Current price** via `fetch_market_data.py --command price`.

## Step 2: Calculate FCF Growth Rate

5-year FCF CAGR from cash flow history. Cross-validate with:
`free_cash_flow_growth` (YoY), `revenue_growth`, analyst EPS growth.

Growth-rate selection:

- Stable FCF history → use CAGR with 10–20% haircut.
- Volatile FCF → weight analyst estimates more heavily.
- **Cap at 15%** (sustained higher growth is rare).

## Step 3: Estimate Discount Rate (WACC)

Use `sector` from company facts to pick the base WACC range from
[sector_wacc.md](references/sector_wacc.md).

Default assumptions:

- Risk-free rate: 4%
- Equity risk premium: 5–6%
- Cost of debt: 5–6% pre-tax (~4% after-tax at 30% tax rate)

Compute WACC using `debt_to_equity` for capital-structure weights.

Reasonableness check: WACC should be 2–4% below
`return_on_invested_capital` for value-creating companies. Apply sector
adjustments from `sector_wacc.md`.

**Need a bottom-up WACC?** If the user pushes back on the sector range,
the company has unusual capital structure (net cash, dual-class equity,
large convertibles), or the deliverable is going to an IC, lazy-load
[wacc_methodology.md](references/wacc_methodology.md) for the full
CAPM + after-tax cost-of-debt + market-cap-weighted derivation.

## Step 4: Project Future Cash Flows

Years 1–5: apply growth rate with 5% annual decay (multiply by 0.95,
0.90, 0.85, 0.80 for years 2–5). Reflects competitive dynamics.

Terminal value: Gordon Growth Model with 2.5% terminal growth (GDP proxy).

## Step 5: Calculate Present Value

Use the bundled deterministic calculator:

```
python -m nerya.skills.builtin.dcf_valuation_skill.scripts.dcf_calc \
  --json '{
    "fcf_base": 100000000000,
    "growth_rate": 0.10,
    "wacc": 0.09,
    "terminal_growth": 0.025,
    "total_debt": 100000000000,
    "cash": 50000000000,
    "current_investments": 0,
    "outstanding_shares": 15000000000,
    "current_price": 220.50
  }'
```

It returns: projected FCF table, terminal value, present values,
enterprise value, equity value, fair value per share, and a 3×3
sensitivity matrix.

## Step 6: Sensitivity Analysis

3×3 matrix: WACC (base ±1%) × terminal growth (2.0%, 2.5%, 3.0%).
The calculator emits this automatically; print as-is in the report.

**Need a richer sensitivity surface?** If the deliverable is going in
front of a decision-maker (IC, PM, formal pitch), lazy-load
[sensitivity_layout.md](references/sensitivity_layout.md) for the
institutional 5×5 / 7×7 odd-grid layout, the symmetric-axis rule, and
the three-table stack (WACC × terminal growth, Y1 growth × terminal
margin, beta × risk-free rate).

## Step 7: Validate Results

Three sanity checks before presenting:

1. **EV comparison**: calculated EV within ±30% of reported
   `enterprise_value`. If off by more, revisit WACC or growth.
2. **Terminal-value ratio**: terminal value should be 50–80% of total
   EV for mature companies. If >90%, growth rate too high; if <40%,
   near-term projections too aggressive.
3. **Per-share cross-check**: compare fair value to
   `free_cash_flow_per_share × 15–25` as rough sanity.

If any check fails, reconsider assumptions before presenting results.

## Step 8: Output Format

Present a structured summary including:

1. Valuation summary — current price vs fair value, upside/downside %.
2. Key inputs table — every assumption with its source.
3. Projected FCF table — 5-year projection with present values.
4. Sensitivity matrix — the 3×3 grid.
5. Caveats — standard DCF limitations + company-specific risks.
