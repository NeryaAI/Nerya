# Existing adapter routes

Use scripts/inspect_adapter.py for configured capability truth. SDK or CLI
installation alone is not Nerya JSON-adapter compatibility.

- self_custody: native Solana Jupiter v2 (vault-backed API key required) and BSC Pancake V2. dex_routes allows
  other Router02-compatible EVM DEX deployments. MetaMask reuses the same route
  while preserving its vault-backed seed/key derivation.
- evm_v2: chain,router,wrapped_native,native_symbol,rpc_urls,signer_ref. This is
  Router02 exact-input ABI, not Uniswap V3/UniversalRouter, Curve or Balancer.
  Token contracts are explicit; native and wrapped assets remain distinct.
- Byreal (existing compatibility only, not a default for new strategies): official AMM/RFQ, explicit signer. Pair K lines are not USD prices;
  execution risk uses token USD data. pool@mint keeps market and asset distinct.
- OKX OS: v6 exactIn, chainIndex, slippagePercent=bps/100, autoSlippage=false,
  base58 Solana tx. Existing EVM/Solana signer and RPC verification apply.
- Bitget default official: installed Python script uses subcommands, UI
  amounts, slippage=bps/10000; quote → confirm → make-order → send. Confirm
  minAmount is checked before signing. Local standard EVM/Solana transaction
  formats only; gasless, typed-data and cross-chain orders require separate
  adapters. wallet_address, signer_ref, rpc_urls and token_symbols are supported.
  Full balances use chain RPC; metadata/quotes/candles use the official script.
- Binance default baw: wallet identity is pinned against wallet_address or
  addresses[chain]; quote/slippage uses bps/100. market-order returns orderId,
  later resolved via list to txHash, then independently verified. Current baw
  swap re-quotes without an absolute minimum binding; Nerya rejects this live
  route. Use a reviewed exact-floor external adapter or another DEX provider.
  Native CLI limit orders are not yet part of the generic wallet swap path.
- Coinbase backend=cdp_v2: current CdpClient, existing wallet_address,
  api_key_name/api_private_key, wallet_secret_ref, rpc_urls. Base/Ethereum
  create_swap_quote → validate raw min_to_amount → quote.execute with stable
  idempotency key → RPC receipt. No account creation. Legacy SDK remains
  explicit backend=legacy; a configured Node adapter is preferred over legacy
  SDK for quote-capable operation.
- Legacy stdin adapters: backend=legacy retains existing compatibility entry,
  but must implement confirmation and enforce min_out. Prefer the versioned
  external contract for new adapters; official npm SDKs do not automatically
  implement that protocol.

New-chain strategy funding: funding_tokens[chain]={token:<address>,symbol:USDC}
or another supported USD stable symbol. Defaults remain Solana USDC/BSC USDT/
Base USDC/Ethereum USDC. This is explicit USD-stable funding, not arbitrary
native-token sizing or cross-chain routing.

Official protocol references checked 2026-09-21:
- https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet
- https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill
- https://github.com/coinbase/cdp-sdk/tree/main/python/cdp/actions/evm/swap

Refresh upstream contracts when the installed version changes; fixtures must
model actual response structure, including pending and failure responses.
