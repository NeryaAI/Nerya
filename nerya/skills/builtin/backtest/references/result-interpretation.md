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
Coverage fractions are different: `coverage_ratio: 1.0` means 100% coverage,
not 1%. Prefer the producer's coverage message and `requested_window_complete`;
the last-minus-first candle timestamp span excludes one interval and does not
by itself establish a gap in a verified exclusive-end window.
Positive alpha means outperformed even when both returns are negative. Prefer
`operator_summary.benchmark_comparison`. Historical data updates do not change an
old report: run the strategy again and link the NEW timestamp; retain old evidence.
GBS is only an explicitly recorded strategy signal, never inferred from buy/sell.
No replay or report view approves a strategy, activates a schedule or trades an account.

Explain exits only from the recorded trade/decision reason and implemented exit
rules. EMA cross exits are signal exits; a losing exit is not automatically a
stop loss. Claim stop-loss, take-profit or protective exits only when corresponding
protection and executed exit evidence exist. In particular, an EMA-only strategy
with zero protective closes cannot be described as "repeatedly stopped out".
Separate end-of-window forced closes from signal or protective exits. When exit
evidence is absent, say the cause is unverified instead of inventing an explanation.

Report execution completeness and economics separately: completed run, full history
coverage and clean accounting can coexist with a negative return and economic FAIL.
Neither that economic verdict nor a completed replay establishes that the candidate's
behavioral tests ran or passed; report those from their separate validation receipt.

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
