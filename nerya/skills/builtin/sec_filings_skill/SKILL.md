<!-- nerya-skill-frontmatter-start -->
---
name: sec_filings
description: "Use to list, fetch, and read SEC filing sections such as 10-K, 10-Q, 8-K, S-1, risk factors, MD&A, and financial statements."
version: 0.1.0
license: MIT
author: Nerya
requires_integration: financial_datasets
---
<!-- nerya-skill-frontmatter-end -->

# SEC Filings

Use when the user asks what filings say or when equity research needs
primary-source disclosure.

## Flow

NORMALIZE ticker and filing type.
LIST relevant filings before reading sections.
READ only the section needed: risk factors, MD&A, business, notes, or
financial statements.
QUOTE sparingly and cite filing/date.
PASS extracted facts to `equity_research` or `research_report`.

## Configuration

Set one of:

- env: `NERYA_FINANCIAL_DATASETS_KEYS` (preferred, comma-separated) or
  `FINANCIAL_DATASETS_API_KEY` (legacy single key)
- vault: `vault://financial_datasets.keys` (preferred, comma-separated) or
  `vault://financial_datasets_api_key` (legacy single key)

If no key is configured, the flow returns a masked setup prompt and allows
new keys to be sent via chat for on-the-fly vault-backed intake.

## Scripts

- `scripts/list_filings.py`
- `scripts/read_section.py`

## Lazy References

- `references/full-playbook.md` for the original detailed workflow.
- `references/filing_anatomy.md` for section mapping.
