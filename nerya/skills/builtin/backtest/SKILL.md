---
name: backtest
description: "Run a saved Nerya strategy over reusable local historical data; diagnose errors and present version-bound results without activation."
version: 0.5.0
license: MIT
author: Nerya
---

# Backtest

## Normal path: one native call

Use `strategy_backtest` with the exact candidate `proposal_id` (or `strategy_id`
for a saved promoted strategy). Choose `engine:"native"` for the strategy's SDK
entrypoint; `engine:"freeform"` only for an intentionally authored custom replay.
Keep `allow_mock:false` for real historical results.

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

## Outcomes and recovery

- `completed`: inspect actual/requested dates, `requested_window_complete`, per-market
  coverage and execution counts. Economic WARN/FAIL is a completed calculation, not
  permission to change the strategy until profitable. The returned card opens the
  corresponding immutable report. No duplicate chart-publishing call is necessary.
- `blocked`: inspect structured diagnostics. Fix syntax/config/SDK issues before
  rerunning. Incomplete data is not proof the exchange only has 30 days; inspect the
  download receipt. Repair/resume missing segments with `historical_data` when needed;
  keep the same candidate, period and risk rules. Never retry an unchanged failure.
- `failed/cancelled`: retain `run_receipt`, `failure_path`, source snapshot and cached
  downloads. Do not present a partial report as a completed run. Cancelled execution
  does not authorize starting another worker without an active request.

For download-only, inventory or explicitly offline requests, load
`references/history-data.md`. `data_mode:"local"` forbids automatic network retrieval.
For advanced settings load `references/config_schema.md`; that file belongs to THIS
skill, not strategy_author. Other details live in `references/extended-replay.md`.
Read only the reference matching the current need; do not reread loaded main pages.

## Execution and data truth

`ctx.result.ok()` means the script returned normally, not a fill. A real SDK
`open_position` queues an order even if run later returns hold. Compare attempts,
queued, filled, rejected, SDK errors and forced closes; action labels are not orders.
Use settled portfolio positions. Multi-market callbacks use `ctx.trigger.market`,
not the first configured symbol; a capacity rejection cannot erase an attempt.

Only closed candles are visible and fills use the recorded next-open assumption.
Warmup is not performance. Do not remove `protection` to make replay pass: preserve
the risk rules in a documented historical execution model. Bar-close protection is
not identical to exchange intrabar execution. Unsupported behavior stays explicit.

The native engine simulates `open_position(protection=...)` for percentage or
absolute-price stop loss/take profit, percentage trailing stops and time limits.
Pass the same supported SDK rules unchanged: do not delete them or write a second
exit algorithm just for replay. Protection fills are counted separately from SDK
submissions. The report records OHLC timing, gap handling, stop-first collisions
and prior-bar trailing watermarks. ATR-relative, partial-exit and bid/ask-triggered
rules still need a supported explicit model; never silently drop those controls.

Script replay can report simulated trade performance. Agent replay with
`replay.agent_execution:"not_run"` proves gates, inputs and dispatches only, never
historical model decisions or trading returns. Use `performance_evidence:false` as
the boundary; do not turn a flat Agent replay into a zero-return trading result.

Percent fields already contain percentage points: 0.15 means 0.15%, not 15%.
Positive alpha means outperformed even when both returns are negative. Prefer
`operator_summary.benchmark_comparison`. Historical data updates do not change an
old report: run the strategy again and link the NEW timestamp; retain old evidence.
GBS is only an explicitly recorded strategy signal, never inferred from buy/sell.
No replay or report view approves a strategy, activates a schedule or trades an account.

## Percentage sizing and flat-curve review

Honor saved `params.sizing: {method: pct_nav, pct_nav: ...}`; fractions are
0..1, unlike report `_pct` fields. Percentage sizing follows current NAV, not
initial capital. `stake_amount.mode: unlimited` preserves SDK sizing;
`mode: fixed` overrides it and is only for an explicitly requested fixed-stake
study. Match `max_open_trades` to the candidate's intended concurrent slots.
Do not silently replace percentage sizing with 100U or override existing limits.

An almost-flat curve needs an explanation: compare real price movement,
filled notional/NAV, attempts/fills/rejections, fees, exposure time and benchmark
capture. `exposure_pct` is time/rows invested, NOT capital utilization. No fills
does not establish trading performance; Agent execution not_run is not 0% return.
Use recorded evidence, not fabricated utilization fields or extra automatic
replays. For low participation, capital allocation or authorized optimization,
load `Skill(skill="strategy_author", file="references/position-sizing.md")`.
Preserve negative results and unchanged benchmark scaling; do not manufacture
activity or tune a strategy merely to make its chart attractive.

## Research review, only when requested

An ordinary replay remains **one native call**, not an automatic audit campaign.
Read `bias_checks`, `research_checks` and recorded execution-model limits in the
returned receipt. Static scan passed means no blocking pattern was found, NOT
proof of no leakage; warnings remain unresolved. A completed replay, positive
return or economic PASS does not establish out-of-sample validity.

For future-data/repainting concerns or recursive-indicator initialization, load
`references/causality-audit.md`. For robustness, independent test windows,
walk-forward, cost stress, component ablation or market-regime review, load
`references/research-validation.md`. Read only the needed reference.

Preserve the original baseline and bind every comparison to source/data hashes,
run IDs, market profile and assumptions. One hypothesis per experiment; retain
negative results. Do not tune until profitable, silently change risk rules, or
present an inspected holdout as locked. These references describe methodology,
not new tool actions. Check actual tool capabilities before running anything;
unsupported checks stay not_run/blocked with a reason, never become a fabricated
pass. UI review actions prepare a scoped draft, not an automatic batch or trade.

## Reusable factors

When asked to extract or reuse research components, load `factor_library`.
Pass this run's exact strategy_id/backtest_ts (as ts) and proposal_id to its
extract action. It reads frozen source and keeps extracted candidates separate
from the factors declared in the run. New strategies may include exported,
version-pinned factors.json; native replay freezes those references alongside
source. Total strategy profit is not proof of an individual factor's value.
