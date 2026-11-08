# Mainstream execution contracts

Start with financial_components in paper/backtest mode for strategy discovery.
Use financial_readiness with read:funds to inspect local execution prerequisites;
it is not an exchange handshake, approval, successful RPC simulation or verified
live trade. Loading this Skill cannot enable funds permissions, install keys
or activate a strategy.

## Jupiter discovery and historical evidence

Call `financial_components(mode="backtest", protocol="jupiter", market="SOL/USDC")`
for the requested pair. Reuse the returned `builtin:trading` ID and revision;
its `swap` action delegates to the wallet provider, whose Solana self-custody
route implements Jupiter v2. `jupiter_v2` is the readiness family, not a
standalone connector or component ID. Empty connector_list or wallet/onchainos
data_api catalogs cannot establish that this implementation is missing.

Installed swap support supplies no historical OHLC or route/fee observations.
Jupiter is a swap aggregator; its current quote describes a present route, not
an archive of candles or historical execution. A `market_data_list_symbols`
CCXT no-exchange error only identifies an unsupported symbol enumerator. It
does not negate `builtin:trading`, prove global history absence or authorize
a Binance/USDT fallback. Preserve SOL/USDC; USDC and USDT are distinct assets.

Follow the component producer's read-only `next_required_action` to the backtest
replay-capabilities reference. Keep `historical_data_verified=false` and
`financial_execution_allowed=false`; metadata discovery is not a failed replay
or a permission grant. Inspect advertised source schemas for the exact chain,
token identities, pair and requested UTC window. A current quote or Solana RPC
snapshot is not past history; pool candles alone do not establish Jupiter
route/fee/fill history. If unavailable, identify the exact missing source and
replay capability, requested versus observed coverage (unknown if not fetched),
and request that source/dataset without replacing the original scope.
`model_interpretation` and limitations do not authorize a substitute venue or
token; the admitted `requested_scope.summary` remains authoritative.
For creation plus backtest, apply strategy_author's Historical discovery stop:
retain any existing disabled candidate unchanged and end after bounded discovery
finds no matching producer. Report blocked replay with its exact missing
source/window; package writing does not repair missing history. Never replace
it with CEX candles, fabricate fills/returns, configure live credentials or
prepare a financial action to satisfy a no-trading backtest request.

## Preferred routes

Use Jupiter v2 for Solana token swaps, Uniswap v3/v4 or PancakeSwap v3 for EVM
liquidity positions, and Aave v3 for supported lending actions. Preserve an
operator-selected venue; do not switch an existing binding silently. Do not
select Byreal as the default for new strategies or expand its LP support.

Jupiter aggregates whichever routes the actual order supports. A Raydium,
Orca or Meteora route in a quote is swap evidence, not a native LP adapter.
Native Solana LP/farming, Pancake Infinity, v4 nonzero hooks, negative-risk
redemption and combo redemption are not implemented. Say unsupported; never
invent methods or relabel a paper result as a real transaction.

Jupiter /swap/v2/order needs a vault-backed API key. Nerya excludes sponsored
and RFQ multi-signer transactions. Fixed input, min-out, fees and slippage are
checked; an independently read chain receipt determines settlement. The
network-fee ceiling and refundable rent reserve are separate. Preserve the
original signature/requestId after a timeout. Do not silently fall back to v1.

## Script SDK and Agent tools

Script runs use ctx.financial.prepare(request, client_request_id=...),
execute(action_id, quote_hash=...), get(action_id), reconcile(action_id),
account_state(request), components() and readiness(). Every call retains the
current strategy producer, resource binding and finite authorization. Paper
and shadow contexts do not have a funds-execution shortcut. Pure Agent tasks
use financial_prepare/execute/get/reconcile and financial_components; the
same gates apply. Never import connectors or call raw wallet/HTTP/CLI code in
an authored strategy.

## LP actions and range changes

Protocol names: uniswap_v3, uniswap_v4, pancakeswap_v3. Actions: lp_add,
lp_remove, lp_collect. Supply wallet_id, chain, protocol and parameters.pool_id.
Use live component parameter schemas for amounts, integer ticks, liquidity,
token_id and minimum_liquidity. Position managers, token order, pool identity,
price feeds and code hashes must be operator reviewed. Do not set reviewed:true
from Agent reasoning. An existing NFT cannot change its range in-place.

Use financial_rebalance_prepare({plan,client_request_id}) with an immutable
plan containing exit, optional swap, entry, expires_at and max_total_fee_usd.
exit is a bounded lp_remove of an owned NFT; entry is a new lp_add of the same
underlying pair; swap can adjust the ratio only inside that pair and wallet.
Persist rebalance_id. financial_rebalance_advance({rebalance_id,expected_revision})
advances one step; financial_rebalance_get reads it; stop requires all child
actions resolved or explicitly discarded. SDK names are prepare_rebalance,
advance_rebalance, get_rebalance and stop_rebalance.

Never replace an uncertain step with a new workflow/key. New ticks may query
old receipts but cannot borrow an old run's approval identity. Confirmed exit
proceeds bound later spending, even when the wallet contains unrelated assets.
Do not automatically resize an already authorized entry if proceeds differ.
Prerequisite allowances require their own finite approval and refreshed quote.
Partial progress is not an atomic rollback: report the current asset inventory.

## Aave

For paper authoring/rate replay, reuse the current unfiltered `account_list`
result or call `account_list` with no venue filter. `venue:"paper"` filters the
provider, not mode, and its empty result does not establish an empty roster.
Select an active `mode:"paper"` account with live trading disabled and compatible
chain/provider, asset currency and declared protocol binding; use its actual ID
in draft and manifest `accounts`. An EVM provider can host an Aave binding;
`aave_v3` names the financial protocol, not necessarily an account provider.
An `account_upsert` unknown-provider error is an account-routing diagnostic, not
a financial execution capability gap. Read the actual roster/component evidence
and reuse a compatible account; this workflow neither creates nor reconfigures
accounts. If compatibility is unresolved, ask through `request_user_input` and
keep it unverified; a paper binding grants no wallet or live execution authority.

The strategy manifest identifies the protocol reserve as
`AAVE_V3:<chain>-<asset>`, for example `AAVE_V3:ETH-USDC` for the
Ethereum USDC reserve. Use the requested/discovered chain and reserve; the
chain-only label `EVM:USDC` omits the lending protocol. This identifier binds
the candidate scope; it does not provide an RPC, signer, history or approval.

For an actual supply strategy, implement lend_supply / lend_withdraw through
ctx.financial. Trading open_position / close_position create market trades,
not Aave deposits; keep that path for actual trading strategies. A market-order
substitution or a read-only research report cannot complete an Aave strategy.

Load Skill(skill="strategy_author", file="templates/aave_supply.py.j2") and
save it beside the candidate's main.py. This is a bounded execution helper;
the strategy owns data validation, APY entry/exit decisions, amounts and stable
operation keys. Call aave_supply_tick(ctx, request=request,
operation_key=stable_key, data_ready=data_ready, decision=decision). Missing
data, false decisions and non-live modes return hold without FinancialAPI
calls. These gates do not grant permissions. Keep no-trading candidates and
their schedules disabled; author their actual supply/withdraw branches.

The installed FinancialAPI request envelope is:

    request = {
        "kind": "lend_supply",  # lend_withdraw for a bounded, owned exit
        "component_id": "builtin:aave", "protocol": "aave_v3",
        "wallet_id": bound_wallet_id, "chain": bound_chain,
        "asset": bound_reserve_address, "amount": bounded_token_amount,
        "parameters": {},
    }

Bind those variables explicitly. Aave's parameters schema is empty and closed;
asset/amount belong at the top level. A DefiLlama pool UUID identifies a data
series, not an Aave Pool contract or wallet resource. Retain the data-source,
chain and reserve mapping separately. For live execution, the manifest's accounts must include an
operator-configured account linked to the wallet; an optional request account_id
must match that binding. The operator supplies reviewed
financial.defi.deployments[chain].aave_v3 (Pool/data-provider/oracle, code hashes,
Pool implementation and oracle sources), wallet permissions, financial account
limits and portfolio health-factor limits. Do not invent addresses, accounts,
reviewed flags or approvals. Missing bindings allow a disabled, explicitly
unbound candidate, not a claim that the strategy is executable or complete.

Read account_state(request) before preparing. Aggregate wallet supply does not
prove strategy ownership; withdrawal preparation checks the strategy's owned
claims. prepare(request, client_request_id=stable_key) returns action_id and
quote_hash; execute(action_id, quote_hash=...) enforces runtime, producer/run,
resource, risk and finite approval checks. Serialize invocations and retain the
immutable request/key plus action ID in strategy state. For the same operation,
a saved prepared action may resume execute with its returned quote_hash through
the SDK checks; never prepare a replacement. Other pending actions use get or
reconcile on the original identity and block a new operation. A later run cannot
borrow the original run's execution authority. Approval/allowance recovery and
interrupted preparation without an action ID require the original authorized
workflow; the helper cannot mint a new key or approve its own action.
In live mode, read/reconcile an already tracked action before checking whether
the newest data or entry/exit signal permits another operation. Stale data or
a false signal must not hide a pending approval/submission or claim no funds
were sent earlier. A prepared action may execute only while the current gates
remain satisfied; non-live modes keep all funds I/O disabled.

Financial receipts use state; approval/prerequisite responses use status.
Preserve the full public receipt in metadata.financial_action. Only gateway
confirmed maps to filled; approval remains pending, prerequisites remain hold,
submitted/confirming remain submitted, rejected remains rejected, and
unconfirmed/needs_recovery/unknown remain errors with the original identity.
Do not wrap these responses in ctx.result.ok. Test through real FinancialAPI
and ResultBuilder: supply, owned withdrawal, exact request normalization,
empty parameters, missing resources, no-data/false gates, prepared recovery and
uncertain receipts. Offline contract tests are not chain or performance proof.

For supply/deposit yield history, first inspect the provider schema with
`data_api(op="schema", provider="defillama", action="yield_pools")`, then
call it with `data_api(op="call", provider="defillama", action="yield_pools",
args={...})` and filter the catalog by the actual
project, chain, symbol and underlying token. Resolve the returned pool identity
through strategy_author's `references/rate-policy.md` pool-selection question
before drafting or acquiring its history; shared symbols/canonical markets do
not select a row. Retain chain/reserve identity and distinguish ordinary supply
from protection/staking products. `yield_history`
reads the selected pool over explicit UTC `start`/`end`, with at most 366 days
and 500 returned observations. It is read-only and requires no wallet.
For an isolated rate-based replay, `yield_history` supports `replay_input:true`:
declare its actual pool ID and requested UTC dates in `replay_inputs.json`,
then use the host-owned rates engine for deposit-rate policies: load
strategy_author/references/rate-policy.md. Custom research calculations may
read `dataset(id)` through backtest's custom_replay_template.md contract.
The host handles data
preparation; no wallet/financial_prepare or repeated table fetching is needed.

Read `_envelope`: deposit APY fields are annualized percentage points, not
realized return; borrow APY is excluded. Source hashes, actual timestamps, nulls,
truncation and coverage gaps remain part of the evidence. For a rate-based replay,
use the observation known at each historical timestamp and disclose any accrual
assumption, unobserved leading/trailing interval and omitted costs. Never call
that estimate actual lending fills or exact aToken balance growth. Native OHLCV
replay does not model lending; use the supported rate-policy simulation contract
or report the remaining capability gap. Preserve the operator's chain; if no
chain was specified, make the selected pool/chain assumption explicit.

For historical rates/TVL, use `data_api` with `op="schema"`,
`provider="onchainos"` and the exact `defi_rate_chart` or `defi_tvl_chart`
action before calling. Both require `investment_id`; reuse the discovered
product ID and its chain. A `platform` search term is not a product ID.
For product detail and depth/price charts the same rule applies: discover the
provider's `investmentId` with `defi_search` or `defi_list`, bind its actual
protocol, token pair, chain and fee tier, and pass that ID as a string. A token
or pool contract address is not an OnchainOS investment ID. Parameter errors
do not establish missing credentials or unavailable history; inspect the
advertised schema and product identity before making one corrected call.
Current depth rows are ticks at one observation time, not a historical tick
series; row count cannot establish the requested replay window or fee history.
Before treating a rate series as yield, inspect the product detail and bind its
direction: supply/deposit earnings versus borrow cost, plus chain, reserve asset
and rate units. A `LENDING` search result or an APY title alone does not establish
deposit yield. Borrow APY is a financing cost, not the requested USDC supply
return. If the returned product is the wrong direction, make one schema-supported
search for the correct product; otherwise report the missing supply-rate source
and keep the original request unresolved.
For `cli_read` schema errors, inspect `op="schema", action="cli_read"`;
`command` is one allowlisted string and `params` is an object. Use the named
action's schema for its parameters, rather than shell-style help commands.
After inspecting the schema, make at most one corrected call; if it fails,
report the precise unresolved fields instead of trying argument variants.

Compare `next_required_action.observed_time_extent` with the requested replay
window. `requested_time_range=MONTH` and `limit=500` do not establish 30 days:
60 samples spanning about 10 days remain insufficient for a 30-day replay.
The extent covers returned rows only; projection/truncation or missing
timestamps may leave coverage unknown, and endpoints do not prove continuity.
Inspect the schema once for a supported history parameter, make at most one
changed query, then stop if the extent stays unchanged or insufficient. Keep
the disabled candidate and record the requested window, observed range and
missing history. Preserve chain/product/protocol; never synthesize historical
snapshots from a current account state or silently replace the protocol.

Use aave_v3 and lend_supply/lend_withdraw/borrow/repay through the same gateway.
Read account_state first: collateral, debt, reserve status and health-factor
buffers are real chain facts. Variable debt is supported; E-mode, collateral
toggle, flash loans and recursive leverage are not implicit capabilities.
Stop borrowing when policy health buffers are breached. A reducing action may
still need gas and approval. Account-wide risk cannot be inferred from one token.

## Prediction settlement and fees

Polymarket orders remain ordinary prediction trades, not wallet swaps.
Data API v2 envelopes/cursors are mandatory; GTD is at least 180 seconds ahead.
For independently verified order fees configure settlement_rpc_url and
settlement_exchange_code_hashes against the reviewed CTF Exchange v2 contracts.
Nerya verifies canonical receipts, order hash, maker, token, side and exact event
amounts. Missing evidence holds the original order pending; it is not a zero-fee
fill. Without that configuration, fees remain explicitly unverified. Collateral
is PUSD; nominal collateral accounting is not a PUSD/USD exchange-rate guarantee.

redeem with protocol polymarket_ctf requires Polygon, account_id, its explicitly
linked wallet_id, parameters.condition_id and parameters.strategy_id. Only
resolved reviewed standard binary conditions and an owned EOA are implemented.
Both outcome balances must be wholly owned by that strategy; outstanding
orders block redemption. Existing ERC1155 operator approval is required; Nerya
does not issue unlimited approval automatically. The entire pair is redeemed,
so shared/proxy wallets need a different transport. Exact collateral credit,
outcome debits and gas are checked before strategy positions close, including
zero-value losing outcomes. Do not call SDK wallet-provisioning convenience APIs.

## Cost and review evidence

Wallet network costs use receipt gasUsed/effectiveGasPrice or Solana meta.fee;
OP-stack l1Fee is included when provided. Router fees already reflected in
actual output must not be subtracted again. Fee pricing records its valuation
time; a late recovery is not historical-price proof. Missing costs stay
unpriced/unverified. LP review separates principal, fee growth, realized costs,
remaining tokens and protocol exposure. No ordinary OHLCV backtest proves LP
fee APR, concentrated-liquidity returns or prediction settlement behavior.

Official contracts checked 2026-10-05:
https://developers.jup.ag/docs/api-reference/swap/order
https://developer.pancakeswap.finance/contracts/v3/pancakev3pool
https://developer.pancakeswap.finance/contracts/v3/nonfungiblepositionmanager
https://docs.polymarket.com/trading/positions/manage
https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Events.sol
