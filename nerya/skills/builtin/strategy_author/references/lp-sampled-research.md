# Sampled fixed-range LP research

This reference describes optional host-accounted fixed-range sampled research.
Creation follows strategy_author's candidate-first contract; missing pool/history
bindings remain pending and do not prevent saving an editable draft.
The sampled model does not implement dynamic range selection or rebalance and
cannot replace a requested lifecycle
strategy merely because the historical capability is incomplete.
For a generic LP request, surface this as a fixed-range assumption and sampled
support level when selecting it; do not silently downgrade the requested full
window backtest or rewrite an existing candidate's family. This path does not
require live wallet/NFT ownership or USD fee forecasting, but it remains blocked
when its exact pool-history input is unavailable. Generic strategy-local custom
simulation is another research option; it need not use these templates or the
host sampled-accounting protocol and retains its actual verification gaps.

When full-window replay is an explicit user requirement, keep it visible alongside
partial studies; use the cadence branch in
`Skill(skill="backtest", file="references/history-data.md")`. Preserve its
original sampled run as partial evidence. Otherwise choose useful supported
periods or regimes autonomously, explain the selection and compare attributable
results as described in the backtest Skill. Agent-selected defaults are research
assumptions, not user requirements or authorization gates. Ordinary public data
reads within existing permissions need no new grant.

## 1. Prepare inputs for the chosen historical read

First resolve the pool using the LP work discovery sequence in
references/mainstream-strategies.md. A pair is not pool identity; pool_history
requires an already known address. Preserve explicit user constraints on ETH/WETH
mapping, pool and window, and continue the SAME candidate.

For “reuse existing history”, first read
`Skill(skill="backtest", file="references/history-data.md")`, section **Saved LP
history and reuse**. Resolve attributable saved inputs first. Honor an explicit
saved-history-only restriction; otherwise choose and disclose useful periods and
acquire public history within existing permissions when needed.

Discover `data_api` provider `uniswap`, action `pool_history`; inspect its schema
before choosing bounds. Preserve the exact requested chain, protocol, ordered
token contracts, pool and entire window. v1 supports Ethereum Uniswap v3 only. The provider
verifies factory, pool, tokens, decimals, fee and spacing at historical blocks,
rechecks block hashes and returns raw fee counters plus source hashes. It uses a
fixed public RPC selected by explicit `rpc_provider` from the current tool schema;
there is no automatic fallback or custom endpoint. Archive access may require
provider authorization.

Every request requires `pool_address`, `tick_lower`, `tick_upper`, `step_blocks`
and `max_samples`, plus paired `start`/`end` OR `start_block`/`end_block`.
Choose integer `tick_lower < tick_upper` within schema bounds and aligned to
verified tick spacing. Save the range as an explicit research assumption; it
is not observed pool state, the current tick or an owned position. If spacing
or identity is unavailable, retain that missing evidence rather than fabricate it.
Distinguish user-fixed bounds from an unchosen research parameter. Preserve
bounds actually specified or confirmed by the user; earlier model-selected
numbers are not user constraints. When bounds were never specified, formulate
a quote range with its aligned ticks as a disclosed research assumption using
the method below. Ask only if the choice materially changes the user's goal,
cost or authority; unspecified ticks alone need no user decision. Resolve
saved-history compatibility separately: a chosen range does not make mismatched
snapshots reusable.

Preserve user-specified `start`/`end`; record chosen bounds for each other study.
UTC dates resolve to floor blocks with
explicit timestamp offsets. Sampling includes both endpoints, with end included
once even when the step does not land on it. For block span `D = end_block -
start_block` and step `S`, required samples are `ceil(D / S) + 1` (one if D=0).
For example D=10, S=6 needs 3 samples, not 2. Set `max_samples` to at least
that count within the advertised cap (currently 64); for D>0 and budget M>1,
`S >= ceil(D / (M - 1))`. Date windows use resolved floor blocks, not an
assumed exact blocks-per-day conversion. On a schema or sample-budget error,
correct only the named parameters using the schema/returned bounds, preserving
pool and window; never repeat unchanged arguments. A larger `step_blocks`
reduces sample count; `max_samples` alone does not reduce it. Do this arithmetic
directly; `run_shell` is not a prerequisite or recovery route.
A successful read with the complete selected sample set is not a complete tick
path or complete requested window: if the first included observation is later
than the requested start, treat that as the sampler's boundary policy, not a
deadline. Attribute truncation to a deadline only when the failed receipt has
`failure_stage: deadline`; `coverage_ok:false`, a shorter recorded duration, or
`no_tick_path` is not deadline evidence. Repeating the same sample request does
not fill the excluded prefix or produce complete coverage.

For quote bounds, `pool_discover` supplies identity only: ordered `token0`/`token1`,
decimals, fee and spacing; it does not supply current tick or pool state. Uniswap v3 uses
`raw_token1/raw_token0`; `pool_price` uses `raw * 10^(decimals0-decimals1)`.
Convert human `quote per base` to ordered human `token1 per token0` first. If base is
token1, invert bounds (`ordered_lower=1/quote_upper`, `ordered_upper=1/quote_lower`).
Convert each ordered human bound to raw ratio with `raw=human*10^(decimals1-decimals0)`.
Then use `tick=ln(raw_token1/raw_token0)/ln(1.0001)`, floor the lower tick and ceil the
upper tick to `tick_spacing`. This is a range assumption only: a CEX quote cannot establish
pool state, liquidity, fee growth, fills or history; use `pool_history` for bounded history.
Before acquisition, round-trip the aligned ticks back to the declared human quote.
For token0=USDC (6 decimals), token1=WETH (18), USDC per WETH is
`10^12 / 1.0001^tick`; 2500..3500 USDC/WETH brackets ticks 194710..198080
at spacing 10. This is an arithmetic example, not an observed price or default range.
Ticks -90000..-70000 instead imply about 1.10e15..8.10e15 USDC/WETH; schema-valid
alignment alone does not validate the intended price range. Record both the quote
bounds and aligned ticks; compare any attributable same-pool historical tick
without treating it as current state or requiring every fixed range to be in range.
Keep boundary fee-growth evidence bound to the exact requested ticks. Changing
negative ticks to positive ticks in the candidate cannot repair the sampled rows;
obtain compatible producer-verified inputs or retain the range mismatch as blocked.

## 2. Recover historical access

Choose a supported archive provider from the current tool schema and the
research Skill's `references/historical-data-sources.md`. Tenderly's public
Ethereum gateway has returned a block-pinned archive read in native Python;
select `rpc_provider="tenderly"` explicitly for that source. Each successful
dataset retains its actual source and provenance; there is no silent fallback.

An HTTP 401/403 denies that source, not the whole task or the pool's history.
Preserve its receipt, then autonomously research or select another supported
public read-only source for the same pool, window and evidence contract.
`safe_to_retry:false` applies to the failed operation: leave it unchanged and
do not replay it indirectly. A separately selected source is a new read, not
a retry permission or an upgrade of the original receipt. Preserve any
user-pinned data provider; changing that explicit constraint requires user input.

Correct named input errors and follow producer retry/rate-limit guidance.
Ask only for a concrete missing credential, paid access or material scope
decision after available public sources have been investigated. The fixed
tool accepts its advertised provider enum, not arbitrary endpoints or keys;
provider access controls still apply.

Keep data coverage partial/incomplete (unavailable if no rows were obtained);
authorization failure is not proof that the pool or its history does not exist.
Save a disabled candidate with explicit pending bindings, or continue the SAME
candidate if one exists, recording unavailable inputs and the recovery blocker.
Keep trading and review schedules disabled. A saved candidate or passing package
validation does not complete replay; report missing replay evidence without
invented rows, returns, coverage or successful replay evidence. A requested
dynamic strategy retains its dynamic scope and missing capability; these
fixed-range assets cannot substitute for it.

## 3. Save the candidate; optionally use the host sampled assets

For a NEW LP candidate, use `strategy_draft_proposal` with its default
`validate:false`. Pool identity, history and implementation bindings may remain
explicitly pending. Saving or delivering the draft requires no template copying,
validation, tests or replay. Use `mode:paper` and keep execution and review
schedules disabled. For a compatible saved candidate, continue that SAME candidate.

For the optional host sampled implementation, these three revision-bound assets
are a compatible example. Copy/adapt them through ordinary proposal tools at the
returned candidate paths, or implement another suitable research script:

- `templates/lp_sampled_strategy.py.j2` -> `main.py`
- `templates/lp_sampled_policy.py.j2` -> `lp_sampled_policy.py`
- `templates/lp_sampled_replay.py.j2` -> `scripts/custom_replay.py`

When copying an asset, load `Skill(skill="strategy_author", file="<template path>")`
and use its returned `revision` in `write_file` on the actual returned candidate
path, using `source_asset` and omitting `contents`:

```json
{"path":"<candidate root>/scripts/custom_replay.py",
 "source_asset":{"skill":"strategy_author",
 "file":"templates/lp_sampled_replay.py.j2",
 "expected_revision":"<returned revision>"}}
```

Read an existing target before overwriting it; preserve task/proposal binding.
If the chosen scaffold includes tuning, this is a schema-valid objective in
`strategy.yml`; tuning itself is optional:

```yaml
tuning:
  objectives: [execution_quality]
```

When using that objective, merge it into the existing tuning block; keep execution
and review schedules disabled. For host LP replay, declare the data model with `params.lp_replay.mode`:
`sampled` executes at saved observations and always retains partial coverage;
`cadence` requires exact scheduled states and interval evidence. On continuation
for user-permitted sampled research, edit the SAME candidate's
`params.lp_replay.mode` from `cadence` (or legacy cron-based selection) to
`sampled` before replay. Reuse the compatible already-bound `producer_ref` with
identical dataset args as described below. A saved-data-only request permits no
new history/interval acquisition job: complete saved states with missing
intervals do not require filling those intervals for sampled research. Run with
the saved observations, or report incompatible/missing sampled inputs without
reacquiring them. Make the model change and sampled limitations explicit; retain
the failed cadence receipt. A saved mode/cron is not a user cadence requirement.
Preserve explicitly requested cadence: missing intervals remain missing, and
sampled research cannot certify that cadence or complete lifecycle coverage.
If behavior checks are needed, use candidate tests or a review plan rather than
`behavior_validation` in `tuning.objectives`. Passing the package contract does
not clear an archive/RPC blocker or prove historical replay.

When invoking the chosen host sampled producer, it requires these mappings under
`strategy.yml` -> `params`, not at manifest root. Complete its bindings for that
run; these are producer inputs, not prerequisites for saving a draft or for
generic custom simulation. This example uses `execution_mode:script`:

- `params.lp_source` binds `source_id`, `chain:ethereum`,
`protocol:uniswap_v3`, exact pool address in `pool_id`, ordered token contracts,
decimals, fee, tick_spacing, aligned `tick_lower`/`tick_upper`, and
`token_id:null`. Wallet/resource metadata is not proof of an owned position.
- `params.lp_replay` requires `version:1`, `mode:sampled` for this research path, declared `dataset_id`, decimal-string
`initial_token0_units`, `initial_token1_units`, `action_cost_token1`, and bounded
`max_gap_seconds`. Save all chosen inventory/range/cost assumptions explicitly.
- `params.lp_sampled_policy` requires boolean `enter_when_empty`, decimal-string
`exit_below_principal_token1` and positive `collect_min_fees_token1`.

Initial inventory is in human token units, ordered by the verified pool's
token0/token1 and decimals, not by the market label. The model performs no swaps.
For entry inside the fixed range, provide both tokens and enough token1 for the
declared action cost; a single-token balance cannot fund an in-range LP add.
Preserve user-specified balances. Otherwise disclose independent hypothetical
balances for both assets in strategy.md before that replay, without claiming
wallet ownership, equal dollar values or an implicit conversion. On an inventory
failure, repair only these declared assumptions in the same candidate; preserve
pool, ticks, window, snapshot and policy.

For this host sampled path, declare the provider request in `replay_inputs.json`,
following
`Skill(skill="backtest", file="references/custom_replay_template.md")`; its dataset ID must match
`params.lp_replay.dataset_id`. The host owns source/replay/policy configuration
from the saved manifest and prepares immutable data. When pool_history returns
producer_ref, include that exact ref with identical args in the dataset
declaration to reuse its complete snapshot without reacquisition. A legacy
raw log without producer_ref is not this receipt. The copied replay calls
`replay_sampled_lp()` with no arguments: it obtains the saved manifest, declared
dataset and actual entrypoint automatically. Do not pass replacement
source/replay/policy arguments or inspect internal `lp_replay` / `lp_accounting`
implementation as a prerequisite. The replay facade calls the actual saved main entrypoint with only the current
sample, real SDK result/input/config/clock types and bounded run-local state.
The host model consumes its decisions rather than reimplementing that policy.
No future fee forecast, wallet ownership, swaps or live financial calls are
invented. Unknown USD economics in the separate live-capable LP template remain
unknown; a hold-only run is not implemented allocation evidence.

## 4. Run requested research and report its evidence

For requested replay, follow the backtest Skill's validation and execution path;
these checks are not creation prerequisites. If an archive read is blocked,
use source recovery above and report remaining missing evidence alongside any
usable studies. Run `strategy_backtest(engine:freeform, allow_mock:false)` with
the actual candidate ID and read its returned receipt and report. Preserve the
execution sandbox, including its network and subprocess restrictions.
For the host sampled model, values are denominated in token1, not USD, even when
the token is named USDC. Sampled historical fee growth ignores counterfactual
dilution and price impact; uninitialized boundaries and unsupported range
changes fail closed. Gaps omit fee accrual. A completed calculation remains
limited research: `performance_evidence:false`,
`financial_execution_allowed:false`, `requested_window_complete:false`.
It does not satisfy full dynamic LP replay, account fills or realized yield.

For a new run whose saved replay script exactly matches
`templates/lp_sampled_replay.py.j2`, the host selects the trusted LP route:
the isolated worker invokes the saved entrypoint and the host ledger accounts
for its decisions. Confirm the new persisted metrics/result/chart receipt has
`replay.scope:sampled_fixed_range_counterfactual`,
`replay.strategy_execution_verified:true` and `replay.accounting_verified:true`,
with `entrypoint`, `dataset_id`, `prepared_input_sha256` and `rows_sha256` in
`replay` bound to the source/input snapshots. Read `action_counts`,
`actions_modeled` and `allocation_modeled` as modeled activity, and
`input_observations`, `admitted_observations`, `excluded_observations`,
`leading_gap_seconds`, `trailing_gap_seconds` and `gaps` as sampling limits.
This applies only to that new trusted run: an older
`isolated_custom_calculation` receipt with verification false stays unverified;
rereading it cannot attest the new seam. Partial historical observations and
verified sampled accounting can coexist with missing full-window coverage.
Approval of a sampling-grid change cannot make `requested_window_complete:true`
attainable through this sampled producer: boundary alignment leaves the tick path
between samples unobserved; retain the current request unless explicitly amended
and identify the missing historical event/state and reconstruction capability.
Keep `requested_window_complete:false` and `performance_evidence:false`;
these execution fields do not waive required delivery checks or establish a
complete tick path, fills, realized fees, live execution or historical profit.

In the final report, state the saved candidate, studies performed, actual results
and limitations. When the user explicitly requires full-window replay, state that
it remains incomplete if the producer has not established it. Report
`completion.allowed` separately as workflow status: incomplete coverage alone
does not prohibit delivering useful sampled research. Retain the producer's
failed/missing checks and partial/incomplete coverage. A scaffold's default
execution cron does not establish user-requested cadence or a required historical-state count for saved
`mode:sampled`. Complete coverage requires producer-supported historical
event/state reconstruction; more evenly sampled states alone cannot establish it.
Research does not enable trading or schedules, promote a candidate, place account
orders or bypass Risk Gate, Approval Gate or live-trading permission.
