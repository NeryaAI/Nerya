---
name: backtest
description: "Run or continue a saved Nerya backtest, including 继续回测 and incomplete LP replay; inspect current producer gaps, discover read-only on-chain 链上 history, and deliver version-bound results without activation."
version: 0.8.0
license: MIT
author: Nerya
---

# Backtest

Read assets with `Skill(skill="backtest", file="references/history-data.md")`; `file`
is relative to the selected Skill root. For another Skill, select its name in
`skill` and keep `file` relative to that root.

## Delivery contract

The installed host enforces the minimum evidence in
`references/delivery-contract.json`. Missing required receipts remain
incomplete/partial, including when the user prohibits the corresponding work.
Before the first `delivery_start` for this command, load that JSON for the
canonical workflow, check and artifact IDs. It defines the minimum contract,
not the stage layout. Start a standalone replay once with
`skill_id:"backtest"`, `workflow_id:"strategy_replay"`, the admitted scope
summary, and these valid stages (each check list is a JSON array):
`{"id":"preflight","state":"pending","required_checks":["source_binding","request_scope_match"]}`,
`{"id":"coverage","state":"pending","required_checks":["data_coverage","historical_data"]}`,
`{"id":"replay","state":"pending","required_checks":["historical_run","replay_source","execution_integrity"]}`,
`{"id":"readback","state":"pending","required_checks":["report_output"],"required_artifacts":["report","run_receipt"]}`,
and `{"id":"deliver","state":"pending"}`. Each quoted value is a
separate ID; never use slash shorthand such as
`data_coverage/historical_data`.

Reuse an existing authoring delivery for the current command. If the current
command has no delivery, call `delivery_start` before validation/replay producers.
Stages use `state`, never `status`; required checks and artifacts use the exact
arrays above, not model-authored guesses.

Producers persist `verification.checks` automatically; read those checks from the
returned delivery and never submit checks, status, or producer_owned fields in
`delivery_update`. Model-authored passing checks are rejected. Preserve
missing/failed behavioral tests when authoring;
successful replay does not certify those tests. Use the latest producer or
delivery `delivery_revision` as `expected_revision` on each update/finalize.
Update stages with `{"id":"replay","state":"completed"}` and artifacts with
`{"artifact_id":"report","kind":"backtest_report","ref":"<returned path>"}`.
Register run_receipt the same way. Use `kind/label`, not `type/description`.

After reading the producer receipt, call `delivery_finalize`. Claim completion
only when `ok:true` and `completion.allowed:true`; otherwise resolve the returned
reasons or report incomplete/partial work. For a completed lending-rate replay
with coverage gaps, follow the partial-delivery workflow in
`Skill(skill="strategy_author", file="references/rate-policy.md")` instead of repeating producers.
Stage required_artifacts lists IDs, not paths, for example
{"id":"readback","required_artifacts":["report","run_receipt"]}.
Register artifacts as {"artifact_id":"candidate","ref":"<source_binding.artifact_id>","revision":"<source_binding.revision>"},
{"artifact_id":"report","ref":"<producer-returned report_path>"} and
{"artifact_id":"run_receipt","ref":"<producer-returned run_receipt>"}.
Keep these artifact IDs aligned with the stage's `required_artifacts`.
Register only actual returned artifacts; omit checks and take current CAS from the
latest delivery_revision. The current `strategy_backtest` producer receipt is the
authority for replay evidence: when it returns `ok:true`, `run_receipt`,
`report_path` and `requested_window_complete:true`, use those exact returned refs
and the receipt's latest `delivery_revision`. Do not carry forward model prose or
a handwritten `coverage_missing` diagnostic as a required check or required artifact.
Retain it only as an optional, unverified historical diagnostic; preserve a coverage
blocker only when the current producer receipt reports a coverage gap. Keep the same
`report` artifact ID and replace its diagnostic ref with the producer-returned
`report_path`; adding another report ID leaves the required artifact misaligned.
File existence/readback or a model claim cannot create `report_output` or stand in
for a producer report. Missing producer evidence and prohibited work keep the
trusted completion floor unmet; report incomplete/partial without repeating
completed work. A negative economic FAIL is compatible with completed execution.
Completion is computed by `delivery_finalize`; never set completion manually or
send model-authored passing checks to force it.
Pending, unknown or stale effects retain their recovery path and never authorize
blind replay of a completed operation. For revision conflict, repeat the original
delivery_start to read existing state, then use its revision.

## Normal path: one native call

Use `strategy_backtest` with the exact candidate `proposal_id` (or `strategy_id`
for a saved promoted strategy). Choose `engine:"native"` for the strategy's SDK
entrypoint; `engine:"freeform"` only for an intentionally authored custom replay.
Omit `engine` (auto) when the package already owns the appropriate replay; auto
recognizes explicit params.rate_replay before supported custom scripts.
For lending-rate simulation load `Skill(skill="strategy_author", file="references/rate-policy.md")`.
For non-candle inputs or unavailable
history, load `references/replay-capabilities.md` before choosing another engine.
For a missing source or coverage segment, use
`Skill(skill="research", file="references/onchain-data-discovery.md")` to
find and acquire compatible history before declaring the replay blocked. A
missing adapter, empty cache or failed provider call is only a discovery
signal; it is not proof of absent historical data. Let the research workflow
try the compatible official, explorer/indexer and read-only producer paths
within the same candidate, delivery and requested window. Ask only at an
access, cost, permission or material scope boundary; otherwise prepare the
missing read-only integration handoff after the bounded search.
Keep `allow_mock:false` for real historical results.
Pass `expected_source_revision` from the latest successful
`strategy_validate.verification.revision`, including on the first replay. If a
producer reports a candidate/input mismatch, correct the diagnosed fields in
the SAME candidate, validate and submit it again, then make one corrected call
using its new revision. Keep the same candidate and admitted identity, UTC window
and provider. Apply only producer-returned parameter corrections within that
scope; do not introduce a different provider, identity or date range during
continuation. An unchanged failure stays blocked: never invent a hash or add
unrelated arguments to evade deduplication. A source revision conflict requires
validating the current source.

For a native continuation with an admitted request-scope contract, do not carry
historical start/end overrides from an old attempt, report, or model context.
Omit those recovery-only bounds and let the current native producer pin the
canonical window; its returned receipt owns the replay window evidence. Preserve
all actual user-requested strategy settings, including timeframe, warmup, risk,
capital, and the agent_replay flag. This rule does not amend an explicitly
admitted user window, and it does not apply to freeform/rates inputs whose saved
replay declaration owns their bounds. A policy_check_only result authorizes no
candidate, window, or historical run context; use it only as the current policy
check before the same-candidate native replay.

For script-gated Agent confirmation, load the Script-gated Agent decisions section
of `references/replay-capabilities.md`. With an operator-installed bounded model
authorization, use `settings.agent_replay:true` and read the returned signed
decision-replay receipt. An unavailable authorization is a concrete blocker,
not permission to replace model decisions with a fixed stub.

If the target `strategy.yml` already declares a `backtest:` block, **do not inspect
the file just to learn those values**. Call `strategy_backtest` with the exact
`proposal_id`/`strategy_id`; the tool reads candidate defaults internally. Do not use
`evolve_proposals`, `read_file`, `run_shell`, `cat`, `grep`, or memory to reconstruct
replay defaults. Normal verification keeps candidate-declared assumptions
authoritative. Only set `override_candidate_backtest_defaults:true` when the operator
explicitly asks to test a different assumption.

When the candidate does not already own those assumptions, pass user-requested
settings directly; a separate configuration file is not required:

```json
{"proposal_id":"<actual returned id>","engine":"native","allow_mock":false,
 "settings":{"window_days":365,"tf":"1h","warmup_bars":100}}
```

Use `settings` only when the ACTUALLY advertised tool schema includes it. A running
older server may not have loaded updated code: in that case use its supported
`config_path` field with the intended settings file, never send an ignored field
and silently fall back to the default period.

This call already resolves candidate replay defaults, performs config/source preflight, incremental history download
and reuse, strict coverage checks, source/data snapshots, bounded isolated replay
and report publication. Do NOT call separate preflight/download/inspect tools on
the normal success path. Progress arrives as ordinary tool progress, not additional
model turns. Preserve the user's capital, sizing, fees and position limits. Candidate
defaults are version-bound evidence assumptions; ordinary model-generated settings do
not override them. Explicit operator-requested alternate assumptions require the
override flag and are saved with the run.

Preflight rejects high-confidence future-data patterns such as negative
shift/diff/pct_change, backward filling/interpolation, centered rolling
windows and forward merge_asof. Dynamic period/alignment values, direct
external file reads, wall-clock access and replay randomness are warnings
because static analysis cannot prove their causality. The replay runtime itself
exposes only the closed historical prefix, exposes higher timeframes only after
their candle close, and fills strategy orders on the next bar open. A signal
on the final candle is rejected rather than filled at that same close.

For exact dates use inclusive `start_utc` and exclusive `end_utc`, both UTC. Do not
silently substitute a shorter period, different timeframe or cached old report.
For separate `historical_data` inspection/recovery, load
`references/history-data.md` and propagate the same admitted window using that
tool's `start`/`end` (or explicit `days`). Report coverage only after matching the
returned dates to the request; an omitted-window default is not that evidence.

When a read-only history producer rejects an otherwise admitted request with a
structured boundary or normalization diagnostic (for example, an interval
boundary, UTC alignment, or provider-required timestamp shape), repair only the
reported representation, keep the same market, protocol, timeframe and admitted
window, and retry that producer once. Read the next receipt before continuing;
do not turn a correctable request shape into a capability gap, and do not retry
an unchanged failure. If the producer still rejects the normalized request,
preserve its diagnostic and finish as partial/blocked according to the receipt.

## Outcomes and recovery

For continuing an incomplete LP replay, load `references/history-data.md` and use
the current state/interval inspector. A completed sampled run remains partial
evidence, not completion of the saved cadence. Coverage needs those missing states
and event bridges; changing sample spacing or window alignment cannot establish it.
When acquisition is prohibited, summarize the current gap and stop. A new requested
replay is evaluated by the current producer; infer neither permission nor a dedup
restriction from an old run's `completed` label.

- `completed`: inspect actual/requested dates, `requested_window_complete`, per-market
  coverage and execution counts. Economic WARN/FAIL is a completed calculation, not
  permission to change the strategy until profitable. The returned card opens the
  corresponding immutable report. No duplicate chart-publishing call is necessary.
  Trailing the buy-and-hold benchmark is a WARN/review condition, not a failed
  replay or failed strategy.
- `blocked`: inspect structured diagnostics. Fix syntax/config/SDK issues before
  rerunning. Incomplete data is not proof the exchange only has 30 days; inspect the
  download receipt. Repair/resume missing segments with `historical_data` when needed;
  keep the same candidate, period and risk rules. For a missing provider or an
  unsupported input surface, use `references/replay-capabilities.md` to repair
  the data/replay capability. Never retry an unchanged failure.
- `failed/cancelled`: retain `run_receipt`, `failure_path`, source snapshot and cached
  downloads. Do not present a partial report as a completed run. Cancelled execution
  does not authorize starting another worker without an active request.

For a blocked run, reply in two or three sentences: the current producer's reason,
what remains saved, and the necessary next action. Leave tool logs, JSON flags and
per-step receipt tables in the timeline/details. Historical model explanations are
not current diagnostics; when a gate stops before data inspection, report that gate
without inventing or repeating a historical data-cause explanation.

For download-only, inventory or explicitly offline requests, load
`references/history-data.md`. `data_mode:"local"` forbids automatic network retrieval.
For advanced settings load `references/config_schema.md`; that file belongs to THIS
skill, not strategy_author. Other details live in `references/extended-replay.md`.
Read only the reference matching the current need; do not reread loaded main pages.

## Read the result, not only the exit status

Writing a replay script is not running it. Require an actual `strategy_backtest`
result, then read its returned `run_receipt` and `report_path`. A claimed or
missing `replay_results.json` cannot establish a run. No invocation or no saved
producer receipt means replay not run/unverified, never completed coverage or
returns. A failed validation body remains failed despite a successful tool
envelope; repair it before replay. Report source execution, data coverage and
performance only to the extent established by their separate producer checks.

Read `verification.checks` when returned. Package checks, historical coverage,
source binding, replay execution, and research audits are separate. A passed
`bias_checks.static_temporal_scan` records only that the static scanner found no
blocking pattern; it does not prove causal correctness. Engine timing rules such
as closed-prefix inputs and next-bar fills describe the replay model, not an
independent future-data or lookahead audit. Preserve
`research_checks.dynamic_lookahead` (and any independent causality audit) as
`not_run`, `pending`, `blocked` or `failed` until that producer check actually
reports `passed`. Missing, pending or stale is not passed. New edits require
fresh checks; pending or unknown side effects must not be replayed as ordinary
failures. These receipts never authorize live trading and are not proof of
profitability, no leakage or out-of-sample validity.

`ctx.result.ok()` and an action label are not fills. Compare attempts, queued,
filled, rejected, SDK errors and forced closes. Use settled positions and the
recorded next-open execution model. Preserve stop-loss, take-profit, trailing
stops and time limits; unsupported intrabar/ATR/partial behavior stays explicit.
An Agent replay with `performance_evidence:false` does not establish historical
model decisions or trading returns. Warmup and no-trade curves are not alpha.

Preserve the candidate's percentage sizing and constraints; fractions differ
from report `_pct` fields. For capital allocation or flat curves, load
`Skill(skill="strategy_author", file="references/position-sizing.md")`.
For exact accounting, percent units, benchmark interpretation, protections and
Agent-mode limits, load `references/result-interpretation.md`.

Load deeper research methods only when requested: `references/causality-audit.md` addresses
future-data/repainting concerns; `references/research-validation.md` covers
independent windows, walk-forward, cost stress and ablations. Keep source/data
hashes, baseline and trial history. Never tune against a sealed test, erase
negative outcomes, or invent a passed check from unsupported capabilities.
For factor reuse load `factor_library` with the exact run ID and pinned versions.
