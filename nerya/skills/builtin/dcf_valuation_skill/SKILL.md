<!-- nerya-skill-frontmatter-start -->
---
name: dcf_valuation
description: "Use for intrinsic value, DCF, fair value, valuation sensitivity, and price-target analysis; source data needs Financial Datasets credentials or explicit inputs."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# DCF Valuation

Use when the user asks what a company is worth, whether price is above
or below fair value, or how assumptions change intrinsic value.

## Flow

REQUIRE normalized fundamentals from `equity_research` or an explicit
input file.
CHOOSE revenue, margin, reinvestment, terminal, and WACC assumptions.
RUN `scripts/dcf_calc.py`.
LOAD methodology references only when assumptions are contested.
This skill uses the same Financial Datasets keychain as
`equity_research` / `sec_filings`, configured via:

- env: `NERYA_FINANCIAL_DATASETS_KEYS` / `FINANCIAL_DATASETS_API_KEY`
- vault: `vault://financial_datasets.keys` / `vault://financial_datasets_api_key`

REPORT base, bear, bull, sensitivity, and caveats.

## Scripts

- `scripts/dcf_calc.py` for deterministic valuation math.

## Lazy References

- `references/full-playbook.md` for the original detailed workflow.
- `references/wacc_methodology.md` for WACC derivation.
- `references/sector_wacc.md` for sector defaults.
- `references/sensitivity_layout.md` for presentation layout.
- `references/financial-services-financial_analysis-dcf_model.md` for the financial-services upstream workflow.
