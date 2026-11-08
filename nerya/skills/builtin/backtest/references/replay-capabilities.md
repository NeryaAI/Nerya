# Choose replay from actual capabilities

Use the research and comparison workflow in the backtest Skill. Preserve explicit
strategy, asset, chain/pool, window and risk requirements; choose unspecified
research periods and assumptions from usable data. A missing input narrows the
claims that can be made, rather than ending all authoring or research.

## Resolve inputs and evidence level

- Native replay suits a saved entrypoint consuming candles/features with supported
  sizing, orders and protection semantics. Reuse available exact-venue history.
- Lending deposit-APY policies use
  `Skill(skill="strategy_author", file="references/rate-policy.md")`.
  Saved `params.rate_replay` selects host rate-policy replay over attributable
  observations. Modeled allocations are not chain execution or realized yield.
- Pool-price simulation must identify the execution target separately from the
  actual dataset, token/quote direction and fill/cost assumptions. The installed
  `geckoterminal.pool_ohlcv` schema and saved `params.price_simulation.dataset_id`
  support their advertised bindings. Follow the matching branch in
  `Skill(skill="strategy_author", file="references/mainstream-strategies.md")`
  for that interface. Do not silently change a user-specified asset or venue;
  clearly distinguish supplementary proxy analysis from exact-pool evidence.
- News, wallet flows, reserves, LP positions and protocol actions need observations
  with timestamps and an explicit simulation model. Candle replay does not
  populate arbitrary `ctx.news` or financial snapshots. Use a strategy-local
  freeform replay when the generic engine cannot express the inputs.
- Reviewed workspace providers can expose `get_klines` through
  `ExchangeProviderSpec`. Use the existing adapter proposal lane for a new
  integration; installed providers and approvals stay under their normal owners.

Generic freeform research does not require byte-for-byte equivalence with a host
LP template or that template's data protocol. Use supported package inputs and
the sandbox; state observed versus modeled fields and keep source/data hashes.
Host validation of prepared inputs or LP accounting supplies stronger evidence
when available, but is optional for a limited custom study. A script's PASS,
coverage or return fields cannot create host-verified performance.

Read `verification.checks` as distinct outcomes. The minimal replay delivery
requires `source_binding`, `historical_run`, `replay_source` and `report_output`
plus `report` and `run_receipt`; authoring also requires `strategy_contract`
and `candidate`. `request_scope_match`, `historical_data`, `data_coverage`,
`execution_integrity` and `behavioral_tests` are optional diagnostics. Keep
their real statuses and limits, without adding them to stage `required_checks`.

## Sparse history and bounded discovery

Inspect compatible saved history first, then use installed source catalogs,
schemas and primary-source discovery through
`Skill(skill="research", file="references/onchain-data-discovery.md")`.
An empty local cache, missing adapter or failed provider request establishes
only that local observation. Continue read-only research and available acquisition
within the task's permissions without requiring a separate research grant.

Compare several useful supported periods or resolutions, selecting them from
data availability and the strategy's lookback needs. Prefer existing attributable
observations over an exhaustive reconstruction. If the requested period is
unavailable, keep that gap visible and run supplementary windows, sampled studies
or explicit custom scenarios where useful. Honor explicit dates and timeframe;
an alternative study does not fulfill a missing requested period. Unsupported
synthetic/proxy inputs cannot be relabeled as historical facts.

Continue authoring, validation and reporting independently of missing historical
inputs. When no honest financial calculation is possible, deliver the saved
candidate, observed data and diagnostics with replay/returns marked not run or
unverified. Report an actual partial calculation when one exists. Ask only where
access, cost or a material change to explicit requirements needs user input.

## Script-gated Agent decisions

A deterministic script uses `strategy_backtest` without `policy_check_only` or
`settings.agent_replay`. `execution_mode:script` alone does not imply model calls.

For actual model decisions, use the existing bounded
`backtest.retrospective_replay` authorization path. Check with
`strategy_backtest(policy_check_only=true)` without candidate/replay arguments.
The result checks the loaded policy, not remaining budget, ledger reconciliation,
backend readiness or completed replay. Use its actual output cap, deadline and call/token
limits; an old grant or `enabled:true` alone is not current authorization.

When this policy check opens an authorization question, follow that existing
interaction. After the user answers, pass the exact persisted interaction ID as
`policy_recheck_interaction_id` alongside `policy_check_only:true` for the bounded
recheck. It is neither a nonce nor a grant. Missing/expired authorization limits
model calls; it does not forbid data research, package authoring, validation or
deterministic branch checks. Preserve the same candidate and reports while
resolving model permission through the available path. Do not manufacture
authorization or silently replace a requested model decision with a fixed stub.
For an ordinary recovery question, use the plain policy check; its answer is not
the specialized authorization interaction required by `policy_recheck_interaction_id`.

With valid authorization, `settings.agent_replay:true` supports a script with one
declared confirmation role and candidate-local prompt. The host supplies the
historical prefix and invokes the pinned model outside the candidate sandbox.
Read `decision_replay` receipts and their source/data/prompt binding; current-model
retrospective decisions are not contemporaneous or live performance evidence.

A genuinely sent request with `decision_request_unconfirmed` remains charged and
unknown. Distinguish its request deadline from the authorization deadline.
Cancellation may occur after earlier decisions completed. Read the saved tape
and producer status before reporting what ran; an expired deadline alone does not
establish the outcome of the last request. Preserve completed receipts for reuse.
Preserve the request digest, run/failure receipt and ledger; reconcile through
provider-owned response/execution evidence and the supported recovery path before
retrying that effect. A local digest is not a provider task ID. Renewed consent,
a fresh grant, another engine or a new study window does not reconcile an unknown
request. Do not replay old unconfirmed work or rewrite it as never sent.
`unconfirmed` means no confirmed outcome is available yet, not that evidence can
never be recovered. Describe the producer's available recovery action; when none
is established, state that limitation. Preserve confirmed receipts and their
bindings without inventing either guaranteed resumability or a need to rerun
everything under another authorization.

For unsupported `ctx.subagents`, consult `references/config_schema.md`.
`mock_surfaces.subagents.mode:replay` is reserved and unimplemented;
`execution_mode:agent` alone reports `agent_execution:not_run`.
For a misplaced unconditional model call, use
`Skill(skill="strategy_author", file="references/trend_follow_subagent.md")`.
Repair the same candidate and validate no-signal zero calls, eligible confirmation
and deterministic exits. Bounded synthetic verdicts test behavior only, with
`performance_evidence:false`; they do not prove historical Agent returns.

## LP evidence choices

Verify exact pool/network, ordered tokens, quote direction, range and actual
timestamps. Distinguish price history, APY observations, sampled fee/state data
and full lifecycle accounting. Inspect `row_count`, `truncated` and
`sample_truncated` before treating a preview as the full dataset.

Use the available fields to choose a useful sampled or custom study and disclose
fee, liquidity, tick-path, fill and valuation assumptions. Neither price nor APY
alone proves full LP execution. Prefer several supported windows over collecting
every scheduled state and event bridge. Full block/state reconstruction is an
optional stronger path when exact lifecycle or fee accounting is explicitly
needed; use `references/history-data.md` for its existing inspectors and workers.

A completed sampled/custom calculation may be delivered with
`requested_window_complete:false`, `coverage_ok:false`/unknown or
`performance_evidence:false` as actually returned. Preserve these values and the
report. If full lifecycle evidence was requested, report that outstanding gap
alongside useful partial findings; do not label them full lifecycle results.
The delivery finalizer owns workflow completion, not the truth of a modeled fee.

## Repair and report

Read a structured failure once and fix its diagnosed input/dependency/SDK defect
in the same candidate. Validate changed source before another run; a file write
is not validation. Use real SDK types for relevant behavior checks and reconcile
pending/unknown effects before retries. An unchanged failure calls for another
supported research approach or a limited report, not repeated execution.

For custom replay, load `references/custom_replay_template.md` for supported
entrypoints, facade calls and artifact names. Run the saved strategy/helpers on
timestamp-ordered historical prefixes; identify any independently reimplemented
model as a separate simulation. Bind source endpoints, network, row ranges,
hashes and package revision to the report. A current snapshot is not past history.
Keep sandbox network/process restrictions and all risk/trading gates intact.
