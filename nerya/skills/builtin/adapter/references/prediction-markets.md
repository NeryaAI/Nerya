# Prediction-market adapters

The built-in prediction connector is Polymarket. Find it through connector_list
and connector_describe; no Kalshi connector is currently provided. Prediction
markets use the ordinary trading plan/risk/approval/order-tracker path, not
wallet.swap. A new venue belongs in ExchangeProviderSpec + a Connector.

## Identity and data

Use POLYMARKET:<decimal CLOB outcome token id>, or POLYMARKET:<slug>#<outcome>.
Outcome names are explicit: Yes and No are different assets. A condition id is
a group of outcomes and is not a tradable token. scripts/inspect_prediction.py
lists both outcome token ids. Bare slugs are rejected rather than choosing Yes.
Case must be preserved when forwarding a slug to Gamma.

Public books and prices need no credentials. Price history has sampled prices,
not trade OHLCV: data_kind=price_samples, O=H=L=C, volume_available=false. Do not
interpret the zero compatibility volume as observed trading volume. Fidelity
is minutes between observations, not requested row count. Windows use seconds
at the API and are bounded explicitly by the connector.

## Accounts and orders

Polymarket collateral is pUSD on Polygon, accounted as PUSD. API key, API secret
and API passphrase are CLOB L2 credentials; private_key is a separate Polygon
signer. Keep the four secrets in exchange-scoped vault refs. Funder and
signature_type select EOA/proxy/Safe/deposit wallet identity; EOA funder must
equal signer, non-EOA types require explicit funder. Existing keys/wallets only.

The adapter pins polymarket-client==0.12.0 on Python >=3.11. Nerya's tested
binding separates signing from post_order, computes the signed hash before
sending, and requires already provisioned credentials and wallet identity.
The pinned low-level client constructor avoids SecureClient.create's wallet
deployment; convenience place calls and automatic allowance recovery are
never used. No install or wallet/allowance provisioning happens automatically.

Supported: owned outcome BUY/SELL; market execution uses share-sized marketable
limit order with a price bound and FOK; limit GTC, GTD, IOC/FAK and post-only.
Market BUY amount in the upstream market-order API is collateral, not shares;
the adapter uses create_order to avoid conflating units. Shares have two-digit
precision. USD sizing rounds shares down before reservation; minimum size and
price tick are checked against the current book. GTD needs an expiration at
least 180 seconds in the future (including the 60-second safety buffer).

TP/SL remains optional. Native brackets, stop triggers, leverage and opening
short positions are not implemented. Explicit requests are rejected, not
discarded. Buy the opposite outcome to express the opposite view; do not
silently convert a short request. Strategy-owned exits and conditional-token
balances constrain sells; no withdrawal or bridge is executed here. Standard
binary CTF redemption is a separate finite-authorized financial operation;
read financial_ops/references/mainstream-defi.md for its EOA/ownership limits.

## Confirmation and retries

client_order_id maps to a signed CLOB order hash persisted before posting.
Only the hash, request metadata and status are stored. On timeout, look up that
same hash; never sign/post a new order to resolve an unknown response. SDK
automatic post retries are off. Order query/cancel use authenticated SDK calls.

Order MATCHED and trade MATCHED are not settlement. Book only CONFIRMED trades
with a settlement hash, using taker_order_id or the matching maker_orders leg.
Read actual fill price; an order's limit price is not necessarily its execution
price. Cumulative fills are applied as incremental cost differences so later
fills do not distort cost basis. Cancel refusal stays live; successful cancel
is followed by a fresh order query to catch racing fills.

Configure settlement_rpc_url, settlement_exchange_code_hashes and optionally
settlement_confirmations for independent CTF Exchange v2 fee verification.
The decoder checks canonical receipts, code identity, order hash, owner, side,
token and actual amounts; fee_status=chain_verified. Missing evidence keeps
the original order pending rather than recording a guessed zero fee. Without
that optional configuration fees remain unverified, not net-performance proof.
Snapshot NAV uses the Data API v2/value data envelope, including combo value;
v2/positions uses opaque cursors with wallet/status anchors, not an offset or
first-page sum. Missing/null values never silently become zero.

Verify an adapter with isolated signing, exact auth request shape, outcome
selection, order types, partial fills, timeout recovery, cancel races, paper
separation and Agent/script/SDK entry tests. Public reads prove data access,
not live-money order acceptance. Real orders require independent authorization.
