# Authoring executable mainstream market strategies

Load financial_ops and its mainstream-defi reference, then discover components
in paper/backtest mode before choosing an execution family. financial_readiness
inspects live configuration separately; it is not a prerequisite for authoring.
Preserve the requested chain, venue and execution family. Select an existing
compatible component or stage a verified missing integration through its
proposal lane. Supported examples include CCXT spot/linear, Jupiter swaps, Uniswap/Pancake v3 LP,
restricted Uniswap v4 LP and Aave v3; these are not defaults that override a
chain request. Do not choose Byreal by default or invent
native Raydium/Orca LP support merely because Jupiter can route a swap there.

Build main.py:run using the established script, script-gated Agent or pure
Agent contract. Keep execution and review schedules separate and disabled
until independently approved. Implementation is not activation. Do not edit
Agent Loop to add market-specific branches or give the model direct signers.

## DEX price simulation with an admitted pool source

Use this branch only when the current admitted request explicitly delegates
same-asset pool-price selection for simulation. Choose a compatible source
within that delegation without asking again. A failure, model interpretation
or this reference grants no proxy permission; otherwise follow exact-source
recovery below. Keep execution target, observed data identity and simulation
model separate, preserving the requested chain, pair, pinned window, risk limits,
model-decision policy and no-trading restrictions.

Before drafting a DEX strategy, reuse the current `account_list` result or call
it once; select an active paper account compatible with the requested chain/DEX
execution family and pass its returned ID explicitly in `accounts`. A
protocol-qualified market prefix alone does not select that account. If
compatibility is unverified, retain the producer diagnostic rather than guessing
an ID or switching to live.

1. Read pool metadata first: network, pool address, source DEX and both token
   contracts/mints. Establish base/quote identity and any native/wrapped mapping
   from the admitted scope and producer evidence, not symbols alone.
2. Inspect the OHLCV schema and returned units. Bind the exact selected token,
   quote token and price direction before acquisition. Token/USD candles are
   not SOL/USDC; a token Kline without pool/quote metadata remains unidentified.
3. Acquire the producer-pinned UTC window and resolution; inspect actual bounds,
   gaps and truncation. Reuse producer-returned dataset/manifest references.
   Metadata or row count alone does not establish coverage.
   For a relative window that has not yet been pinned, anchor its end to the
   latest closed UTC candle boundary and subtract the requested duration once
   (1h uses whole hours; 1d uses midnight UTC). Retain the configured warmup;
   requested bars plus warmup must fit the provider limit. Preserve an already
   pinned or explicit window: if it cannot be represented, surface that choice
   through `request_user_input`. On a schema error, inspect the field diagnostic
   and correct the arguments once; a failed read requires no execution approval.

For the GeckoTerminal pool-price path, use provider `geckoterminal` action
`pool_ohlcv` with its actual schema arguments. Save candidate `replay_inputs.json`
as a declaration, using verified pool/mints and the producer-pinned window:

```json
{"version":1,"datasets":[{"id":"pool_prices","provider":"geckoterminal","action":"pool_ohlcv","args":{"network":"solana","pool_address":"<verified pool address>","base_token":"<verified base mint>","quote_token":"<verified quote mint>","start":"<pinned start UTC>","end":"<pinned end UTC>","timeframe":"<saved supported timeframe>","warmup_bars":0}}]}
```

Replace placeholders with actual values and replace `0` with the saved backtest's
integer `warmup_bars` (including zero when actually configured); timeframe must
match the saved replay. Set `params.price_simulation: {dataset_id: pool_prices}`
in the same candidate; this mapping contains only `dataset_id`. Put explanatory
notes in `strategy.md`. The host executes the producer during replay and persists
the dataset bytes and receipts; declare inputs rather than hand-copying rows,
provenance or paths to pre-created prepared files. Use existing SDK replay with
`engine:"native"`; read the advertised schema instead of guessing prepared
structures from implementation files. Missing support remains an integration gap.
Keep the Jupiter candidate identity and the pool's actual provenance; report
observed pool prices plus modeled fills/cost assumptions, not Jupiter historical
routes, fees or fills. If Jupiter native `historical_data` is already unsupported,
reuse that diagnostic and choose the admitted pool path directly.
The provider reports volume in upstream USD units, not base-token volume. Use
price-only rules on this path; do not add volume sizing, volume filters or
liquidity/market-impact claims from that field. Preserve its units in provenance.

Follow backtest `references/replay-capabilities.md` for replay admission and
completion. Missing identity, coverage or admission stays blocked/partial;
proxy authorization never waives the trusted delivery floor.

## Exact-source recovery for Jupiter SOL/USDC

For “帮我做一个 Jupiter SOL/USDC 趋势策略，回测最近30天，不启用交易。”
retain Jupiter, Solana, SOL/USDC, the relative 30-day window and no trading.
`requested_scope.summary` represents the admitted user request. A separate
`model_interpretation` cannot amend it; `limitations_are_authorization=false`.
Never insert a proxy into the summary to make a different replay count as
requested work. USDC is not USDT: `BINANCE:SOLUSDT`, Binance SOL/USDC and
another DEX pool are not interchangeable evidence for Jupiter SOL/USDC.
A proxy needs explicit authorization in the current admitted request before
fetching or replaying it; use the pool-source branch above when already admitted.
A limitation or technical failure does not supply that authorization.
When that choice is needed, call `request_user_input` with a question such as
"是否使用同资产池价做模拟？这不代表 Jupiter 历史路由或成交。" and
`options: ["允许同资产池价模拟", "保留原范围，报告数据缺口"]`.
Wait for the answer in this task and continue from it. An unanswered question
in final prose provides no clickable continuation; reuse a recorded answer
instead of asking again.

Discover execution metadata through the actual producer contract:

```json
{"tool":"financial_components","arguments":{"mode":"backtest","protocol":"jupiter","market":"SOL/USDC","kind":"swap"}}
```

Reuse the returned `builtin:trading` ID/revision for swap support. An empty
connector_list or wallet/onchainos data_api catalog does not negate it. Follow
the producer's `next_required_action` to the backtest replay-capabilities
reference; `historical_data_verified=false` means history is unverified, not
that swap support failed. `financial_execution_allowed=false` stays unchanged.

Keep three identities separate. The installed request_scope resource parser
accepts this candidate manifest identity for Jupiter SOL/USDC:

```json
{"markets":["JUPITER:SOL/USDC"]}
```

`builtin:trading` identifies the execution component, not a market or dataset.
`ONCHAIN:solana:<mint>` is a token data locator, not the candidate pair identity.
Token/USD candles do not establish SOL/USDC or a historical Jupiter aggregator route.
USDC≈USD never authorizes quote substitution or an exact-history claim. Preserve
the source's actual token/mint, quote and provenance instead of relabelling rows.

For `request_scope_unverified` / `candidate_market_unresolved`, repair the
indicated markets field in the same candidate request with `JUPITER:SOL/USDC`;
retain an existing proposal ID and the admitted scope/window. Retry only after
correction. Use this reference directly; skip workspace-wide searches and
CCXT symbol enumeration for Jupiter or ONCHAIN. A `ccxt has no exchange named`
error diagnoses that enumerator only, not missing swap support or all history.
Jupiter is a swap aggregator: a current route/quote describes a present
executable swap, not historical OHLC, archived routes, fees or fills.

Use advertised read-only source discovery and inspect the returned schema
before a bounded exact-source query. For a symbol-only Solana pair, start with
`data_api(op="schema", provider="geckoterminal", action="pair_discover")`,
then call it with the admitted pair symbols. Use the returned exact mints and
pool address for `pool_discover`/`pool_ohlcv`; never invent a pool address.
Verify chain, token/mint identities,
quote asset, source/pool identity, timestamps, interval, continuity and the
requested versus observed UTC range. Anchor the relative 30 days once; do not
move or shorten the window to match available rows. Pool candles alone do not
prove historical Jupiter routing or execution costs. Do not invent a Jupiter
history API, adapter, ticker alias or successful coverage receipt.
Compare observed `first_ts` / `last_ts` to the producer-pinned window in actual
UTC using typed producers; inspect interval continuity and missing bars. A row count
alone never proves full coverage: stale caches and warmup rows cannot satisfy
the requested window. Keep identity and coverage unverified until producer
evidence establishes both; a parsed candidate market establishes neither.

If no installed source advertises those inputs, load
`Skill(skill="research", file="references/onchain-data-discovery.md")`
and actively search official protocol, explorer and indexer data sources. Inspect
documented fields and bounded read-only observations before deciding what is
missing. Report discovered source URLs and the exact remaining adapter, access
or dataset gap, retaining Jupiter SOL/USDC and the requested window. Ask the user
only for required access or a material choice, not to do this search. If discovery is bounded
or incomplete, say so; do not claim global unavailability. An unsupported
enumerator alone is not a sufficient historical-source diagnosis.

### Historical discovery stop

Apply strategy_author's Historical discovery stop here: when bounded discovery
including primary-source search still finds no usable historical producer,
preserve an existing proposal unchanged,
mark replay blocked and end with the exact missing inputs and requested window.
The exact-source request remains a disabled candidate until a matching producer
becomes available.
Package implementation, tests and a review plan do not repair this capability
gap. Continue authoring only for an explicit draft-only request or after a
matching producer becomes available. Keep execution and review schedules off;
a candidate is not a completed backtest.

## Required strategy package

Record account/wallet, canonical instrument or pool, percentage-based budget,
fees and gas reserve, entry rules, owned-position exit rules, data freshness,
cooldown, maximum holding time, recovery IDs and an independent review plan.
Use reviewed parameters and actual NAV/free balance; never hardcode 100U or
interpret 0.90 as 0.90%. No historical source means unverified performance,
not permission to fabricate a profitable backtest.

templates/risk_rules.py.j2 contains pure deterministic exit and LP-adjustment
gates; copy selected functions into the strategy. They do not place orders or
call a model. Feed timestamped SDK observations, not unverified model numbers.
Meme entry additionally needs current token-security and liquidity evidence,
an executable route and an approved token set. Unknown mint/freeze/transfer
restrictions, unavailable sellability evidence or stale pool data are blockers,
not an invitation to increase slippage. Existing market whitelist/finite-grant
checks remain authoritative; the Agent cannot add a discovered token to its
own authorization. A screening result is not a guarantee against rugs.

For wallet and prediction positions schedule deterministic exits. Their
connectors do not implement exchange-native brackets; do not promise an
always-on stop when the runtime is offline. Price/data errors trigger recovery,
not no-signal success. Use ctx.trading.close_position only for strategy-owned
exposure and keep outstanding action IDs until reconciled.

## LP work

For the minimal request “帮我创建一个 Uniswap V3 ETH/USDC LP 策略，回测最近30天，不启用交易。”,
first call financial_components with mode:backtest, protocol:uniswap_v3,
market:ETH/USDC and kind:lp_add. This read-only result discovers component
actions and schemas; it does not discover a pool or verify 30-day history.
For Ethereum chain 1, inspect data_api(op:schema, provider:uniswap,
action:pool_discover), then call with exact base/quote catalog symbols or token
addresses. This verifies current pool identity through the official catalog
and factory at a pinned block, not historical coverage. It may return multiple fee tiers;
retain those alternatives and make the pool/fee choice explicit, never silently
select a different chain or pair. Native ETH has no ERC20 catalog identity:
only use WETH after an explicit task amendment. A missing or ambiguous symbol
is a discovery gap, not evidence that all pools or history are unavailable.
For other chains, this source does not establish capability. The installed
uniswap.pool_history separately requires a known contract address and the
requested window. A DefiLlama pool UUID, component ID, candidate prose or market pair is
not a canonical pool contract. If no verifiable pool is returned, retain
lp_pool_identity_required as a structured capability_gap/status:blocked,
with historical_data_verified:false; stop before drafting or scope extraction.
Do not repeat unchanged calls or infer global pool/history unavailability.

Select the LP family from the user's explicit thesis before drafting. A generic
Uniswap LP request does not select dynamic rebalance, wallet/NFT ownership, or
USD fee/gas forecasting. The lifecycle contract requires those resources only
when the user selects lifecycle execution, owned-position management, or
rebalance behavior. A fixed-range model may be presented as an explicit research
assumption with its sampled-support limitation; it is not permission to replace
the requested full-window backtest or to silently change an existing candidate.

If a producer has returned canonical chain, pool, ordered token contracts,
decimals, fee and tick spacing, bind them in params.lp_source in the SAME
task's strategy_draft_proposal inline strategy.yml, keeping the returned
proposal_id thereafter. Retain explicit admitted task instructions about
ETH/WETH mapping in strategy.md; candidate prose alone cannot authorize a
mapping or amend request_scope. If only a WETH pool is found for admitted ETH,
report the exact asset mismatch and required explicit task amendment. Keep
the original markets and window until such an amendment is actually admitted.
lp_binding in the draft receipt confirms a saved declaration only. Its
lp_history_unverified/replay_status:blocked remains until historical producer
evidence verifies that pool, tokens and the entire admitted 30-day window.
No history or incomplete coverage means blocked replay, not a successful backtest.

For a selected lifecycle contract, assemble the inline
`files["strategy.yml"]` manifest with `source_id`, the exact declared
`wallet_id`, `chain`, `protocol`, `pool_id`, ordered tokens/decimals, fee,
tick spacing, component binding and account request. Reuse current-task producer
receipts and the selected pool/fee; do not invent a wallet or substitute a paper
account ID. For a generic or explicitly fixed-range research request, keep the
manifest at the selected research contract's fields and do not add lifecycle
wallet/NFT/USD-economics requirements merely because LP is mentioned.
Read `references/lp-sampled-research.md` before any quote-to-tick calculation:
raw token units require the decimal factor; a human ETH price alone is not a
pool tick, and USDT is not USDC. Keep unknown bindings blocked, not fabricated.

For `historical_rpc_authorization_required`: Preserve the non-retryable receipt
as the next recovery requirement and continue only after the access precondition changes.
Another identical query or a cosmetic argument
change cannot repair authorization. DefiLlama APY observations are research attachments,
not substitutes for pool tick, liquidity and fee-growth history. A verified current
pool can support a bound, unactivated candidate with explicit history limitations;
its replay stays blocked until an appropriate historical producer passes.
Public summaries name the verified current identity and remaining history gap,
with the recovery action; exact tool diagnostics remain in execution details.

For an explicitly fixed-range historical research request, consult
`references/lp-sampled-research.md`. Its sampled quote-unit model does not
replace dynamic rebalance or waive the original task's historical requirements.

For the candidate manifest, use the exact protocol-qualified pair identity:
`UNISWAP_V3:ETH/USDC` for a requested Uniswap V3 ETH/USDC strategy.
This is a scope identifier, not a candle provider or an execution resource.
An `ETHEREUM:` venue or `OKX_ONCHAIN:` data source does not identify the
Uniswap protocol. Keep the admitted asset spelling; ETH and WETH are not
interchangeable scope identities. Bind the separately discovered chain, pool,
token contracts and fee tier in the financial resource configuration.
Read scope error differences and repair the indicated manifest field; market
data calls, credentials and delivery-stage updates cannot repair that identity.

Use pool tick, liquidity, principal, fees and gas-cost snapshots. Check range
exit persistence and cooldown; forecast incremental fees from a documented
source, never from a made-up APR. Unknown economics means no automatic
rebalance. Select an existing paper account explicitly in `accounts`; the
protocol-qualified market prefix does not select an EVM paper account or
create a wallet/execution resource. Keep the strategy and its schedule disabled.

For reusable executable LP authoring, save `strategy.yml` with explicit
`execution_mode: script` and the three parameter mappings `lp_policy`,
`lp_source`, `lp_execution`. The generator copies
`templates/lp_range_strategy.py.j2` as `main.py`, plus `lp_range_policy.py`,
`risk_rules.py` and `lp_rebalance.py`. Prose mentioning LP does not select
these assets. Empty mappings select a closed scaffold and require completion;
they supply no pool, addresses, history or execution permission. Explicit
candidate file overrides remain authoritative.

Select these assets on the initial draft by passing only the small
`files["strategy.yml"]` manifest override with those mappings; finish its
configuration and tests incrementally in that candidate. If a generic draft
already exists, editing its manifest does not rerun the generator. Keep its
proposal ID and copy the four named templates into their corresponding
candidate files, then configure and validate them. Do not create another
proposal merely to select templates.
Use the template read's top-level `revision` with the existing `write_file`
source mode instead of retyping its body:
`{"path":"<returned candidate main_path>","source_asset":{"skill":"strategy_author","file":"templates/lp_range_strategy.py.j2","expected_revision":"<full template file revision>"}}`.
Repeat one call per companion, using that asset's own revision. Read an existing
target before replacing it. Template copies preserve all normal write guards
and establish neither configured behavior nor verified historical replay.

`templates/lp_range_contract.py.j2` supplies candidate-local behavioral tests
using installed SDK result, clock, input and state types. Initial LP generation
copies it to `tests/test_contract.py` unless explicit tests were supplied. For
an existing candidate, first read its tests and reconcile any pending/expired
write authorization; revise them only through the normal authorized file lane.
The asset's synthetic snapshots exercise lifecycle and recovery branches;
they do not configure the saved manifest or attest pool history, wallet ownership
or performance. Empty LP configuration remains structurally blocked even when
the asset's fixture tests pass.

The manifest contract is:

- `lp_policy`: `max_age_seconds`, `persistence_seconds`, `cooldown_seconds`,
  `cost_multiple` (at least 1), boolean `enter_when_empty`,
  `exit_below_principal_usd` (nonnegative), `collect_min_fees_usd` (positive).
  All other numeric limits must be finite and positive; booleans are not numbers.
- `lp_source`: declared `source_id`, exact `wallet_id`, `chain`, `protocol`,
  `pool_id`, explicit `token_id` (null only for initial entry), ordered
  `token0`/`token1`, two `decimals`, `fee`, `tick_spacing`, and the exact installed
  `component_binding`. `account_request` is a reviewed, schema-valid financial
  request with matching component/wallet/chain/protocol and parameters.pool_id.
  `account_state` is the SDK method, not a financial request kind: pass an
  installed supported action request (for example the reviewed `lp_add` request)
  to `ctx.financial.account_state(request)`. This only reads inventory and does
  not prepare or execute that action. A read request does not authorize a write.
  Preserve ETH/USDC as admitted scope;
  bind wrapped-token contracts separately using reviewed deployment evidence.
- `lp_execution`: immutable `operation_key` for one reviewed cycle, `requests`
  keyed by `lp_add`, `lp_remove`, `lp_collect`, and/or `rebalance_plan`. Supply
  only supported actions; a decision without its configured request fails closed.
  Each request uses the installed component schema and exact resource binding.
  Initial `lp_add` omits token_id and position_id, uses aligned reviewed ticks,
  bounded human-unit decimal amounts/minima and positive minimum_liquidity.
  Remove/collect require the selected NFT and reviewed collection bounds;
  removal additionally requires explicit owned liquidity. No amount, allowance,
  swap route or transaction deadline is inferred from prose.

Publish the actual provider output for the declared source through
`ctx.inputs.source(source_id)` (custom sources may be prepublished as
`source:<source_id>`). Its `lp.snapshot.v1` envelope requires `schema`,
`health: ok`, nonempty `source`, Unix-seconds `as_of`, and the exact
wallet_id/chain/protocol/pool_id. `economics` has its own source/health/as_of,
matching pool_id, `currency: USD`, `expected_incremental_fees_usd` and
`total_execution_cost_usd`. Unknown economics uses null; it closes entry,
collection and rebalance gates. A source label alone is not proof of provenance;
retain upstream pool/block/quote/forecast evidence with the provider artifact.

Live calls `ctx.financial.account_state(account_request)` through normal
resource validation. Its wallet-wide `positions` inventory contains each NFT's
verified `pool.tick`, token orientation/decimals, ranges, raw liquidity,
principal_value_usd and owed_value_usd. Select the exact configured NFT; wallet
ownership alone is not sole strategy ownership. The financial adapter checks
strategy claims before preparing exits, collection or rebalance. Missing or
incomplete inventory is an error, never an assumed empty wallet. For empty
inventory, the declared snapshot must supply `pool` with the same verified
metadata (including current tick); account_state supplies no empty-pool tick.

Paper/shadow/backtest consume an explicit `snapshot.account` in the same SDK
account shape and make zero financial calls. Their StrategyResult stays hold
and reports `lp_decision`, `financial_execution_allowed: false`, `funds_sent:
false`; signals are not fills. Published pool metadata uses the typed public
`underlying_tokens: [token0, token1]` field to preserve addresses through the
input redaction boundary, and component_binding.revision may use
`{sha256: <installed revision>}`. Live adapter rows retain their raw fields.
These snapshots are a strategy input contract, not a historical data producer
or a concentrated-liquidity replay engine.

`run(ctx)` implements initial entry, in-range hold/collection, principal-floor
exit and persistent range rebalance using `risk_rules.rebalance_signal`.
After gates pass it calls actual financial prepare/execute/get/reconcile, or
the durable rebalance coordinator. The latter requires owned-NFT removal,
optional supported pair-only ratio swap and new-NFT entry, an immutable finite
expires_at and max_total_fee_usd. After partial execution, preserve the
coordinator ID, child receipts/cashflows and pending action; entry can spend
only confirmed proceeds. Treasury ratio swaps require an already configured
supported route; LP support does not imply every router is supported.

Persist the operation before preparing it. Reconcile saved operations before
fresh data/config reads, with their original key/request/plan; never regenerate
deadlines or switch NFTs to escape an unresolved child. A confirmed cycle
stays latched until the manifest explicitly supplies a different operation_key.
An interrupted prepare with no action ID may attempt only its original key;
producer mismatch/idempotency conflict stays blocked for operator recovery.
Subsequent ticks do not execute previously prepared single actions. Coordinator
continuation still enforces original producer authority, grants, approvals,
prerequisites and proceeds. Stopped/uncertain cycles require explicit recovery.

The SDK/gateway supplies the trusted financial context for quote, account
reads, validation and execution. Candidates must use that boundary rather than
injecting identity into runtime config. If a producer reports an identity or
approval prerequisite, preserve its prepared action and follow its recovery
receipt; never bypass validation or regenerate the operation. Offline transport
fixtures verify this binding, not production execution or historical coverage.

A candle-only range observer is a separate, incomplete diagnostic artifact,
not the requested LP execution strategy. Keep that distinction in the candidate
and final report if pool inputs are unavailable. V3 tick conversion requires
verified token0/token1 orientation and decimal units; the symmetric full-range
constant-product loss formula does not establish concentrated-liquidity loss.
Validate those domain assumptions independently of tests copied from the same
formula. Package tests prove only their stated assertions, not fee growth, pool
accounting, historical coverage or deployable LP behavior.

## Financial actions and protocol inventory

For deposit-APY entry/exit rules and historical rate simulation, load
references/rate-policy.md before drafting. It specifies the reusable
implementation and host-owned replay; preserve the exact pool and window.

For Aave supply/withdraw strategies, load
`Skill(skill="financial_ops", file="references/mainstream-defi.md")` and
`templates/aave_supply.py.j2` before implementing entry/exit. The tested helper
must be saved as `aave_supply.py` in the SAME candidate and called by its actual
`main.py:run` entrypoint. The protocol scaffold also ships
`templates/aave_strategy.py.j2` as main.py with false data/decision gates; replace
those gates with the requested rules and timestamped inputs, retaining the
helper's binding/recovery checks. Loading a template without using it does not
implement the protocol. Validate and test both saved files together. The helper
uses `ctx.financial` with `lend_supply`/`lend_withdraw`; spot
`ctx.trading.open_position/close_position` cannot implement protocol deposits.
Keep the reviewed wallet/chain/reserve binding explicit. A paper account label
or APY calculation supplies neither that binding nor an execution receipt.

Use the documented `ctx.financial.prepare/execute/get/reconcile` contract and
reviewed component schema. Keep action identity and quote hash durable. Confirmed
submission is not necessarily a fill; holding time starts from confirmed fill
evidence. Classify failed-before-submission separately from pending/uncertain
effects; reconcile those effects before considering another action.

A generic portfolio row does not guarantee a protocol position identifier.
Resolve strategy-owned inventory through the component's documented read surface,
validate network/account/market and preserve the schema's identifier type. Do not
convert numeric protocol IDs to strings or infer them from a market name. Close
only owned confirmed exposure with its actual size and reduce-only semantics.

Test parameter names/types and SDK results against the actual installed contract;
permissive fake methods can conceal invalid closes or unsupported facades. For
non-candle inputs read `references/replay-capabilities.md` before submission.

## Acceptance and review

Test generated source's true/false/stale branches, zero LLM calls on no-signal
ticks, budget fractions, partial fills, cost accounting, original-ID recovery,
duplicate suppression, and paper/live separation. LP tests must include the
actual remove -> confirm -> optional swap -> confirm -> add sequence. Protocol
RPC fixtures establish transport behavior, not live-money returns. A real
model-created strategy and deployment acceptance remain separate tests.

Review is a script gathering compact real execution/cost/risk evidence, then
one Agent proposing changes to strategy code and the next review plan. Keep
review cadence and triggers strategy-specific. Compare net returns to hold
without relabeling underperformance as an execution failure. Retain missing
fee, price, replay and chain-confirmation evidence as explicit review gaps.


## Preserve the financial request envelope

An SDK method name is not an action kind: account_state(request) reads a trade
resource descriptor with kind=trade for a trading component. Source attribution
belongs inside plan; unknown top-level metadata is rejected. Component parameters
such as reduce_only and protocol position_id belong in request.parameters, with
the component schema's original type. Read producer state before status as a
compatibility fallback. Execute only prepared actions within authorization;
query/reconcile pending original identities before another action.

Never infer ownership merely because the account has one matching position.
Recover owned exposure from the strategy's confirmed original action receipt and
verified component inventory. Missing action attribution stays blocked. Disabled
schedules and paper mode do not require deleting the requested execution path.
A requested trading strategy still needs positive entry/exit contract tests and
same-source replay, with account orders gated separately.
