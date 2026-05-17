# SEC filing anatomy — picking the right form / section

This reference is a quick lookup for the agent. When the user asks a
qualitative question, decide first **which form** carries the answer,
then **which section** inside it.

## Form types

| Form | What it is | Filed by | Cadence | Where it shines |
|------|------------|----------|---------|-----------------|
| **10-K** | Annual report | US-listed issuers | yearly | full risk factors, MD&A, audited financials, governance |
| **10-Q** | Quarterly report | US-listed issuers | quarterly | unaudited financials, MD&A delta vs last 10-K |
| **8-K** | Current report (material event) | US-listed issuers | within 4 business days | M&A, executive changes, restatements, guidance, Reg FD disclosures |
| **S-1** | IPO registration | new issuers | once (with amendments) | use of proceeds, lock-ups, cap table, biggest risks |
| **DEF 14A** | Proxy statement | US-listed issuers | yearly (pre-AGM) | exec comp, board composition, shareholder proposals |
| **20-F** | Annual report (FPI) | foreign private issuers | yearly | foreign-listed analog of 10-K |
| **6-K** | Current report (FPI) | foreign private issuers | as required | foreign-listed analog of 8-K |
| **Form 4** | Insider transactions | insiders | within 2 business days | insider buys / sells / option exercises |
| **13F** | Institutional holdings | 13F filers | quarterly | what the big holders own |

> The Financial Datasets `/filings/` endpoint surfaces 10-K / 10-Q /
> 8-K / S-1 / DEF 14A / Form 4 with full text. For 13F or non-listed
> issuers fall back to direct EDGAR.

## 10-K section map (the one the agent will read most)

| Item | Title | Why you'd open it |
|------|-------|-------------------|
| 1 | Business | what the company actually sells; segments; competition |
| 1A | Risk Factors | the issuer's own list of things that could go wrong |
| 1B | Unresolved staff comments | rare — pending SEC questions |
| 2 | Properties | real-estate / capacity (more useful for industrials) |
| 3 | Legal Proceedings | material lawsuits |
| 4 | Mine safety / other | mostly N/A for non-mining names |
| 5 | Market for registrant's common equity | dividends, buybacks, performance graph |
| 6 | (Reserved) | empty since 2021 SEC simplification |
| 7 | **MD&A** | management's narrative; segment trends; liquidity |
| 7A | Quantitative / qualitative market risk | hedging posture, FX exposure |
| 8 | **Financial Statements** | audited income / balance / cashflow + footnotes |
| 9 | Changes in / disagreements with accountants | red-flag check |
| 9A | **Controls and Procedures** | material weakness disclosures |
| 9B | Other information | rare disclosures |
| 9C | Foreign jurisdictions inspection | China-listed names |
| 10 | Directors / governance | board composition |
| 11 | Executive Compensation | (also in proxy) |
| 12 | Security ownership | beneficial ownership table |
| 13 | Related-party transactions | conflicts of interest |
| 14 | Auditor fees | tied to 10-K |
| 15 | Exhibits | contracts, subsidiary list |

## 10-Q section map (slimmer)

- **Part I — Financial Information**
  - Item 1: Financial Statements (unaudited)
  - Item 2: MD&A (delta vs prior period)
  - Item 3: Quantitative / qualitative market risk
  - Item 4: Controls and Procedures
- **Part II — Other Information**
  - Item 1: Legal proceedings (delta)
  - Item 1A: Risk Factor updates (delta)
  - Item 2: Equity sales / repurchases
  - Items 3–6: defaults, mine safety, other, exhibits

## 8-K item code map

| Item | Topic |
|------|-------|
| 1.01 | Entry into a Material Definitive Agreement |
| 1.02 | Termination of a Material Definitive Agreement |
| 2.01 | Completion of Acquisition or Disposition |
| 2.02 | Results of Operations and Financial Condition (earnings) |
| 2.03 | Material direct financial obligation |
| 2.04 | Triggering event accelerating direct obligation |
| 2.05 | Costs associated with exit / disposal activities |
| 2.06 | Material impairments |
| 3.01 | Notice of delisting / failure to satisfy continued listing rule |
| 4.01 | Changes in registrant's certifying accountant |
| 4.02 | Non-reliance on previously issued financial statements (RESTATEMENT — read every time) |
| 5.01 | Changes in control |
| 5.02 | Departure / appointment of directors / officers |
| 7.01 | Reg FD disclosure |
| 8.01 | Other events |
| 9.01 | Financial statements and exhibits |

## Which section to ask for via the script

The `read_section.py` script accepts these `section` keys (already
mapped to the regex catalogue):

- `risk_factors` — Item 1A (10-K) / Part II Item 1A (10-Q)
- `business` — Item 1
- `mdna` / `md_and_a` — Item 7 (10-K) / Part I Item 2 (10-Q)
- `financial_statements` — Item 8 (10-K) / Part I Item 1 (10-Q)
- `controls` — Item 9A (10-K) / Part I Item 4 (10-Q)
- `legal` — Item 3 (10-K) / Part II Item 1 (10-Q)
- `cover` — first ~400 lines (filing summary, registrant, period)

If the user asks for "what guidance did they raise?", call:

```
read_section --json '{"ticker": "X", "form": "8-K", "section": "cover"}'
```

and rely on the matched-heading regex to surface item 2.02 / item 7.01
content.

## Citation discipline

Every quote pulled out of a filing must travel with these four
fields back to the memo:

1. `form` (e.g. `10-K`)
2. `accession_number` or `filing_date`
3. `section_label` (returned by the script)
4. `source_url`

If any one is missing, do not present the quote in the memo —
re-fetch first.
