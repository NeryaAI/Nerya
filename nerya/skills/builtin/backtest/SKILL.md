---
name: backtest
description: "Run or continue Nerya historical backtests, research accurate data and compare multiple periods, including sparse on-chain/LP history; deliver attributable results and limitations without activating trading."
version: 0.8.0
license: MIT
author: Nerya
---

# Backtest

On continuation, use saved progress and the producer's diagnosed next action.
Read the named current inputs and perform that action; reuse the existing
inventory instead of reopening every prior report or rediscovering the replay
implementation. Investigate further when a new result changes the diagnosis.

Before continuing LP replay, select the model for the current user study. A saved
`cadence` default or old `cadence_data_incomplete` failure does not require full
interval acquisition. For user-permitted sampled/custom research, load
`Skill(skill="strategy_author", file="references/lp-sampled-research.md")`,
section 3, and adjust the same candidate before replay. A saved-data-only request
forbids new history/interval acquisition jobs; compare supported saved periods
and disclose the model and gaps. For explicitly requested cadence, retain its
stronger input requirements and report missing inputs under that restriction.

## Research and compare

1. **Keep the strategy.** Reuse the actual `proposal_id` or `strategy_id` and
   preserve explicit user requirements: strategy rules, assets, chain, venue/pool,
   dates, timeframe, sizing and risk limits. Candidate defaults are starting
   assumptions, not an immutable research scope. Record Agent-selected assumptions
   and distinguish them from explicit requirements.
2. **Find accurate history.** Prefer existing usable history and saved producer
   datasets before acquiring more. Verify the exact instrument or pool, ordered
   token contracts, quote direction, timestamps, source, observed fields and gaps.
   Use `references/history-data.md` for inventory/reuse and
   `Skill(skill="research", file="references/onchain-data-discovery.md")` when
   source discovery is needed. A cache miss or one provider error does not prove
   history is unavailable. Continue public read-only research within existing
   permissions; ask only for genuinely missing access, paid resources or a material
   change to the user's requirements.
3. **Choose useful periods.** Compare multiple supported lookback windows,
   resolutions or market regimes and explain their selection; no fixed period
   list is required. Preserve any explicitly requested dates and strategy
   timeframe. Where data is sparse, use available windows, supported sampling or
   a strategy-local custom simulation with stated scope and assumptions. Keep an
   unmet requested window visible alongside supplementary studies. Do not spend
   hours collecting every LP cron state and event bridge by default; select full
   lifecycle reconstruction only when the question actually needs it.
4. **Run and deliver evidence.** Execute the saved strategy through the suitable
   replay engine, compare results across periods with costs, coverage and model
   limits, and save the actual reports. Coverage gaps constrain interpretation;
   they do not by themselves prevent a useful partial calculation or delivery.
   Report unavailable periods and negative/no-trade results as well as successes.
   Never invent observations, complete coverage, fills or verified returns.

## Invoke the existing tools

Validate the saved candidate with `strategy_validate`; repair concrete source or
SDK errors in the same candidate. Use `strategy_backtest` with its actual ID.
Omit `engine` for auto selection when the package already owns the replay;
`native` runs the candle SDK entrypoint, `rates` the supported rate-policy replay,
and `freeform` a saved strategy-local research script. Engine choice follows the
inputs and strategy, not a more favorable result.

For native replay, pass supported `settings` or an existing `config_path`; a
separate file is unnecessary for ordinary settings. For example:

```json
{"proposal_id":"<actual returned id>","engine":"native","allow_mock":false,
 "settings":{"window_days":365,"tf":"1h","warmup_bars":100,"coverage_policy":"allow_partial"}}
```

Choose actual periods and lookback needs for the task rather than copying the
example. Use `coverage_policy:"allow_partial"` for research with gaps and
disclose them; evaluate a study's full-coverage requirement in the report rather
than blocking every calculation. Use only fields
in the advertised tool schema. Explicit dates use inclusive `start_utc` and
exclusive `end_utc`, both UTC. Record requested and actual bounds for every run.
`data_mode:"local"` forbids network retrieval. Keep `allow_mock:false` for real
historical results; label synthetic stress/behavior scenarios separately.

Normal replay owns preflight, history preparation/reuse, isolated execution and
reports; no separate inspect/download tour is needed on success. For custom
replay, use `strategy_backtest(proposal_id="<actual id>", engine="freeform",
allow_mock=false)`; native `settings`, `data_mode`, `config_path` and
`preflight_only` are not freeform inputs. Save custom assumptions in the package.
Generic freeform research need not match the host LP template or its full-state
data protocol; host-verified LP accounting is an optional stronger evidence path.
Load `references/replay-capabilities.md` for engine/input choices and model
authorization, or `references/custom_replay_template.md` for supported script
paths and the facade. Those interface details do not make full coverage or a
particular LP template a prerequisite for all research.

Reuse `strategy_validate.verification.revision` when supplying
`expected_source_revision`; after source changes, validate again and use the new
revision unchanged. Repair diagnosed input/config errors before retrying. A new
period is a distinct disclosed study, not a nonce for repeating a failed or
unconfirmed operation. Keep prior reports and receipts.

## Read results and close the delivery

Read the actual `strategy_backtest` result and returned `run_receipt` and
`report_path`. A written script, file path or `ok:true` envelope alone does not
establish replay execution or historical performance. Separate source execution,
observed coverage, simulated accounting and independently verified performance.
Use recorded timestamps and identify each compared run's own data source and
producer reference; omit unknown fields instead of filling them with placeholders.
Preserve `verification.checks`, including missing, failed, stale and `not_run`
checks; model-authored PASS fields cannot replace producer evidence.

After a cancelled or failed model replay, read its saved `decision_tape.json`
alongside the failure receipt: a run can contain confirmed decisions and an
unconfirmed later request. Report each separately. Before proposing a retry or
renewed authorization, follow the recovery section in `references/replay-capabilities.md`.

For a new replay delivery, load `references/delivery-contract.json` and use
`delivery_start(skill_id="backtest", workflow_id="strategy_replay", ...)`.
Reuse the current authoring delivery when present. Standalone replay stages may use
`preflight` with `required_checks:["source_binding"]`, `replay` with
`required_checks:["historical_run","replay_source"]`, and `readback` with
`required_checks:["report_output"]` and `required_artifacts:["report","run_receipt"]`.
Coverage, historical-data quality, execution integrity and scope matching are
optional diagnostics, not required stage checks. Register actual returned refs
with `delivery_update`, using the latest `delivery_revision` as
`expected_revision`. Stages use `state`; artifact records use `artifact_id`,
`kind` and `ref`. Producers persist checks; omit model-authored `checks` from updates.

Reconcile evidenced stages and call `delivery_finalize` with the latest revision.
Its `completion.allowed` owns workflow completion under the minimal contract;
it does not mean complete coverage or verified returns. Deliver real limited
research and saved reports with all gaps visible, even when stronger evidence is
unavailable. Do not rerun completed calculations or collect an entire LP lifecycle
solely to make delivery green. Economic FAIL can accompany completed execution.
For data-only inspection or saved-report explanation, return findings directly
without starting a new replay.

## Safety and evidence boundaries

Reconcile pending/unknown effects and a genuinely sent, unconfirmed model-request
ledger before retrying; an old request, fresh grant or changed window does not
authorize replaying that effect. Ordinary deterministic backtests need no model
grant. For actual model decisions, use the existing bounded authorization path in
`references/replay-capabilities.md`; missing model permission does not forbid
data research, authoring or independent deterministic checks.

Preserve sandbox isolation, closed historical prefixes, next-bar execution where
applicable, stops, fees and risk limits. Static temporal scans do not prove causal
correctness; fixtures and custom simulations do not establish verified returns.
`ctx.result.ok()` is not a fill. Report attempts/fills/rejections and forced closes
from real run evidence. Keep `performance_evidence:false` wherever returned.
Never tune against a sealed test or erase negative outcomes. Research/replay
does not promote a strategy, enable schedules, place account orders or bypass
Risk Gate, Approval Gate or live-trading permission.

Load only the needed reference: `references/config_schema.md` for native settings,
`references/result-interpretation.md` for accounting/benchmarks,
`references/causality-audit.md` for lookahead audits and
`references/research-validation.md` for walk-forward, cost stress and ablations.
Read assets with `Skill(skill="backtest", file="<relative reference path>")`.
