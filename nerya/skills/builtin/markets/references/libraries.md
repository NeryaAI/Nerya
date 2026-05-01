# Markets — libraries and adapters

Reach for these before writing your own venue or chain client.

## Internal Nerya modules

| Module | What it does |
|---|---|
| `nerya.trading.exchanges.<venue>` | Per-CEX adapter (Binance, Bybit, Hyperliquid, …). Each exposes `quote()`, `book()`, `symbols()`, and a `submit_order()` not used by this skill. |
| `nerya.markets.onchain` | Chain-agnostic helpers (balance, history, transfer). Routes to the right RPC under the hood. |
| `nerya.markets.wallet` | Wallet roster + key handling. Use this — never load private keys directly. |
| `nerya.core.paths.WorkspacePaths` | Where to read cached market data and write new snapshots. |

## External libraries

- **ccxt** — generic CEX wrapper. Useful for venues we haven't
  written a native adapter for; less precise than a native adapter
  on rate-limit and error handling.
- **web3.py** — Ethereum + EVM-compatible chain interaction. Use a
  block-tag-aware call (`block_identifier="latest"`) when reading
  state to avoid stale RPC caches.
- **solana** + **solders** — Solana RPC + signing. Pin to the same
  versions used by `nerya.markets.onchain` to avoid the
  serialisation skew between releases.
- **httpx** — outbound HTTP for venues we hit directly. Always set a
  timeout; never use the default.

## Endpoints discipline

- **Don't hardcode RPCs.** Read the endpoint from `nerya.yml` /
  `WorkspacePaths`. Operators rotate keys.
- **Respect rate limits.** Burst gets you banned. Use the venue's
  documented rate; back off on 429 with jitter.
- **Cache symbol lists for the lifetime of a script.** Symbols
  rarely change within a single run; re-fetching them is just
  noise.

## Common shapes

A "quote" payload is `{venue, symbol, ts, bid, ask, mid}`. A "book"
payload is `{venue, symbol, ts, bids: [[price, size], ...], asks:
[...]}`. Stick to these so downstream scripts can be written once.

A balance payload is `{chain, address, asset, amount, decimals,
ts}`. The `decimals` field matters — never compare amounts across
assets without normalising via decimals first.
