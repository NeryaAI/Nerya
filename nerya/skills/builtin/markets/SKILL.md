<!-- nerya-skill-frontmatter-start -->
---
name: markets
description: "Use for current or historical market data, order books, symbol metadata, wallet balances, on-chain activity, and venue capability reads."
version: 0.1.0
license: MIT
author: Nerya
metadata:
  nerya:
    script_policy:
      read_only:
        - get_quote.py
        - get_candles.py
        - get_book.py
        - list_symbols.py
        - ranked_universe.py
        - onchain_history.py
        - wallet_balances.py
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

## Ranked universes (top N by market cap, volume, etc.)

For crypto market-cap requests, use ONE native call:
`market_data(action="ranked_universe", venue="binance", count=N,
rank_by="market_cap", quote="USDT")`. It performs the ranking read and venue
mapping together and returns ordered `market_ids`, plus skipped/substituted
assets. Do not search for another market-cap source after this succeeds.
The native action also defaults a missing venue to Binance for this specific
crypto ranking path and returns `venue_mapping_complete`. When it is true,
`market_ids` are already venue-validated; do not call `list_symbols` again.

For other ranked sources, use ONE authoritative ranking response, then ONE
`market_data(action="list_symbols", venue="...")` call to map that ordered list
onto active venue markets. Preserve ranking order; if an asset has no usable
market on the requested venue, skip it and continue down the same ranking until
the requested count of tradable markets is reached, and report the skipped
asset/substitution.

Do not call `data_api`, web search/fetch, or shell for crypto market-cap ranking
when `ranked_universe` succeeds. Do not re-fetch the same ranking, launch
per-symbol urllib/curl/python probes,
run exchange checks through `run_shell`, inspect Git state, or hunt for the web
fetch's cached artifact just to reconstruct data already returned by a tool.
The tool result is the evidence. Only retry a source once when the first request
actually failed, and then move on or state the limitation.

For wallet/DEX/prediction-market integration failures or adding a provider, load
`adapter` to inspect the exact binding and public execution contract.

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

Read a selected reference with `Skill(skill="markets", file="<path>")`.
Use `references/source-routing.md` when source, provider, venue or wallet
selection is unclear; `market_data_routing` is a compatibility entry only.

- `references/full-playbook.md` for detailed read rules and chart behavior.
- `references/libraries.md` for market data libraries.
