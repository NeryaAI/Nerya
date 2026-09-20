# Source, Venue and Wallet Routing

Determine the asset class, market/symbol, requested provider and freshness
requirement before selecting a route. Use `connector_list`/`connector_view`
for installed capability discovery, `market_data` for ticker/OHLCV/features,
and `data_api` list/schema/call for provider-specific bounded reads.
Follow returned `selected_route`, `next_required_action` or `bounded_sequence`
exactly; do not guess endpoint names or replace an operator-named provider.

For wallet/on-chain/meme strategies, inspect the wallet capability catalog or
returned strategy guide through `data_api` before selecting a venue. Use the
returned wallet functions for enrichment and the selected `market_data`
route for historical OHLCV. Do not hardcode a wallet, RPC or exchange merely
because it was used in an old example. Installation recommendations require
the actual `wallet_install` policy; reading this reference does not install.

Label each wallet with `provider=<exact-id>`. Missing credentials or network
failures must be reported for that provider before any explicitly labelled
fallback. Never substitute a different account's balance. Trading/backtests
need timestamped data and must not consume narrative statements as prices.

Detailed historical tables remain available with
`Skill(skill="market_data_routing", file="references/full-playbook.md")`.
The live connector/data schema takes precedence over old example routes.
