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
READ quotes, candles, and computed features through native `market_data`.
FOR provider-specific tables or analytics beyond quote/OHLCV, use native
`data_api`: list, inspect schema, then call with bounded `limit`/`columns`.
FOR charts, prefer `get_candles` so raw series stay out of context.
CHECK timestamp, venue, and fallback method.
PASS fresh results to `trading`, `backtest`, or `market_research`.

Use a bundled script only for a capability the native tools do not expose and
only when the operator explicitly requests it; `script_run` may require
approval.

## Wallet balance reads

NAME each wallet by its provider id when reporting balances (for
example `provider=self_custody_evm`, `okx_os`, `self_custody_solana`),
so the operator can map every figure to a concrete wallet provider.
IF a wallet provider is not configured, say which provider is missing
and what credential/RPC it needs; never substitute another account's
balance for it.
KEEP only ids, tickers, and field names in English; write the rest of
the sentence in the operator's language (no half-translated phrases
like " bounded 结论" mid-sentence).

## Source availability honesty

IF a data source cannot be reached (network down, credential missing,
provider error), state explicitly which source failed and that the
read 无法获取 / is unavailable from that source, *before* offering any
fallback figure. When the operator says the network is down or a
provider is offline, verify connectivity first and lead with the
failure status; do not silently answer from a different source as if
nothing was wrong.

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
