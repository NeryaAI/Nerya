# Local historical data and reliable replay

## Short path

Prefer existing usable history and the normal `strategy_backtest` history
preparation. Use `historical_data` for explicit inventory, preparation or recovery;
local candle inventory does not establish protocol-state/fee replay capability.
A native `preflight_only:true` call is useful for a suspected source/dependency
error before a long download, not a mandatory extra step for every run.

Bind each dataset to its actual instrument, timeframe and UTC interval. For a
relative recent-N-days window, use `days` without `start`/`end` and read the
producer's resolved bounds. Explicit `start` is inclusive and `end` exclusive.
Compare requested and actual dates before making coverage claims; tool defaults
do not establish the user's period. Record observed rows, expected bars where
meaningful, warmup and gaps for each study.

Use `historical_data(action="list", dataset_kind="candles")` or
`historical_data(action="inspect", ...)` for local inventory. Download matching
history through `historical_data(action="download", markets=[...],
timeframes=[...], start=..., end=...)` where needed. Reuse successful segments
across overlapping periods. Native `strategy_backtest(..., data_mode="local")`
uses prepared data without downloading; creating another candidate is unnecessary.

Choose several useful periods from supported data rather than enforcing a fixed
list. Preserve explicit user dates and strategy timeframe; keep an unavailable
requested period visible alongside supplementary studies. Sparse history can
support shorter/other windows, coarser sampling or custom simulations with stated
assumptions. Coverage gaps do not by themselves require ending the backtest.

## Saved LP history and reuse

For “reuse existing history”, resolve saved inputs before choosing a new window
or acquiring data. `historical_data(action="list", dataset_kind="candles")` lists
candles only; it does not inventory `data_api` pool-history results.
`lp_state_requirements` supports candidate-bound `inspect`, not `list`; an
`invalid_requirements_args` result establishes no absence of LP history.
`data_api(op="list")` lists capabilities, not saved datasets. Bound absence
claims to the inventory actually inspected.

Inspect supplied dataset/run/raw refs, current task evidence and advertised
provider cache inventories. Verify chain/pool, ordered token contracts/decimals,
quote direction, fee/range where relevant, UTC/block bounds, sample grid and
source hashes. Same pool alone does not make one tick range's fee counters
valid for another. Public observations can be reused as data with their original
provenance; prior approvals, uncertain requests and delivery checks do not transfer.

When no dates were specified, choose useful compatible saved windows and explain
them. For the existing host-prepared Uniswap path, use
`data_api(op="snapshots", provider="uniswap", action="pool_history")` to recover
this task's producer refs and exact args. Put the returned `producer_ref` beside
unchanged `provider`, `action` and `args` in `replay_inputs.json`; the preparer
verifies and reuses that snapshot. A `raw://` ref is not a `producer_ref`.
An explicit ref pins the saved result; inspect the prepared receipt to confirm reuse.
The returned pool/token metadata can be reused without another discovery call.

If host-attested reuse is unavailable, preserve that verification limit and use
supported saved package data for a clearly labeled custom study where possible.
Document its actual source and hashes; copied rows do not acquire a host
attestation. Continue ordinary read-only discovery/acquisition through existing
permissions, or the adapter proposal lane for a new integration. Ask only for
missing access, paid resources or a material change to explicit requirements.

## Uniswap pool-event archive

Use `historical_data(action="inspect", dataset_kind="uniswap_pool_events", ...same request...)`
to read the archive receipt, counts, bounds and next cursor. This is the data-only
readback path; opening its SQLite store through `run_shell` is not a verification
prerequisite.

### Optional full LP lifecycle reconstruction

Choose this branch when exact lifecycle, scheduled decisions or fee accounting
needs full-state evidence. Ordinary LP research should first use available
sampled/custom inputs and compare supported windows. Neither a disabled cron
schedule nor a generic full-window backtest request requires collecting every
cron state and event bridge by default. Partial studies retain their actual
coverage and assumptions.

For a host cadence study, inspect the saved candidate with
`historical_data(action="inspect", dataset_kind="lp_state_requirements",
proposal_id="<existing candidate>")` (or `strategy_id`). This offline planner
counts schedule decisions and accounting boundaries against local state evidence;
it does not execute the strategy or download data. It supports `mode:cadence`
and legacy absent-mode cron candidates. Explicit `lp_replay.mode:sampled`
returns `lp_state_requirements_mode_mismatch`; retain the sampled report and
choose a cadence study only when its evidence is needed. A revised candidate
uses the normal proposal/validation lane and keeps schedules disabled.

For this stronger host path, use the inspector's exact `next_download_args`
for missing states and `next_interval_download_args` for event bridges.
`max_states` defaults to one and is at most four per synchronous call.
Preserve candidate revision, pool/range, UTC/block bounds and cursor within that
acquisition. Missing states and unverified bridges limit this full-cadence path,
not all generic freeform research. Two endpoint snapshots alone cannot verify
fees accrued between them. Host `prepare_lp_cadence` accounting is optional;
a custom calculation need not match its template or protocol to run.

For a larger chosen acquisition, the existing worker interface is
`historical_data(dataset_kind="lp_state_job", action="start")` with one saved
`proposal_id` or `strategy_id`, `expected_source_revision`, stable `request_id`,
`max_states_total` and `timeout_seconds`. State jobs accept up to 10,000 lifetime
attempts. Choose a useful finite collection plan consistent with the user's time
and cost constraints, rather than automatically filling the whole missing count.
`timeout_seconds:0` is reserved for an intentionally chosen run without a job
deadline; do not select it merely to chase full coverage.
Do not pass `expected_next_timestamp` to `start`.

Keep returned `job_id`/`task_id` and follow `next_status_call`; use
`task_output(task_id=..., wait_for_completion=true)` for the worker result.
Recover a missing handle through `task_list`/`task_get`, not another worker.
For `owner_unconfirmed`, inspect
`historical_data(..., action="status", job_id=...)` before resuming.
`stop` requests cooperative cancellation; `resume` of the same stopped job
retains consumed attempts, deadline, source binding and committed archives.
A running/unconfirmed worker must be reconciled before any replacement.
A new request ID must not reset a budget or replay uncertain work.

Event acquisition uses a separate `lp_state_job` with `phase:"intervals"`,
stable `request_id`, the same candidate/revision and `timeout_seconds`.
Its optional `max_requests` limits lifetime event-operation attempts, including
failures/retries; it is not a block/interval count. Do not pass `max_states_total`
to this phase. Preserve separate job receipts and do not convert an active job.
The worker follows inspector cursors and stops on unknown errors or no progress.

Read state/interval counts, boundary evidence and continuity from actual receipts.
`states_acquired`/`intervals_acquired` describe collection work, not replay or
performance. Preserve failed continuity reasons. After a bounded collection,
deliver useful results or choose further work according to the research need;
batch exhaustion does not compel exhaustive reconstruction.

After exact pool discovery and a bounded event/state probe, use the existing
`historical_data` tool with `dataset_kind: "uniswap_pool_events"`. This branch
supports `inspect` and `download`, with `pool_address`, explicit `rpc_provider`,
and producer-resolved inclusive `start_block` / `end_block` for the selected
study window. Preserve explicit pool/date requirements; record any supplementary
window separately.

`chunk_blocks` is at most 100 and defaults to 100. `max_chunks` defaults to one
and is at most ten per call; it bounds this call, not the declared window.
`start_block` / `end_block` always declare the entire admitted target range for
one archive ID, not one chunk. Keep identity, both bounds and `chunk_blocks`
unchanged, using `max_chunks: 1` for this bounded workflow. After each result,
copy its `next_download_args` for the next call: the producer supplies
`expected_next_block`, and an old committed cursor returns saved progress
without fetching again. `next_block` never replaces `start_block`; do not
invent a nonce or alter the range to evade write deduplication. Offline
`inspect` neither fetches data nor creates a cache.
For fixed-range LP accounting preparation, pass both `tick_lower` and
`tick_upper` from the saved strategy. They are part of the archive identity;
an event-only archive cannot acquire range evidence by relabelling it. The
producer records both boundaries' historical state and follows Mint/Burn
liquidity changes, rejecting continuity when a boundary is cleared or rebuilt.
`last_chunk_range_continuity` describes only the named chunk, not the whole
archive or all scheduled decisions. Preserve any failed continuity reason.
This is local data acquisition through the normal workspace-write permission
lane. Acquire subsequent chunks within the user's requested window; a chunk
size is an implementation batch, not an additional user authorization boundary.

Report stored ranges, next missing block, source hashes and limits.
`event_range_complete` means the requested event range is stored; it does not
prove initialized-tick reconstruction, fee accounting or full LP replay.
Read `requested_window_complete`, `tick_fee_reconstruction_verified` and
`replay_input` from the actual receipt; an event archive alone cannot attest
full LP accounting. Keep sampled reports and their limits. Coverage is an optional
diagnostic, not a prerequisite for delivering a real limited calculation.

## Candle CLI equivalents

```sh
nerya data download --workspace <workspace> \
  --markets BINANCE:BTCUSDT BINANCE:ETHUSDT --timeframes 1h \
  --start 2024-12-25 --end 2026-01-01 --progress

nerya data list --workspace <workspace>

nerya data inspect --workspace <workspace> \
  --markets BINANCE:BTCUSDT BINANCE:ETHUSDT --timeframes 1h \
  --start 2024-12-25 --end 2026-01-01

nerya backtest --workspace <workspace> --proposal-id <candidate> \
  --config <workspace-relative-config.yml> --preflight-only

nerya backtest --workspace <workspace> --proposal-id <candidate> \
  --config <workspace-relative-config.yml> --data-mode local --progress
```

Successful calculation and strategy profitability are different. An economic
FAIL still has metrics; a preflight or worker failure has a diagnostic receipt,
not invented returns. No command above promotes, activates or trades an account.

## Configuration

```yaml
start_utc: "2025-01-01"
end_utc: "2026-01-01"
tf: 1h
timeframes: [1h]
warmup_bars: 60
data_mode: local
coverage_policy: allow_partial
allow_timeframe_fallback: false
download_timeout_seconds: 300
max_run_seconds: 600
max_bars: 2000000
```

Keep the user's actual capital, sizing, position cap, fee/slippage, short-selling
and risk settings in this same file. Unknown keys, invalid booleans/timeframes,
negative costs and non-finite amounts are rejected rather than silently ignored.
One year must not quietly become the default 180-day preset. A calendar month
(`1M`) is not a minute (`1m`); unsupported intervals are explicit errors.

Use `coverage_policy: allow_partial` for research with incomplete observations;
report missing ranges, usable warmup and the actual `coverage_ok` status. Select
`strict` only when the study needs full candle coverage. A completed partial run
does not establish complete coverage, and a warmup hold does not establish absence
of signals. Session-based equities need calendar-aware interpretation; do not
generate weekend bars or claim continuous spacing verifies an equity calendar.

## Resumption and sources

This local store is a producer cache, not the source-discovery layer. If the
requested market, protocol or window is absent, do not cycle the same empty
`historical_data` inspection or treat a local provider error as proof that
history does not exist. Load `Skill(skill="research",
file="references/onchain-data-discovery.md")`, identify the exact
observation and a compatible read-only source, then use an installed producer
or use the adapter proposal lane for a missing integration. Prefer available
history and supported alternate periods/sampling/custom simulations over repeated
empty probes. Preserve explicit identity/date requirements and state any
supplementary study's actual window. Documentation establishes source capability,
not historical observations; a bounded sample establishes only its observed rows
and fields, not complete coverage or verified performance.

The workspace-local `artifacts/backtest_cache/history-v2.sqlite3` is a
transactional database, not an external service. It stores closed candles by
venue-qualified market, exact timeframe and open timestamp. Spot/derivative
venues remain distinct. Successful segments commit immediately. Repeating a
partial or interrupted download fetches only missing leading, internal and
trailing intervals; a deadline or short page is NOT proof of exchange history
limits. Empty responses never mark a range as covered.

For supported Binance archives, long ranges use monthly files first and daily
files for the remainder; other venues reuse the existing market-data adapter in
bounded chunks. Transient failures have bounded retry budgets. The receipt
shows cached/downloaded rows, requests, gaps, source hash and job ID.

Sample/mock/paper data cannot enter the real-history store. Old JSON files named
`.parquet` are not silently trusted or overwritten: the explicit migration
helper imports them as `legacy_unverified`, excluded from verified replay reads.
They require actual source refresh, not relabelling, to become historical evidence.

## Failure evidence

A native run has `run.json` with the authoritative phase/status, `preflight.json`,
`data_manifest.json`, a saved `source/` snapshot and `replay_input.json` containing
the exact loaded rows. A completed run also has metrics, charts and trade CSVs.
An unsuccessful run has `failure.json`; worker failures retain `worker.log` and
structured error details, including market/bar when known. Never interpret a
config-only directory or a tool progress event as a completed backtest.

Execution runs in a dedicated child process with a parent-owned timeout and
cancellation. Ordinary Python network clients are disabled in that worker so
strategy code cannot accidentally query today's prices while replaying history.
This is execution isolation, not an operating-system security sandbox. Static
preflight cannot prove all dynamic branches; a late dynamic error is retained as
failure rather than turned into successful performance.

Do not delete stop-loss/take-profit rules on an unsupported protection error.
Provide an explicit historical execution model or report the unsupported
surface. Do not add strategy, venue, retry or coverage rules to Agent Loop.
