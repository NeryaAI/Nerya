# Prediction-market adapters

The built-in prediction connector is Polymarket. Find it through connector_list
and connector_view; no Kalshi connector is currently provided. Prediction
markets use the ordinary trading plan/risk/approval/order-tracker path, not
wallet.swap. A new venue belongs in ExchangeProviderSpec + a Connector.

## Identity and data

Use POLYMARKET:<decimal outcome asset id>, or POLYMARKET:<slug>#<outcome>.
Outcome names are explicit: Yes and No are different assets. A condition id is
a group of outcomes and is not a tradable token. scripts/inspect_prediction.py
lists both outcome token ids. Bare slugs are rejected rather than choosing Yes.
Case must be preserved when forwarding a slug to Gamma.

Resolve the market version before trading: v1 selects the outcome's
clobTokenIds entry; v2 selects its positionIds entry, even when both fields
exist. Keep asset ids as decimal strings. get_market_metadata exposes the
protocol, asset, outcome and market state; missing versions and retired ids
are unavailable, not permission to select another outcome. The official SDK
selects the exchange and signing domain from the validated asset id.

For scripts/inspect_prediction.py, pass provider from connector discovery or
a venue-prefixed market. The script uses the registry, accepts its aliases,
and reuses that public connector for history. Start with bounded discovery,
then inspect the explicitly selected outcome. A bare provider default is not
an account binding.

For a compacted `list_symbols`, choose a suitable outcome from the visible rows.
For a single outcome's quote and constraints, read `stdout_json.notes` first;
a null constraint is unknown. If a required row, depth or history is omitted,
use the returned `raw://<date>/<call_id>` to locate the workspace file
`state/tool_raw/<date>/<call_id>.json` with `read_file`. For `list_symbols`,
read `payload.symbols`; for `script_run`, read `payload.stdout_json`.
This is a file artifact, not a `resource_read` URI. Custom Agent allowlists
need `read_file` to recover omitted evidence.

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

The Nerya connector accepts shares. BUY supports GTC, GTD and post-only limit
orders. Immediate BUY (market, FOK, IOC/FAK) is explicitly unsupported here:
the venue treats it as a collateral budget and price improvement can acquire
more shares than requested. Offer a share-sized limit order for the operator
to choose; preserve their original request rather than silently converting it.

SELL supports owned-share limit orders and native SDK market orders with a
minimum price and FOK or IOC/FAK. Default market SELL is FOK; explicit IOC/FAK
allows a partial fill. Balances use CONDITIONAL for CTF and CONDITIONAL-V2 for
V2 positions. USD sizing rounds shares down before reservation. Derive the
exact share step from the pinned SDK and the
current tick size; validate size/tick against the current book. Missing,
stale or future book timestamps block execution. GTD expiration must be at
least 180 seconds in the future, including the 60-second safety buffer.

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
is followed by a fresh order query to catch racing fills. Failed trades do not
keep a canceled order permanently live. Matched but unsettled amounts remain
reserved, including Nerya orders absent from the open-order listing. Cancel
submission is independent of fee-RPC availability; failed reconciliation after
a confirmed cancellation returns an explicit pending result.

SDK list methods yield pages. Flatten their items while checking cursors and
depth limits; an incomplete collection is not a complete balance or fill set.

Configure settlement_rpc_url, settlement_exchange_code_hashes and optionally
settlement_confirmations for independent CTF Exchange v2 fee verification.
The decoder checks canonical receipts, code identity, order hash, owner, side,
token and actual amounts; fee_status=chain_verified. Missing evidence keeps
the original order pending rather than recording a guessed zero fee. Without
that optional configuration fees remain unverified, not net-performance proof.
ExchangeV3 uses the same fill ABI. Its proxy also requires an approved
settlement_exchange_implementations entry keyed by proxy address, containing
the implementation address and code_hash. Verification reads the ERC-1967
slot and implementation bytecode at the receipt block; changed implementations,
unavailable historical state or an upgrade in that block remain unverified.

Snapshot NAV uses the Data API v2/value data envelope, including combo value;
v2/positions uses opaque cursors with wallet/status anchors, not an offset or
first-page sum. Missing/null values never silently become zero.

## Funds display

The portfolio and account pages show Nerya's local strategy positions, with
public instrument metadata cached by account snapshots. Display event/outcome,
shares, prices, value and settlement state; keep raw ids in details. A resolved
market is not evidence of redemption. Account NAV includes all account assets,
while strategy holdings/PnL exclude external holdings. PUSD cash uses the shared
snapshot valuation. A zero mark is valid; unavailable prices or unverified fees
are unknown. Metadata failure keeps the position visible with its id and state.

Verify an adapter with isolated signing, exact auth request shape, outcome
selection, order types, partial fills, timeout recovery, cancel races, paper
separation and Agent/script/SDK entry tests. Public reads prove data access,
not live-money order acceptance. Real orders require independent authorization.
