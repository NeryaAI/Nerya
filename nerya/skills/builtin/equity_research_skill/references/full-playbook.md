<!-- nerya-skill-frontmatter-start -->
---
name: equity_research
description: "Use for end-to-end deep research on a US-listed stock. Pulls income statements, balance sheets, cash flow, key ratios, analyst estimates, SEC filings, and news, then composes an evidence-backed investment memo. Triggers when user asks for company research, fundamental analysis, financial deep dive, fair value, or 'should I buy/sell <ticker>'. Adapted from dexter (MIT). Requires `NERYA_FINANCIAL_DATASETS_KEYS` (comma-separated multi-key for rotation) or `FINANCIAL_DATASETS_API_KEY` (legacy single key), plus vault entry `vault://financial_datasets.keys` / `vault://financial_datasets_api_key`."
version: 0.1.0
license: MIT
author: Nerya
requires_integration: financial_datasets
---
<!-- nerya-skill-frontmatter-end -->

# Equity Research Playbook

Adapted from `dexter` (MIT). Turns a single-ticker question into a full
investment memo backed by live financial data, with self-validation and
data-citation discipline baked in.

## When to use

- User asks for company research, fundamental analysis, fair value, deep dive
- User mentions a US-listed ticker (AAPL, MSFT, NVDA, …)
- The decision needs financial statements / valuation / analyst estimates

## When NOT to use

- Crypto-only questions → use `market_research` + `research`
- Pure technical/chart question → use `market_research`
- Quant signal validation → use `quant_research`
- News-only / sentiment-only question → use `research`
- Formal first-time IB-style coverage initiation report
  (DOCX deliverable, 25-35 charts, JPMorgan/Goldman layout) → use the
  workspace skill `finance/equity_research/initiating_coverage`
  (see "Companion skill" below)

## Companion skill: `initiating_coverage` (workspace-imported)

Nerya's `equity_research_skill` (this file) and the workspace-imported
`finance/equity_research/initiating_coverage` skill (Apache-2.0, from
Anthropic's `financial-services` reference plugin) cover **different
lifecycle stages** of equity research and are intentionally
complementary, not competing. Both share `dcf_valuation_skill` for the
actual valuation math.

| dimension | this skill (`equity_research`) | companion (`initiating_coverage`) |
|---|---|---|
| use case | "should I buy/sell <ticker>?" answered now | first-time formal coverage report |
| deliverable | 1–3 page memo in chat | DOCX + xlsx model + 25–35 PNG charts |
| layout | markdown JSON envelope, chart_block splice | JPMorgan/Goldman/MS institutional, Times New Roman |
| workflow | 1 agent turn, agent-loop friendly | 5 sequential tasks (research → modeling → valuation → charts → assembly), each 10–30 min wall-clock with operator confirmation between stages |
| data side | live `financial_datasets` API + `sec_filings` skill | upstream-prescribed prerequisites verified per task |
| valuation | calls `dcf_valuation` skill once | calls `dcf_valuation` skill in Task 3, then layers comparable-companies analysis |

**Decision rule.** If the user wants the *answer* to a research question
right now, use this skill. If the user wants a *deliverable artifact*
matching IB initiation-report standards (with all the layout / chart-
count / DOCX assembly that implies), route to `initiating_coverage` and
let it drive the operator task-by-task.

The two skills NEVER chain automatically — `initiating_coverage`
explicitly refuses pipeline-mode invocation by design (it asks the user
to pick which of its 5 tasks to start with).

## Workflow Checklist

Copy this checklist into the working response and tick boxes as you go:

```
- [ ] Step 1: Identify ticker + research question
- [ ] Step 2: Pull base data (income, balance, cash flow, snapshot)
- [ ] Step 3: Pull supplementary (analyst estimates, segments, key ratios)
- [ ] Step 4: Pull qualitative (latest 10-K/10-Q via sec_filings, news)
- [ ] Step 5: If valuation asked → invoke dcf_valuation skill
- [ ] Step 6: Cross-check claims, list data gaps
- [ ] Step 7: Hand off to research_report skill for final memo
```

## Data sourcing fallback (when `financial_datasets` is unavailable)

This skill normally pulls everything via the bundled
`fetch_financials.py` + `fetch_market_data.py` scripts (Financial
Datasets API, gated by `NERYA_FINANCIAL_DATASETS_KEYS` / `vault://financial_datasets.keys`
with comma-separated key rotation, plus legacy support for
`FINANCIAL_DATASETS_API_KEY` / `vault://financial_datasets_api_key`).
When
the operator has not configured that integration, the skill is filtered
out at registry-load time and never reaches you. So if you ARE reading
this section, the integration is configured and the bundled scripts
are your primary path.

If a single statement endpoint returns `degraded` / empty, fall back
**in this order** to the free public sources Nerya already has wired up:

| data type | fallback path | example call |
|---|---|---|
| price history (OHLCV) | native `market_data` (uses `YahooFinanceConnector`, **never** the yahoo MCP — see Phase L) | `market_data(venue="yahoo", market="AAPL", interval="1d", count=120)` |
| current quote + key metrics (PE / beta / 52w hi-lo / earnings dates / margins) | yahoo MCP via `mcp_call` | `mcp_call(namespace="yahoo", tool="get_stock_info", args={"ticker":"AAPL"})` |
| income / balance / cashflow statements | yahoo MCP `get_financial_statement` | `mcp_call(namespace="yahoo", tool="get_financial_statement", args={"ticker":"AAPL","financial_type":"income_stmt"})` |
| holders (institutional / insider / mutual fund) | yahoo MCP `get_holder_info` | `mcp_call(namespace="yahoo", tool="get_holder_info", args={"ticker":"AAPL","holder_type":"institutional_holders"})` |
| analyst recommendations (upgrades / downgrades) | yahoo MCP `get_recommendations` | `mcp_call(namespace="yahoo", tool="get_recommendations", args={"ticker":"AAPL"})` |
| dividends / splits / corporate actions | yahoo MCP `get_stock_actions` | `mcp_call(namespace="yahoo", tool="get_stock_actions", args={"ticker":"AAPL"})` |
| options expirations / chains | yahoo MCP `get_option_expiration_dates` + `get_option_chain` | `mcp_call(namespace="yahoo", tool="get_option_chain", args={"ticker":"AAPL","expiration_date":"2026-06-19","option_type":"calls"})` |
| news headlines | yahoo MCP `get_yahoo_finance_news` | `mcp_call(namespace="yahoo", tool="get_yahoo_finance_news", args={"ticker":"AAPL"})` |
| SEC filings (10-K / 10-Q / 8-K text, risk factors, MD&A) | edgar MCP via `sec_filings` skill OR direct `mcp_call` | call `mcp_describe(namespace="edgar")` first to see all 21 tools |
| US economic data (CPI / GDP / rates) | fred MCP if enabled, else `research` skill | `mcp_call(namespace="fred", tool="...", args={...})` |
| general web context | `research` skill | — |

**Never** call `mcp_call(namespace="yahoo", tool="get_historical_stock_prices", ...)` — that tool is denied at registry-load time (Phase L overlap filter). Use native `market_data` for OHLC instead, always.

## Step 1: Identify ticker + research question

Resolve the company name → ticker (Apple → AAPL, Microsoft → MSFT,
Nvidia → NVDA, Alphabet → GOOGL, Meta → META, Tesla → TSLA, …). Write
the research question explicitly — every downstream step refers back to it.

## Step 2: Base data

Run:

```
python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_financials \
  --json '{"ticker": "AAPL",
            "statements": ["income", "balance", "cashflow", "snapshot"],
            "period": "annual", "limit": 5}'
```

Output is JSON: ``{statements: {...}, source_urls: [...], _envelope: ...}``.
Save as an artifact and refer to it by path — do not paste raw API
output back into the conversation.

## Step 3: Supplementary data

```
python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_financials \
  --json '{"ticker": "AAPL",
            "statements": ["historical_metrics", "analyst_estimates",
                           "segments", "earnings"],
            "period": "annual"}'
```

## Step 4: Qualitative context

- Invoke the **`sec_filings`** skill to read the latest 10-K MD&A
  (Item 7) and Risk Factors (Item 1A).
- News:

```
python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_market_data \
  --json '{"ticker": "AAPL", "command": "news", "limit": 20}'
```

- Price history (also emits an interactive K-line chart_block that
  the chat will splice in next to the call — the agent does *not*
  need to also dump the OHLCV in its reply, the chart is
  self-explanatory):

```
python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_market_data \
  --json '{"ticker": "AAPL", "command": "prices", "interval": "day", "limit": 120}'
```

- For multi-source web context, use the `research` skill (multi-engine
  + multi-key search with auto rotation).

## Step 5: Optional valuation

If the question contains "fair value", "intrinsic", "DCF", or "what is
X worth": load the **`dcf_valuation`** skill IMMEDIATELY and follow its
8-step checklist. Pass the artifact paths from steps 2–3 so it does not
re-fetch data.

## Step 6: Cross-check + data gaps

Mandatory before writing the report. For each numerical claim that will
appear in the memo, confirm:

- Latest revenue / EPS within last quarter range?
- Adjusted vs reported — state which one is being quoted.
- Currency: assert USD unless the issuer reports in another currency.
- All figures have a `source_url` in the envelope.

List anything that came back empty or `degraded` as a `data_gap`. Do
not silently fill from memory.

## Step 7: Final memo

Hand off to the **`research_report`** skill using its
`single-name deep dive` template. Required sections:

1. Executive Summary (3–5 bullets, each with evidence)
2. Core Thesis (what would invalidate it)
3. Evidence — Market/Technical, Fundamental/Valuation,
   News/Catalysts, Macro/Flow
4. Risk warnings with triggers + monitoring signals
5. Recommendation (rating, sizing range, entry/exit, review date)
6. Data Appendix (every table has `As of` and `Source` columns)

## Output Contract

Same as `market_research` plus:

```json
{
  "ticker": "...",
  "data_sources": [
    {"endpoint": "...", "url": "...", "as_of": "..."}
  ],
  "valuation": {"method": "dcf|comp|...", "fair_value": null,
                "current_price": null},
  "data_gaps": []
}
```

## Standards

- **Cite or do not say** — every number must point to its `source_url`.
- Separate observed facts, model inference, and recommendation.
- Use `unknown` (not `n/a`, not `0`) for missing data.
- If the user follows up with a trade idea, the trade still has to go
  through Nerya's risk_gate and approval_gate. This skill never bypasses
  either.
