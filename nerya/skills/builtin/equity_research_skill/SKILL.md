<!-- nerya-skill-frontmatter-start -->
---
name: equity_research
description: "Use for deep research on a US-listed stock: financials, filings, estimates, news, valuation, and memo output."
version: 0.1.0
license: MIT
author: Nerya
requires_integration: financial_datasets
---
<!-- nerya-skill-frontmatter-end -->

# Equity Research

Use for company-level fundamental research, buy/sell questions, fair
value work, or investment memos.

## Flow

NORMALIZE ticker and data route.
FETCH statements, ratios, estimates, price, and news.
LOAD `sec_filings` for 10-K, 10-Q, 8-K, MD&A, and risk factors.
LOAD `dcf_valuation` when valuation or price target is needed.
COMPARE thesis, catalysts, risks, and disconfirming evidence.
WRITE a memo only after sources are gathered.

## Scripts

- `scripts/fetch_financials.py` for financial datasets.
- `scripts/fetch_market_data.py` for price/market data.

## Configuration

This skill needs the Financial Datasets keychain. Configure one of:

- env: `NERYA_FINANCIAL_DATASETS_KEYS` (preferred, comma-separated) or
  `FINANCIAL_DATASETS_API_KEY` (legacy single key)
- vault: `vault://financial_datasets.keys` (preferred, comma-separated) or
  `vault://financial_datasets_api_key` (legacy single key)

When quotas or auth fail, key rotation runs automatically across all
configured keys. If no key is configured, the skill returns a setup prompt
and supports masked chat intake for new keys.

## Lazy References

- `references/full-playbook.md` for the original detailed workflow.
- `references/data_routing.md` for source selection and fallbacks.
- `references/disclosure_caveats.md` for memo caveats.
- `references/financial-services-equity_research-initiating_coverage.md` for the financial-services upstream workflow.
