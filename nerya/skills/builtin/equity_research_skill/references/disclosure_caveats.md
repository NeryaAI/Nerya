# Disclosure caveats — read before you cite

Every number you put into the final memo carries a chain-of-custody
risk. This page lists the failure modes the agent has to think about
*before* it writes "Revenue grew 12% YoY".

## Restatement risk

- Companies can re-issue prior-period financials (M&A, SEC inquiries,
  segment changes). The Financial Datasets API serves the **most
  recent** version on file; older numbers may not match what was
  originally reported.
- Action: when comparing historical revenue / EPS / FCF series, prefer
  pulling the full 5–10 year history in **one** call so all rows come
  from the same as-of snapshot.

## Reporting lag

| Filing | Typical lag |
|--------|-------------|
| 10-Q   | 30–45 days after quarter end |
| 10-K   | 60–90 days after fiscal year end |
| 8-K (material events) | within 4 business days |
| Insider Form 4 | 2 business days |

If today is within the typical lag window after quarter / year end,
**explicitly state** that the most recent quarter / year is not yet
reported and use the prior period as the latest data point.

## Adjusted vs reported (GAAP / non-GAAP)

- Income statement items returned by the API are GAAP unless the
  field name explicitly says "adjusted".
- Analyst-estimate consensus for EPS is almost always **non-GAAP**
  (excludes SBC, restructuring, etc.).
- Action: when presenting an EPS beat / miss, state which basis is
  being compared.

## Currency / cross-listing

- US-listed ADRs (BABA, TSM, NVO, …) report in USD on the API but the
  underlying issuer files locally in CNY / TWD / DKK. Do not double-
  convert.
- Multi-listed names (RIO LSE vs RIO ASX) may have different share
  counts; assert which listing the snapshot refers to.

## Missing fields

The API returns `null` for fields the issuer did not disclose
(e.g. segments for a single-segment company). Treat `null` as
`unknown` and surface it in the `data_gaps` array — never substitute
with `0` or "n/a" prose.

## Insider trades nuance

- Form 4 filings include planned 10b5-1 sales. The API tags the trade
  type when available; if absent, assume the sale could be planned
  (do not interpret as a directional signal alone).
- Buy / sell quantities are aggregate at the row level — sum across
  rows for total insider activity over the period.

## News / sentiment guardrails

- News rows include a `published_at` timestamp. Only cite headlines
  newer than the user's stated reference date.
- Do not infer sentiment from a single headline; cross-check against
  prices in the same window.

## SEC filings provenance

When quoting a Risk Factor or MD&A passage, your citation must include:

1. Form type (10-K / 10-Q / 8-K)
2. Accession number or filing date
3. Item / Section number
4. The `source_url` returned by the `sec_filings` script

Without all four, do not present the quote.

## Final review checklist

Before composing the memo, the agent confirms:

- [ ] Every numeric claim has a `source_url`
- [ ] Every claim has an `as_of` date in the appendix
- [ ] Adjusted vs reported is explicit where it matters
- [ ] Currency is asserted (USD unless cross-listed)
- [ ] `data_gaps` lists every `null` / `degraded` envelope encountered
- [ ] No silent fill from model memory
