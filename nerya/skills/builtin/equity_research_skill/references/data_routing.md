# Equity research data routing

When the agent picks up a research question, it has to decide *which*
endpoint to hit before it spends tokens. Use this table as the routing
table. All scripts referenced live under
`nerya.skills.builtin.equity_research_skill.scripts.*` and ultimately
call `nerya.data.equities.EquitiesClient`.

## Question → endpoint

| Research question shape | Statement(s) | Script invocation |
|-------------------------|--------------|---|
| "How fast is revenue / profit growing?" | `income`, `historical_metrics` | `fetch_financials --json '{"ticker":"X","statements":["income","historical_metrics"],"period":"annual","limit":5}'` |
| "Is the balance sheet healthy?" | `balance` | `fetch_financials --json '{"ticker":"X","statements":["balance"],"period":"annual","limit":5}'` |
| "Can it generate cash?" / "FCF?" | `cashflow` | `fetch_financials --json '{"ticker":"X","statements":["cashflow"],"period":"annual","limit":5}'` |
| "What's the current valuation snapshot?" | `snapshot` | `fetch_financials --json '{"ticker":"X","statements":["snapshot"]}'` |
| "What does the Street expect?" | `analyst_estimates` | `fetch_financials --json '{"ticker":"X","statements":["analyst_estimates"]}'` |
| "Which segment carries growth?" | `segments` | `fetch_financials --json '{"ticker":"X","statements":["segments"],"period":"annual","limit":5}'` |
| "Did it beat / miss recently?" | `earnings` | `fetch_financials --json '{"ticker":"X","statements":["earnings"],"limit":4}'` |
| Latest news headlines | (market data) | `fetch_market_data --json '{"ticker":"X","command":"news","limit":20}'` |
| Insider trades | (market data) | `fetch_market_data --json '{"ticker":"X","command":"insider_trades"}'` |
| Recent prices | (market data) | `fetch_market_data --json '{"ticker":"X","command":"prices","interval":"1d","limit":120}'` |
| Risk factors / MD&A wording | SEC filings | invoke the `sec_filings` skill |
| "What is fair value? Should I buy?" | derived | invoke the `dcf_valuation` skill |

## Period selection

- Most research questions expect **annual** unless the user is asking
  about quarterly momentum ("did Q3 revenue accelerate?").
- For valuation work, prefer 5–10 years of annuals; quarterlies are
  noise.
- For analyst estimates / snapshot endpoints, period is implicit.

## Limit selection

- Default `limit=5` for fundamentals (5 years of annuals = a full
  business cycle for most names).
- `limit=20` for news / insider trades / prices.
- `limit=4` for earnings (last year of quarters).
- Hard cap is enforced inside `fetch_financials.py` to keep tool
  responses small.

## Cost-aware routing

The Financial Datasets API counts every endpoint call. Order of
preference:

1. **`/financials/`** (returns income + balance + cashflow in one call)
   when you need all three — saves 2 calls.
2. **`/financial-metrics/snapshot`** when you just need P/E, ROE,
   margins right now — single call, no period scan.
3. **Avoid** calling `/financials/...` repeatedly for the same ticker
   in the same turn — store the JSON to an artifact and reuse.

## Currency / restatement caveats

Always pair this routing table with `disclosure_caveats.md`. Numbers
without a `source_url` and an `as_of` field are not allowed in the
final memo.
