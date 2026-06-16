<!-- nerya-skill-frontmatter-start -->
---
name: market_data_routing
description: "Use before research, backtests, valuation, or reports when choosing symbols, venues, data sources, freshness checks, and fallbacks."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Market Data Routing

Use this as a router. For actual data reads, load `markets`,
`research`, `equity_research`, or `sec_filings`.

## Flow

IDENTIFY asset class: equity, ETF, crypto, futures, forex, macro, filing,
or on-chain.
MAP ticker/symbol to the venue format.
CHECK freshness needed by the task.
PICK primary source and fallback.
USE native `market_data` for quote/OHLCV/indicators. USE native `data_api`
for long-tail provider data such as AkShare tables, wallet provider data,
and allowlisted OnchainOS wallet/DeFi reads.
For on-chain meme / DEX / wallet-backed strategies, call
`data_api(op="call", provider="wallet", action="capability_catalog",
args={"topic":"meme"})` or `wallet.meme_strategy_guide` before choosing data
sources. Use `selection.selected_route.call` instead of hardcoding OKX/Byreal:
installed and logged-in wallets provide their own wallet venues, and when no
wallet is ready the returned GOAT/self-custody fallback uses
`wallet_install(provider="self_custody", mode="goat")` and then
`ONCHAIN:<chain>:<token>` plus install recommendations for richer wallets.
Use returned wallet functions and OnchainOS actions for discovery/enrichment,
then use `market_data.get_candles` on the selected route for historical OHLCV.
IF the task will trade or backtest, prefer timestamped market snapshots
over narrative sources.

## Lazy References

- `references/full-playbook.md` for detailed routing tables and source rules.
