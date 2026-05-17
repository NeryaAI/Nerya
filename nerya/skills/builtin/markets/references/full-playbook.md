<!-- nerya-skill-frontmatter-start -->
---
name: markets
description: "Use whenever the agent needs current or historical market data, exchange order-book / fee / symbol metadata, on-chain balances, wallet info, or token transfers \u2014 basically any *read* of the outside financial world. Triggers on phrases like \"what's the price of\", \"show me the order book\", \"exchange fees\", \"list my wallets\", \"check this address\", \"transfer USDC\", or any question about market state, on-chain activity, or venue capabilities. Use this skill *before* trading; the trading playbook depends on these reads being current."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Markets playbook

The markets skill wraps every read against external venues and
chains. It splits cleanly into two sub-domains:

- **Centralised exchanges (CEX)** — quotes, books, fees, symbol
  metadata, recent fills.
- **On-chain** — token balances, transaction history, transfers,
  wallet management.

Both share a single rule: the data is *snapshots*. Treat anything
older than a few seconds as stale for trading decisions, and always
note the timestamp the runtime returns.

## Choosing a data source

For prices and books, prefer the venue you'd execute on. Pulling a
quote from a different exchange than where you'd place the order
introduces a basis you'll forget about by the time you size the
trade.

For chain reads, prefer:

1. The native node/indexer when the network has one (Solana RPC,
   Hyperliquid info endpoint).
2. A trusted third-party indexer second (Etherscan-class).
3. Cached/last-known state only as a tie-breaker, never authoritative.

## Bundled scripts

| Script | Purpose |
|---|---|
| `scripts/get_quote.py` | Single-symbol quote on a named venue. |
| `scripts/get_candles.py` | Historical OHLCV K-line (emits an interactive chart block). |
| `scripts/get_book.py` | Top-of-book / depth snapshot. |
| `scripts/list_symbols.py` | Trade-able universe + min size / tick. |
| `scripts/wallet_balances.py` | Per-wallet token balances. |
| `scripts/onchain_history.py` | Recent transfers for an address. |

### Charts: prefer `get_candles` over re-rendering quotes

If the user wants to *see* price history (rather than just read the
last close), call `get_candles` instead of looping `get_quote`. The
script emits a `chart_blocks` field that the kernel splices into the
chat as an interactive lightweight-charts canvas — the user gets a
zoomable K-line, you don't burn tokens echoing each candle into the
LLM context.

You decide the data path:

- **`path: "bulk"` (default with workspace)** — large or unknown-size
  data. Series payload is persisted to `artifacts/charts/<id>.json`;
  the chart block keeps only a URI reference. Best for >50 candles or
  any time you'd rather not see raw OHLCV in your own context.
- **`path: "inline"`** — small data (~tens of candles) you also want
  to *read* in the same response. Embeds the points directly in the
  block; capped at 256 KB by the composer's OOM guardrail.

All scripts read JSON payload from `--json` / `--payload-file` /
stdin and emit JSON on stdout. Invoke them from `run_shell` and
parse the result.

## Common failure modes

- **Mixing testnet and mainnet symbols.** Always confirm the chain /
  network field in the response before you trust a balance.
- **Rate-limit clusters.** If you call `get_quote` in a tight loop,
  use `list_symbols` once and cache locally for the loop's lifetime.
- **Treating a 404 as zero.** Missing data is missing, not zero.
  Surface the absence; do not pretend the symbol is empty.
