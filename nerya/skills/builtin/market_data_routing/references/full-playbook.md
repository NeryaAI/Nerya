<!-- nerya-skill-frontmatter-start -->
---
name: market_data_routing
description: "Use before market research, backtests, valuation work, or report writing when the agent must choose data sources, symbol formats, freshness checks, and fallbacks across equities, ETFs, crypto, futures, forex, macro, filings, and on-chain data."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Market Data Routing

Use this skill before forming a market view. The job is to decide which
data source is appropriate, prove that the data is fresh enough, and
record fallback quality if the best source is unavailable.

## Source Priority

| Market | Primary | Fallback | Notes |
|---|---|---|---|
| US equities / ETFs | yfinance or venue connector | filings / news search | Verify split-adjusted prices and currency. |
| HK equities | yfinance or local connector | web/news source | Preserve HK ticker suffix and currency. |
| A-shares | local A-share connector | akshare/tushare if installed | Confirm trade calendar and adjusted price mode. |
| Crypto spot/perp | exchange connector / ccxt | public REST endpoint | Record venue, funding, OI, basis when relevant. |
| Options | venue data + implied vol source | public option chain | Never infer Greeks without contract metadata. |
| Macro | official release page | FRED / central bank / stats bureau mirror | Record release date, period, revision status. |
| Filings / fundamentals | official filings API / issuer filings | reputable data vendor | Separate reported, TTM, and estimated values. |
| On-chain | chain RPC / indexed API | block explorer | Record chain, block time, and address scope. |

## Routing Workflow

1. Identify asset class, venue, quote currency, date range, frequency,
   and whether the task needs point-in-time data.
2. Prefer first-class Nerya connectors and built-in market scripts.
   Only write a one-off script when the connector surface cannot answer.
3. Run a small probe first: symbol lookup, one latest quote, one short
   candle slice, or one sample filing. Do not build a full analysis on
   unverified symbol assumptions.
4. Check freshness:
   - intraday trading: latest timestamp within the expected bar delay,
   - daily research: latest close matches the last completed session,
   - macro: release date and period match the requested horizon,
   - filings: report period and filing date are both explicit.
5. If the primary source fails, use the fallback and label the final
   confidence penalty. Never hide degraded data quality.

## Required Evidence Block

Every downstream market report should include:

```json
{
  "data_sources": [
    {
      "dimension": "price|fundamental|news|macro|onchain|flow",
      "source": "connector or URL",
      "symbol": "source-native symbol",
      "as_of": "ISO timestamp or release date",
      "freshness": "fresh|stale|degraded|unknown",
      "fallback_used": false
    }
  ],
  "data_gaps": ["missing option chain", "funding unavailable", "..."]
}
```

## Pitfalls

- Do not mix adjusted and unadjusted price series in one return
  calculation.
- Do not compare fundamentals from different reporting periods.
- Do not treat a news snippet as a source. Fetch the full page or use a
  source with a stable title/date.
- Do not backtest on today's incomplete bar unless the strategy is
  explicitly intraday and timestamp-aware.
- Do not silently switch venues. A Binance BTCUSDT view and an OKX
  BTC-USDT-SWAP view are different instruments.
