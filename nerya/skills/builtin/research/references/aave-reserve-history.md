# Aave historical reserve observations

Use this branch for a missing historical Aave reserve rate or index. Reuse the
admitted pool, chain, underlying asset and UTC window; keep old replay results.

1. Discover `data_api` action `aave.reserve_state_history` and read its schema.
   Resolve the exact catalog UUID, official market and underlying-token address
   from existing identity evidence. `market` is the official market's Pool
   contract (`markets.address`), not the reserve's aToken; use the catalog aToken
   to match the reserve within that market. Its producer checks the UUID's aToken
   against official metadata and the historical reserve response. A symbol or
   similarly named market is not a pool identity.
2. For a recorded gap, choose an instant inside that exact interval and pass
   `at_utc`; the producer resolves its floor block and retains the neighboring
   header proof. If canonical block numbers are already known, use
   `block_numbers` instead. Select a supported public `rpc_provider` from the
   schema. No wallet, credentials or transaction is needed.
   Verify one sample before expanding acquisition. A rejected historical read
   applies to that source and operation: inspect its diagnostic, then investigate
   another supported public source for the same identity and window. Keep the
   failed receipt; report a data gap only after assessing the available sources.
3. Read the returned block/hash/timestamp, `current_liquidity_rate_ray`,
   `liquidity_index`, `normalized_income_ray`, `last_update_timestamp` and
   `base_supply_rate_percent`.
   Keep the producer snapshot/reference and source hashes as the evidence;
   a copied report is not a new verification receipt.
   Count samples inside a gap using their actual block timestamps, separately
   from requested `at_utc`. A floor block before the gap is a boundary sample.
   Keep exact integer index strings in JSON evidence; put narrative notes in
   Markdown files, with the requested and observed times labelled separately.
   Prefer the host's saved raw JSON as the data artifact. A small evidence
   manifest can link those references; reuse the producer's rows, hashes and
   retrieval times rather than retyping the dataset into a new report file.

The [Aave ReserveDataLegacy definition](https://github.com/aave-dao/aave-v3-origin/blob/main/src/contracts/protocol/libraries/types/DataTypes.sol)
defines `currentLiquidityRate` and `liquidityIndex` in RAY units. The former is
an annualized supply APR. The producer divides it by `10^25` to express percent;
this is also the base-rate mapping used by the
[DefiLlama Aave v3 adapter](https://github.com/DefiLlama/yield-server/blob/master/src/adaptors/aave-v3/index.js).
It does not compound it into APY or include external/native rewards.
`liquidity_index` is the stored reserve counter paired with
`last_update_timestamp`, not a fresh `getReserveNormalizedIncome` calculation
at the sampled block. `normalized_income_ray` comes from that separate Pool
view call at the same canonical block and shares its header rechecks. Both are
exact integer RAY indices, not percentages or account returns. Preserve both
fields when comparing observations; no local floating-point reconstruction is
needed.
The [official reserve logic](https://github.com/aave-dao/aave-v3-origin/blob/main/src/contracts/protocol/libraries/logic/ReserveLogic.sol)
defines this block-time normalization; the producer reads the Pool view directly.

`base_supply_rate_percent` therefore cannot silently replace an older total
`deposit_apy_percent` series. Before replay use, the host must admit a compatible
rate definition, identity, time/availability semantics and provenance. The
current action declares `replay_input:false`, `point_in_time_ready:false` and
coverage `not_asserted`: it supplies actual missing observations for research,
not complete-window or historical-performance evidence. Report this remaining
normalization/admission work instead of rerunning an unchanged replay.

For official averaged supply APY rather than block state, inspect
`aave.supply_apy_history`. Its unknown averaging/availability semantics remain
separate; an average chart observation cannot attest historical reserve state.

For a declared historical reserve-index policy simulation, the backtest host
can prepare `aave.reserve_index_history` from explicit original bounds and an
observation interval. It returns requested decision times separately from
actual floor-block times and exact normalized-income indices. This distinct
simulation input has explicit instantaneous-observation assumptions; use
`Skill(skill="strategy_author", file="references/rate-policy.md")` for its version-2
candidate contract. Ordinary point research continues to use
`reserve_state_history`.
