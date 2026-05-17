<!-- nerya-skill-frontmatter-start -->
---
name: markets
description: "Use for current or historical market data, order books, symbol metadata, wallet balances, on-chain activity, and venue capability reads."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Markets

Use for factual reads from markets, exchanges, and chains. Load this
before trading decisions.

## Flow

NORMALIZE market as `VENUE:SYMBOL` when possible.
READ quote/book/candles/symbols/wallet state through a script.
FOR provider-specific tables or analytics beyond quote/OHLCV, use native
`data_api`: list, inspect schema, then call with bounded `limit`/`columns`.
FOR charts, prefer `get_candles` so raw series stay out of context.
CHECK timestamp, venue, and fallback method.
PASS fresh results to `trading`, `backtest`, or `market_research`.

## Scripts

- `scripts/get_quote.py`
- `scripts/get_candles.py`
- `scripts/get_book.py`
- `scripts/list_symbols.py`
- `scripts/wallet_balances.py`
- `scripts/onchain_history.py`

## Lazy References

- `references/full-playbook.md` for detailed read rules and chart behavior.
- `references/libraries.md` for market data libraries.
