<!-- nerya-skill-frontmatter-start -->
---
name: sec_filings
description: "Use to list, fetch, and read sections of SEC filings (10-K, 10-Q, 8-K, S-1) for US-listed issuers. Extracts Item 1A Risk Factors, Item 7 MD&A, Item 8 Financial Statements, and other named sections. Triggers when user asks for risk factors, management discussion, MD&A, 10-K, 10-Q, 8-K, S-1, prospectus, or 'what does the latest filing say about ...'. Adapted from dexter (MIT). Requires `NERYA_FINANCIAL_DATASETS_KEYS` / `vault://financial_datasets.keys` (preferred, comma-separated), legacy `FINANCIAL_DATASETS_API_KEY` / `vault://financial_datasets_api_key`."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# SEC Filings Reader

Adapted from `dexter` (https://github.com/virattt/dexter, MIT).

## When to use

- Need risk-factor coverage for a US-listed name
- Need MD&A wording (forward-looking statements, segment commentary)
- Need verbatim quotes for a research memo
- Need to confirm a one-off claim (e.g., "AAPL added a buyback")

## When NOT to use

- Numerical financials → use `equity_research` (faster, cheaper)
- Non-US issuers → not covered by Financial Datasets API
- Pure news / sentiment → use `research`

## Workflow

```
- [ ] Step 1: List recent filings for the ticker
- [ ] Step 2: Pick the right filing (form type + date)
- [ ] Step 3: Fetch a specific section (risk factors / MD&A / item 8)
- [ ] Step 4: Quote-and-cite — never paraphrase a regulated disclosure
              without flagging it as paraphrase
```

## Data sourcing fallback (when `financial_datasets` is unavailable)

The bundled `list_filings.py` and `read_section.py` are the primary
path. If they return `degraded` / empty, route to **edgar MCP** — the
Nerya workspace ships it enabled by default and it covers the same
ground from the SEC's authoritative source (no API key required).

| task | edgar MCP route | typical args |
|---|---|---|
| list filings | `mcp_call(namespace="edgar", tool="list_filings", args={"ticker":"AAPL","form_types":["10-K"],"limit":5})` | `ticker`, optional `form_types`, `limit` |
| download a specific filing | `mcp_call(namespace="edgar", tool="download_filing", args={"accession_number":"0000320193-25-000079"})` | from a prior `list_filings` result |
| risk factors (Item 1A) | `mcp_call(namespace="edgar", tool="get_filing_section", args={"accession_number":"...","section":"risk_factors"})` | section names match the table below |
| MD&A (Item 7) | same `get_filing_section` with `section="management_discussion"` | |
| financial statements (Item 8 / XBRL) | `mcp_call(namespace="edgar", tool="get_xbrl_facts", args={"ticker":"AAPL","concept":"Revenues"})` | XBRL-tagged numerical concepts |

To enumerate all 21 edgar tools, run `mcp_describe(namespace="edgar")`
once at the top of the turn — they'll be auto-promoted to direct call
form for the rest of the conversation (Phase K lazy-loading).

For non-US issuers (Hong Kong, Mainland China, Russia, etc.) edgar
does not apply — fall back to the `research` skill (general web search)
and quote the issuer's own IR page or local exchange disclosure.

## Step 1: List recent filings

```
python -m nerya.skills.builtin.sec_filings_skill.scripts.list_filings \
  --json '{"ticker": "AAPL", "form": "10-K", "limit": 5}'
```

Forms supported: `10-K`, `10-Q`, `8-K`, `S-1`, `DEF 14A`, `4`. Pass an
empty `form` to get the most recent across all types.

## Step 2: Read a section

```
python -m nerya.skills.builtin.sec_filings_skill.scripts.read_section \
  --json '{"ticker": "AAPL", "form": "10-K",
            "section": "risk_factors", "limit": 1}'
```

`section` accepts:

| value | matches |
|-------|---------|
| `risk_factors` | Item 1A — Risk Factors |
| `business` | Item 1 — Business |
| `mdna` / `md_and_a` | Item 7 — MD&A |
| `financial_statements` | Item 8 — Financial Statements |
| `controls` | Item 9A — Controls & Procedures |
| `legal` | Item 3 — Legal Proceedings |
| `cover` | filing cover + summary |

The script returns Markdown with the original headings preserved so the
agent can cite verbatim.

## Step 3: Cite

Every quote must include:

- The form type (e.g., `10-K`)
- The accession number / filing date
- The specific Item / Section
- A direct link to the filing on financialdatasets.ai (the script
  returns `source_url`)

Without all four, do not present the quote.

## Output Contract

```json
{
  "ticker": "AAPL",
  "form": "10-K",
  "filing_date": "2025-11-01",
  "accession_number": "0000320193-25-000123",
  "section": "risk_factors",
  "section_label": "Item 1A — Risk Factors",
  "content_markdown": "...",
  "source_url": "https://api.financialdatasets.ai/filings/...",
  "_envelope": {"source": "financial_datasets", "mode": "live"}
}
```
