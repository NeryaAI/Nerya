---
name: backtest
description: "Replay Nerya strategy packages or in-flight strategy proposals over historical candles before promotion."
version: 0.2.0
license: MIT
author: Nerya
---

# Backtest

Use after a strategy validates and before promotion, or when the user
asks whether a strategy would have worked historically.

## Extended replay workflow

This reference is on-demand. The root SKILL.md owns the normal one-call path;
separate data preparation is optional, not required before every replay.

For native replay, first load `references/history-data.md`. Prepare the explicit
replay config, run `strategy_backtest(preflight_only=true, engine="native", ...)`,
and resolve blockers before a long download. Use `historical_data` to download
and inspect reusable local candles, then run the SAME candidate/config with
`data_mode:"local"`. Strict coverage is the default; a missing year is not a
successful one-month substitute. Download deadlines preserve completed segments
and report actual gaps, not a claimed exchange history limit.

Use the native `strategy_backtest` tool, not shell/Skill-script wrappers. For a
candidate pass its exact `proposal_id`; otherwise pass `strategy_id`. Keep
`allow_mock:false` for historical evidence. Use `engine:"native"` for the saved
strategy entrypoint, `engine:"freeform"` for its custom research script, or
`engine:"auto"` to preserve package discovery. A leftover custom script must not
force a standard strategy through missing third-party dependencies.

Before choosing settings, load
`Skill(skill="backtest", file="references/config_schema.md")`. Write a supported
workspace-relative config and PASS `config_path` to the native engine. Freeform
scripts have their own configuration; do not silently ignore the requested
dates/timeframe by mixing the two lanes.

When the operator names a non-default duration, timeframe, warmup, capital,
cost model or position limit, prepare that config BEFORE the first replay and
pass its exact `config_path` on the first `strategy_backtest` call. A request
for one year must not silently run the default six-month preset first.

## Match the evidence to the execution mode

| Strategy | Native historical replay proves | Separate execution check |
| --- | --- | --- |
| Pure script | Closed-bar signals, simulated fills, fees and final portfolio | One isolated paper tick through the SDK |
| Script-gated Agent | Gate/skip/dedupe, selected inputs and roles, dispatch counts | Execute a qualifying task through the real Agent runtime; a stopped branch must make no model call |
| Finite event Agent | Historical candle-close events and task input collection | Route one event through TriggerRuntime; replaying a task is not executing its model |
| Continuous listener or non-candle events | Not a standard candle tick | Bounded listener tests or a documented historical event replay |

Native Agent replay uses the SAME `build_agent_task`/configured entrypoint and
input/role validation as runtime. It does not call a current model on every old
bar. `replay.agent_execution:"not_run"` and `performance_evidence:false` mean
dispatch/observation evidence ONLY: report trigger, skip, error and input counts,
never treat a flat portfolio as zero-return trading performance. A full Agent
trading backtest requires historically bounded model decisions and a documented
execution model; neither task counts nor today's model answer supplies that.

Native replay sees a bar at its CLOSE, never a complete higher-timeframe bar
before that bar closes. Orders settle at the next bar's opening. The reported
end-of-data close assumption is explicit. `equity.csv`, `metrics.json` and the
report curve share the same final valuation, including forced-close fees.

## Diagnose once, repair the cause

`ctx.result.ok()` means only that strategy code returned normally. It does not
submit orders. Conversely, a real `ctx.trading.open_position(...)` call queues
an intent even when `run()` ultimately returns `ok` or `hold`; the return value
is NOT the unique order channel. A queued receipt has `status:submitted`, not
`filled`. Read the next tick's `ctx.portfolio` for settled positions.

For zero trades inspect `replay.order_attempts`, `orders_submitted`,
`orders_filled`, `orders_rejected`, `sdk_errors`, and `rejection_reasons`, then
`order_events.csv`, `rejected_signals.csv`, and `decisions.csv`. Do not equate
`status_counts.ok` or an action label such as `open_long` with execution.
Caught SDK exceptions remain recorded attempts. Preserve the original result
and create a new run after repairing the actual branch or submission error.

The configured universe `ctx.config.markets` remains the FULL list. The current
candle event's market is `ctx.trigger.get("market")`, not `markets[0]`.
Scheduled portfolio ticks may have no market: explicitly iterate the universe
and deduplicate by each market's CLOSED candle. Do not run that full-universe
loop once per market callback. `max_open_trades` is a portfolio-wide concurrent
position cap; a cap rejection cannot erase an SDK attempt. Never increase it
merely to manufacture trades. Record and explain any explicit override.

`backtest_dependency_missing`: inspect `missing_module` and the custom script.
Do NOT rerun unchanged, guess host virtualenv paths, install packages by a
workspace-escaping shell command, or delete the candidate. Native replay is an
alternative only when the saved entrypoint implements the same requested logic.
Otherwise repair the custom script/dependency through the authorized lane.

Read proposal files before native `edit_file`; a stale-read rejection requires
another read, not a shell workaround. Do not ask the strategy_author Skill for
backtest references. Missing history and unsupported execution surfaces remain
explicit blocked results, not fabricated successful charts.

Live protection executors are intentionally unavailable in standard OHLCV
replay. If `open_position(... protection=...)` is rejected, do NOT merely delete
the protection so the replay turns green. Preserve the intended stop/take-profit
semantics with explicit historically replayable close logic (using only data
available at that bar), or leave the replay blocked and explain the unsupported
surface. Only pass `protection=None` in replay when equivalent exit semantics
are actually represented by the strategy loop.

## Cards and completion

Market details are keyed by the exact proposal, strategy and backtest timestamp.
Keep each venue-qualified market in its own historical candle series, and keep
simultaneous fills as separate markers. Never substitute today's candles when
a saved run lacks market evidence. CSV rows and chart markers share trade IDs.

GBS has no implicit buy/sell mapping. When a strategy actually emits a GBS
signal, record it with the supported SDK surface
`ctx.trading.signal(market=..., signal_kind="gbs", confidence=...,
reasoning_ref=..., payload={"price": ..., "position": "aboveBar"})`.
Use the strategy's own documented GBS meaning, never invent one from the name.
The native replay retains these events in `signals.csv`; GBS circles represent
signals, not fills. Missing GBS records must remain missing, not be reconstructed
from order sides or a narrative description. Opening a detail tab is UI-only and
does not approve, promote or start the strategy.

The native tool publishes the strategy/proposal ID, immutable backtest timestamp,
verdict, evaluation mode, provenance, coverage and artifact locators. These are
the chat card contract. Inspect the actual returned paths and report; do not
write a second summary JSON, forge chart blocks or move artifacts just to make a
card appear. A failed attempt produces a diagnostic card, not PnL. A completed
observation produces an activity report, not a profit chart. A trading verdict
FAIL is still a completed calculation, not a tool failure or permission to
change the user's strategy until it becomes profitable.

When comparing strategy return with buy-and-hold, use
`alpha_vs_benchmark_pct` as the authoritative direction. Positive alpha means
the strategy OUTPERFORMED the benchmark even when both returns are negative;
negative alpha means it UNDERPERFORMED. Reuse the deterministic
`operator_summary.benchmark_comparison` wording when present and never write a
comparison that contradicts the reported alpha.

Keep proposals and schedules inactive outside an explicitly isolated test
workspace. Do not modify the Agent Loop to enforce this workflow.

IF the strategy package contains `backtests/research_backtest.py`,
`backtests/freeform_backtest.py`, or another supported freeform backtest script:
RUN `strategy_backtest` as usual. LOAD `references/full-playbook.md` for
freeform evidence rules.

IF real OHLCV candles exist but the loaded window is short:
Report available real history and coverage diagnostics. Preserve the requested
window and strict policy; do not present partial coverage as a completed requested
validation. An explicit partial-window study has lower confidence.

IF normal OHLCV replay is impossible because code or an SDK surface fails:
Inspect the specific contract and preserve the intended strategy. Repair the
candidate or adapter as appropriate, then validate and replay the SAME candidate.

IF normal OHLCV replay is impossible because the market is not honestly
replayable as candles:
LOAD `references/custom_replay_template.md` and
`references/full-playbook.md`. Build an honest custom replay from durable
historical data with explicit limitations.

READ `report.md`, `metrics.json`, and chart artifacts.
REPORT verdict, risks, and whether promotion is blocked.
When summarising tool output, use `operator_summary_text`,
`operator_summary`, or `metrics_display` when present.

Never present random, synthetic, generated, or placeholder price series as a
successful backtest. When `allow_mock=false`, a backtest is only acceptable if
the data source is real historical market/event data. If that data is not
available, say the backtest is blocked instead of fabricating candles. A
standard-backtest waiver is an operator approval record, not performance
evidence.

## Scripts

- `scripts/backtest_run.py` for strategy/proposal replay.
- `scripts/freeform_run.py` for strategy-local SDK/freeform replay.
- `scripts/render_chart.py` when only chart rendering is needed.

## Lazy References

- `references/full-playbook.md` for the original detailed playbook.
- `references/config_schema.md` and `references/config.default.yml`.
- `references/metrics_glossary.md`.
- `references/chart_schema.md`.
- `references/custom_replay_template.md`.
