# Historical on-chain data: source selection

Official documentation reviewed 2026-11-01. Recheck the relevant page before
acquisition: access, retention and schemas change. This reference establishes
documented paths, not tested credentials, downloaded datasets or replay coverage.
Use `onchain-data-discovery.md` for the acquisition and proposal workflow.

## Autonomous source choice

Select the missing observation, not just a provider name. Choose compatible
installed read-only sources within the admitted scope and budget without asking
the user to find a provider. Retrieve a bounded sample, compare chain, pool/program,
quote units and timestamps, then paginate the admitted window through its producer.
A documented endpoint or matching symbol is a discovery lead, not replay input.

Distinguish a source being available, a sample retrieved, a window acquired, and
producer-admitted replay input. If integration is missing, prepare the smallest
read-only adapter proposal with endpoint, schema, identity, pagination, access/cost
and normalization. Ask only for credentials, paid access or a material scope choice;
keep completed reads and the existing candidate rather than restarting discovery.

## Choose the observation first

| Needed observation | Starting route | Evidence boundary |
| --- | --- | --- |
| A specific pool's historical price signal | CoinGecko pool OHLCV or Birdeye pair OHLCV | Observed candles, not Jupiter routes/fills or LP fees |
| Actual Solana/Jupiter swaps | Archival transactions and decoded instructions/events | Executed transactions, not counterfactual quotes for every historical time |
| Uniswap V3 pool/position history | Official subgraph deployment or block-pinned archive RPC and events | Indexed/sampled state has different coverage from full event replay |
| Lending-rate history | Matching installed historical-rate producer | APY simulation is not reserve-index accounting or realized yield |

An aggregator is not one canonical liquidity pool. Resolve the underlying pool
only when the task permits pool-specific price research. Preserve an explicitly
pinned Jupiter source; changing it to a Raydium pool or token/USD series changes
the evidence, even if the token symbols match.

## Pool and pair OHLCV

### CoinGecko / GeckoTerminal on-chain data

The installed `geckoterminal.pool_discover` / `pool_ohlcv` path supports bounded
Solana pool-price simulation. Inspect its current schema for the supported
resolution, window and request limit. Its input preparation binds exact mints
to the [Jupiter official token information](https://developers.jup.ag/docs/tokens/token-information)
catalog, then records pool/token orientation and observed coverage. This is
current identity plus historical prices, not historical aggregator execution.

Read the [official pool OHLCV schema](https://docs.coingecko.com/reference/pool-ohlcv-contract-address.md)
and the appropriate [Demo/keyless documentation](https://docs.coingecko.com/demo/reference/pool-ohlcv-contract-address.md).
The documented path is
`GET /onchain/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}`;
select the documented server/auth for the actual product, rather than moving
a Pro request to an assumed free URL.

- Resolve network, pool address and base/quote token contracts from metadata.
- `currency` defaults to USD; request `token` currency when quote-token units
  are required, and set the `token` orientation explicitly.
- `before_timestamp` paginates older data; `limit` is at most 1000. Match
  `timeframe` and `aggregate` to the required interval.
  The reviewed endpoint limits each call to a six-month range; older windows
  require pagination rather than assuming one response contains all history.
- Empty intervals are omitted by default; `include_empty_intervals=true`
  carries the previous close with zero volume. Record padding separately.
- Historical access depends on plan and pool tracking start. Check actual
  returned coverage; the maximum documented range is not a completeness receipt.

### Birdeye

Read [OHLCV V3 — Pair](https://data.birdeye.so/docs/data-api/price-ohlcv/get-defi-v3-ohlcv-pair.md).
The documented endpoint is `GET https://public-api.birdeye.so/defi/v3/ohlcv/pair`,
with `X-API-KEY`, `x-chain`, pair address and interval `type`.
Use the actual schema for units and time bounds. Token OHLCV is a different
endpoint and is not evidence for one venue's pair.

The reviewed schema supports bounded range/count modes, up to 5000 records;
count mode takes either `time_from` or `time_to`, not both. Empty candles are
omitted unless `padding=true`. Fine intervals have shorter retention (the
reviewed page specifies two weeks for 1s and three months for 15s/30s). Verify
the selected chain, plan, credits and interval before scheduling a download.

## Solana / Jupiter transaction history

[Jupiter Price V3](https://developers.jup.ag/docs/price/index.md) returns current
USD token prices with block identity, not a requested historical pair series.
The reviewed API does not establish historical routing coverage. A current
quote or a live stream cannot backfill historical quotes or prove old fills.

[Solana RPC](https://solana.com/docs/rpc/http) exposes
`getSignaturesForAddress` plus `getTransaction`. Check endpoint retention,
signature pagination, supported transaction versions, failed transactions and
token-account coverage before choosing a raw-RPC backfill. RPC method existence
does not guarantee one provider retains the requested interval.

Two documented indexed paths are:

- [Helius getTransactionsForAddress](https://www.helius.dev/docs/rpc/gettransactionsforaddress.md):
  provider-specific RPC, with time/slot/status filters, pagination, full
  transactions and optional associated-token-account activity. This is not a
  standard method accepted by every Solana RPC. It needs a project API key and
  returns at most 1000 full transactions per call; retain its pagination cursor.
- [Helius Parsed Events](https://www.helius.dev/docs/parsed-events.md): REST
  transaction parsing, address history and scans by program or mint, including
  decoded CPI instructions. The reviewed docs recommend it over legacy
  Enhanced Transactions for new integrations. Read current auth/credit terms.

For a Jupiter study, verify program identity, route/CPI instructions, input and
output mints, raw amounts/decimals, fees, transaction success, slot/time and
signatures. Deduplicate multi-leg routes rather than treating each leg as a
new user trade. Wallet history covers that wallet, not all swaps of a pair;
program scans require their own completeness and budget evidence. Historical
transactions also do not imply arbitrary historical Solana account snapshots.

## Uniswap V3: indexed events or archive state

[Official V3 subgraph queries](https://developers.uniswap.org/docs/ecosystem/subgraphs/concepts/v3/queries.md)
describe pools, swaps, positions, daily/hourly series and historical block
queries. Obtain the current deployment/query URL via
[The Graph's query workflow](https://thegraph.com/docs/en/subgraphs/querying/introduction/),
which requires an API key. Verify chain, deployment/schema, indexed head and
historical availability. Paginate against a stable block/window so head changes
do not produce duplicate or omitted events. Daily USD aggregates alone cannot
reconstruct a position's accrued fees.

[Ethereum archive state](https://ethereum.org/en/developers/docs/nodes-and-clients/archive-nodes/)
provides historical contract state. Use a supported read adapter for
block-pinned `eth_call` and event/log reads; historical log access and historical
state access are distinct capabilities. The
[V3 pool interface](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolState.sol)
defines `slot0`, liquidity, global fee growth, tick fee-growth-outside and
position accounting. Required observations depend on whether the engine models
sampled LP allocation or reconstructs event-level position fees; label the
former as sampled simulation, not exact fee execution.

For full LP history, first use an authorized bounded log-density/range probe
on the same pool and requested window. Record each actual inclusive block
span, row count, response bytes and structured error separately from the
provider's error text; conflicting text does not establish a supported range
limit. Treat density-based window size estimates as estimates, not acquired
coverage. If projected events exceed the existing replay row/byte contract,
propose bounded contiguous block pagination, content-addressed chunk storage
and a hashed range manifest with resumable checkpoints and explicit gaps
before expanding acquisition; preserve the declared sample dataset separately.
Full-coverage admission also requires canonical ordering/deduplication, boundary
and checkpoint reconciliation, and validated initial-state and tick/fee-state
reconstruction. Complete log retrieval alone does not prove exact fee accounting,
counterfactual fills, realized yield or full historical performance.

For an initial event/state feasibility check, discover `uniswap.pool_event_slice`
with `data_api` and read its schema. Supply the exact pool, explicit supported
RPC provider and a contiguous inclusive block range (at most 100 blocks) inside
the original window. Start with a small slice; inspect the range receipt and
checkpoint comparison before expanding acquisition. This action retains all
pool event types, including non-Swap events, with block hashes and source hashes.
Its sequential Initialize/Swap/Mint/Burn fold checks core price, tick and active
liquidity against the terminal state, not initialized-tick or fee accounting.
Unknown events leave this core reconstruction unverified. `replay_input` is false and full-window coverage
remains incomplete even if the slice checkpoint matches. Preserve the returned
limitations and actual range; do not put this discovery result into a candidate's
`replay_inputs.json` or use it to clear `data_coverage`.
For explicitly authorized acquisition beyond one slice, load the backtest
Skill's `references/history-data.md` event-archive branch. Its
`historical_data(dataset_kind="uniswap_pool_events")` download lane stores
bounded chunks and resumes missing ranges; offline inspection is separate.
An archive's event-range receipt still does not admit full LP replay.
For a saved fixed LP range, supply its paired tick bounds to the event slice
or archive producer to obtain boundary state and initialization-continuity
evidence. Positive `initialized` values at both endpoints alone are insufficient:
a Burn may clear a boundary and a later Mint may reinitialize its fee counters.
Full fixed-range accounting additionally needs historical state at every actual
strategy decision and accounting boundary. Bind that cadence to the saved
schedule; a chart timeframe does not establish the decision schedule.

Current Nerya `uniswap.pool_history` has a fixed public-RPC surface, not a
custom-RPC/key configuration contract. A provider denial is not repaired merely
by asking for an archive key. First identify a compatible source and propose
the missing supported read/auth integration. Preserve the pool, fee tier, asset
mapping and window. Do not replace LP history with CEX candles.

## Indexed EVM event history: SQD Portal

For missing historical logs, review the [Portal EVM contract](https://docs.sqd.dev/en/portal/evm/api.md)
and [access policy](https://sqd.dev/developers/). Public shared capacity supports
bounded evaluation without a key; authenticated/dedicated access is a separate
credential/cost decision. Discover datasets and inspect the selected dataset's
`start_block` before choosing a historical range.
When adopted by the active runtime, `data_api` provider `sqd` exposes `timestamp_block` and
`evm_log_slice` for Ethereum 1. Inspect their schemas: the event action uses
one exact address, optional `topic0`/`topic1`, at most 100 inclusive blocks and
a bounded row limit. Its `coverage` distinguishes scanned blocks from returned
events and gives a next block; a row-cap continuation can repeat a block, so
deduplicate by block hash/transaction/log index. Keep `replay_input:false`.

- Base: `https://portal.sqd.dev/datasets/{dataset}`; Ethereum's documented name
  is `ethereum-mainnet`. `GET /metadata` reports availability, not acquired history.
- `GET /timestamps/{unix_seconds}/block` resolves the **first block at or after**
  the instant. This is not the floor-block contract used by existing LP state
  preparation; verify adjacent block timestamps before handing off boundaries.
- `POST /finalized-stream` accepts `type:evm`, inclusive `fromBlock`/`toBlock`,
  field selection and log filters. Keep exact contract/address/topic filters,
  block number/hash/time, transaction identity, log index, topics and raw data.
  Responses are NDJSON batches, not a whole-range receipt. Continue from the
  last returned block + 1 with unchanged target bounds/filters; record progress
  and gaps. Empty filtered results alone do not certify range completeness.
- Branch on structured `error.type`/`error.code`, not error prose. Capacity
  failures (`rate_limit_error`, `overloaded`, including HTTP 529) carry
  `Retry-After` in seconds; preserve it and `x-request-id`. Resume the same
  bounded request only within its remaining budget, rather than rotating hosts.

This is indexed event discovery, not archive `eth_call` or an installed
Nerya replay input. Aave rate/index events require the existing normalization,
boundary and coverage admission contract; Uniswap logs require initial state,
tick/fee reconstruction and checkpoint reconciliation. Neither replaces the
candidate's pinned source or clears coverage by itself.

Observed 2026-11-07 local evaluation: Ethereum metadata returned `start_block:0`;
timestamp lookup for 2026-10-31T12:00Z returned HTTP 529, structured
`rate_limit_error/overloaded`, `Retry-After:10`. A later bounded retry succeeded
and a 100-block finalized Aave Pool sample contained raw reserve-update events;
the initial stream capacity refusal likewise recovered on a budgeted retry.
This establishes a retrieved event sample, not full-window acquisition, reserve
state reconstruction or replay admission. Capacity unavailable is distinct from
history absent. Preserve a retryable source's delay and request identity; stop
when its bounded budget is exhausted rather than turning it into an auth denial.
The later native Python transport probe returned a structured Cloudflare
HTTP 403 / `1010`, `browser_signature_banned`, `owner_action_required:true`,
`retryable:false`. This is a site-owner client-signature refusal, not an API-key
requirement, malformed query or unavailable historical dataset. Preserve its
safe error code and Ray ID for provider support. The exact signature factor
(User-Agent, TLS or network path) was not isolated; do not claim a specific
factor from the error name alone. A successful earlier client does not establish
native access. Provider-approved access or a supported integration is required;
changing client identity or network path is not a repair for this refusal.

Diagnose a transport discrepancy before concluding discovery is exhausted:
capture bounded status, response content type and structured error fields;
inspect effective local proxy configuration without printing credentials.
Python on macOS can inherit system proxies even when proxy environment
variables are absent. Distinguish local configuration errors from provider
refusals, and retain successful samples. An unchanged denied operation stops,
not independent local analysis, report repair or approved source discovery.

## Lending-rate series

### Independent explorer event discovery

[Blockscout logs](https://docs.blockscout.com/devs/apis/rpc/logs) documents
`/api?module=logs&action=getLogs` with inclusive `fromBlock`/`toBlock`, exact
contract and topic filters (`topic0_1_opr=and`), capped at 1,000 logs. Split
ranges before treating a capped page as exhaustive. Its
[block module](https://docs.blockscout.com/devs/apis/rpc/block) documents
`getblocknobytime` with `closest=before|after`; returned block numbers still
need canonical header/time checks. Logs are not historical reserve state.

An independent Ethereum explorer sample on 2026-11-07 resolved
2026-10-30T12:00Z to floor block 26305432. A native Python `getLogs` sample
for the same Aave Pool/USDC event over blocks 26305432..26305531 returned
HTTP 403; no logs were accepted, no retry or alternate transport followed.
The successful timestamp lookup does not establish log access. This is a
documented integration lead, not an installed producer or acquired replay input.

For official Aave average-rate research, discover `aave.supply_apy_history`
with `data_api` and inspect its schema. It cross-checks an existing DefiLlama
UUID's aToken against official market/reserve metadata, preserving the UUID and
both sources' provenance. Supply verified addresses and the exact requested
window; never infer a market from symbols or suffixes. The action is read-only
research, not admitted replay input: average availability remains unverified.

Discover `defillama.yield_history` through `data_api` and read its installed
schema. See [DefiLlama's yield API](https://api-docs.defillama.com/) for the
current provider documentation. Match the exact pool, chain, asset and requested
window. `yield_pools` filters use exact, case-sensitive provider values. When a
guessed project name returns no rows, discover with established chain and asset
filters, then select the returned project and pool UUID rather than cycling
through guessed protocol spellings. Inspect underlying tokens and pool metadata;
keep distinct markets distinct and record the selected pool.
Historical APY can support an explicitly labeled rate-policy simulation;
reserve-index accrual requires separate protocol/index observations. Keep gaps
and realized-versus-modeled yield explicit.

For Aave reserve history, start with the official
[protocol-subgraphs documentation](https://github.com/aave/protocol-subgraphs#historical-queries)
and select the deployment for the exact chain and market. It documents
`reserves(block: {number: ...})`; resolve UTC timestamps to historical blocks
through an installed producer. Its `liquidityRate` and borrow-rate fields are
APR in RAY units (`10^27`), not percentage APY. Inspect the selected schema for
the liquidity/debt indexes, reserve identity, decimals and block metadata needed
by the accounting model. Rate fields alone do not establish index accrual. A
documented deployment still needs authorized access and producer replay admission;
use the existing APY-simulation lane only when that is the requested observation.

### Aave V3 Ethereum native USDC: additional source handoff

For actual block-pinned reserve rates/indexes, use the installed
`aave.reserve_state_history` action and the workflow in
`references/aave-reserve-history.md`. The averaged APY source below is a separate
observation type; reserve sampling does not establish its replay compatibility.

Reviewed 2026-11-06 using two Exa search questions and primary docs/source.
RPC/subgraph findings below are docs only. A bounded public GraphQL sample was
also retrieved on 2026-11-06: ordinary Ethereum V3 USDC `supplyAPYHistory` returned
181 approximately four-hour observations for `LAST_MONTH`; this rolling window
excluded the saved opening boundary. `LAST_SIX_MONTHS` returned 180 daily
observations, including 31 UTC-midnight rows spanning the saved 30-day window
with 86,400-second intervals. This is a source sample, not a producer-admitted
dataset, index-accrual receipt or completed replay. RPC/subgraph exact-window
coverage remains unverified. A missing producer is **integration missing**, not proof that
history is unavailable. For an internal interval gap, acquire compatible new
observations there; preserve the selected pool/window and saved gap threshold.

- **Identity:** the [Ethereum address book](https://github.com/bgd-labs/aave-address-book/blob/main/src/AaveV3Ethereum.sol)
  lists ordinary V3 Pool `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2`,
  native USDC `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` (6 decimals),
  aToken `0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c` on chain 1.
  Bind these to the saved DefiLlama pool UUID; verify historical proxy/ABI identity.
- **Subgraph access/schema:** the Aave deployment list identifies ETH Mainnet V3
  as `Cd2gEDVeqnjBn1hSeqFMitw8Q1iiyV9FYUZkLNRcL87g`. The
  [V3 schema](https://github.com/aave/protocol-subgraphs/blob/main/schemas/v3.schema.graphql)
  has `ReserveParamsHistoryItem` with reserve, timestamp, liquidityRate and
  liquidityIndex, but no canonical block/transaction/log ordering fields.
  Enrich provenance from chain evidence. [The Graph gateway requires an API key](https://thegraph.com/docs/en/subgraphs/querying/introduction/)
  even on its free plan. Use fixed-block, reserve/window-filtered `first` pages
  with `orderBy: id`/`id_gt`; [cursor pagination](https://thegraph.com/docs/en/subgraphs/querying/graphql-api/)
  avoids large offsets and equal-timestamp omissions. Verify actual page limits,
  deployment/indexed head/errors, indexing start and [pruning](https://thegraph.com/docs/en/subgraphs/best-practices/pruning/);
  time-travel support alone does not establish retained history.
- **Public RPC capability versus access:** [PublicNode](https://ethereum.publicnode.com/)
  still advertises archive data at `https://ethereum-rpc.publicnode.com`
  (page rechecked 2026-11-07), without exact retention, rate or log-range guarantees.
  The installed `uniswap.pool_history` producer records that anonymous historical
  state access requires a personal token. Treat the public claim as discovery,
  not proof of anonymous archive access; inspect the actual access prerequisite
  before acquiring a multi-week window. Header/log availability does not certify
  historical `eth_call` state. [Aave Pool reads](https://aave.com/docs/aave-v3/smart-contracts/pool)
  provide `getReserveData(USDC)` (stored liquidityIndex/currentLiquidityRate/
  lastUpdateTimestamp) and `getReserveNormalizedIncome(USDC)`. The
  [event ABI](https://github.com/aave-dao/aave-v3-origin/blob/main/src/contracts/interfaces/IPool.sol)
  is `ReserveDataUpdated(address indexed reserve,uint256 liquidityRate,uint256 stableBorrowRate,uint256 variableBorrowRate,uint256 liquidityIndex,uint256 variableBorrowIndex)`.
  [JSON-RPC](https://ethereum.org/en/developers/docs/apis/json-rpc/) handoff:
  block-pinned `eth_call` plus `eth_getLogs` filtered by Pool proxy, event
  signature and padded USDC topic; paginate contiguous inclusive block spans,
  advance end + 1, shrink on provider limits. Historical logs and state require
  separate access checks; event cadence is not guaranteed.
- **Official APY alternative:** the [public V3 API](https://aave.com/docs/llms.txt)
  `https://api.v3.aave.com/graphql` documents [supplyAPYHistory](https://aave.com/docs/aave-v3/markets/data#apy-history)
  keyed by chainId/market/underlyingToken and relative day/week/month/six-month/year
  windows. Results are `date` + `avgRate {raw decimals value formatted}`
  (fractional value versus percentage formatted), not reserve indexes/events.
  Live `APYSample` schema descriptions only say "The avg rate" and "The date";
  bucket boundaries and the time each average became available are unverified.
  Do not feed an average into a policy decision at its labeled timestamp without
  an evidenced availability-time contract: this can introduce lookahead. The
  reviewed section establishes no absolute-window cursor, block provenance
  or retention guarantee. A rolling window is not a repair receipt for a saved gap.
  Public-source inspection (2026-11-07): the [SDK schema at 801260c](https://github.com/aave/aave-sdk/blob/801260c39921177c56f62d1f1e0d51971e9fd4bc/packages/graphql/schema.graphql)
  documents only those two fields; [the client action](https://github.com/aave/aave-sdk/blob/801260c39921177c56f62d1f1e0d51971e9fd4bc/packages/client/src/actions/reserve.ts)
  forwards the query without aggregation logic. The [official chart at 0c33111](https://github.com/aave/interface/blob/0c3311107e0bc458cbcd77b54e224b2f72a10569/src/modules/reserve-overview/graphs/ApyGraphContainer.tsx)
  maps `new Date(item.date).getTime()` and `Number(item.avgRate.value)`, then sorts by date.
  This establishes chart interpretation, not bucket boundaries or publication time.
  To resolve those fields, seek a documented backend aggregation/availability
  contract; otherwise pursue block-pinned reserve history rather than repeatedly
  fetching the same SDK schema or treating chart timestamps as availability.

### Public RPC access observations (2026-11-07)

The [Tenderly Node RPC overview](https://docs.tenderly.co/node-rpc/overview)
documents the account-free public Ethereum gateway
`https://mainnet.gateway.tenderly.co`. A native `UrllibHttp` read returned
chain ID 1, block 26305432/hash
`0xfd7e80cf4f86f01ecb18454a0704465ce9e4bf4131d77a73a14f34d0380e4ef2`,
timestamp `0x6ae486bf`, and Aave Pool
`getReserveNormalizedIncome(USDC)` result
`0x000000000000000000000000000000000000000003d9cc9127aef0213f733b1d`.
This is acquired archive-state evidence at one block, not window coverage.
The documented keyless limits are 1 GB of responses per IP per day and 3,000
results per `eth_getLogs` call. Respect returned quota errors; plan log ranges
by the provider's actual response limits rather than an invented task-tool cap.
For Uniswap sampling, select the installed `pool_history` provider
`rpc_provider="tenderly"` explicitly. Preserve the requested pool/window and
record the new source; a denial at another provider does not prohibit this
independent public read. A user-pinned data provider remains a scope constraint.
For Aave, use the index only through a producer that preserves block provenance
and its accrual semantics; it cannot fill an average-APY series as an untyped row.

These are native Python read-only probes, not admitted historical datasets.
The [1RPC network directory](https://docs.1rpc.io/using-the-web3-api/networks)
lists public Ethereum endpoint `https://public.1rpc.io/eth`. It returned chain
ID 1 and block 26305432 with hash
`0xfd7e80cf4f86f01ecb18454a0704465ce9e4bf4131d77a73a14f34d0380e4ef2`.
At that block, Aave USDC normalized-income `eth_call` returned historical
state unavailable. A 100-block log query returned a 50-block maximum; a
50-block request returned HTTP 429, and a later 25-block request returned
HTTP 403 / RPC -32602, requiring a personal token at PublicNode. The relay
can use different upstream providers; one successful header does not establish
consistent archive-state or log access. Resolve that access prerequisite before
further requests, rather than rotating relay routes.

The [Flashbots public endpoint](https://docs.flashbots.net/flashbots-protect/quick-start)
`https://rpc.flashbots.net/fast` returned HTTP 403 / RPC -32601 for the same
read: method not whitelisted. It is a transaction-protection service, not a
verified archive-read source. No transaction was submitted.

[rpcfree Ethereum](https://rpcfree.com/ethereum-rpc/) advertises full archive
access but also says its RPC is paused. Native POSTs to its advertised endpoint
returned HTTP 200 with an HTML page, not a JSON-RPC envelope. Treat it as
unavailable for these reads; do not interpret the status code as data coverage.
No historical values or replay inputs were acquired from these probes.

**Integration contract:** resolve saved UTC times to canonical blocks at or before
those times. Retain block number/hash/timestamp, ABI/source revision, exact request,
retrieval time, raw response hash and explicit acquired ranges/gaps; retain raw
RAY integers as decimal strings. [Reserve logic](https://github.com/aave-dao/aave-v3-origin/blob/main/src/contracts/protocol/libraries/logic/ReserveLogic.sol)
accrues linear interest since lastUpdateTimestamp onto the stored index; use
normalized income at the pinned block or reproduce that version's integer rounding.
Reconcile ordered/deduplicated events with boundary state. Keep an index-accrual
lane distinct from average-APY simulation: transforming indexes/events to APY
requires an explicit producer conversion and coverage contract, not inserting
untyped rows into the existing series. Recompute the saved gap check on admitted
observations. Index ratios model accrual for a fixed scaled balance; they do not
prove strategy fills, deposits/withdrawals, costs, realized wallet yield or replay
performance. Preserve the existing partial result until producer admission passes.

An observed archive access denial is not a history-absence finding. A bounded
dRPC probe on 2026-11-06 returned HTTP 403 / Cloudflare 1010 on `eth_chainId`,
before any historical header or reserve read. No retry or alternative origin was
used; archive capability remained unverified. Preserve such a receipt and its
exact source/target. Continue only after a supported access prerequisite changes
or a separately reviewed source is admitted; average APY cannot replace denied
block-pinned index evidence.
