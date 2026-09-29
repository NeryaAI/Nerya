---
name: strategy_author
description: "Create, edit, debug and validate Nerya script, script-gated Agent and event Agent strategies. Use the main conversation; finish the requested historical replay without activating trading."
version: 0.18.0
license: MIT
author: Nerya
---

# Strategy Author

Create and verify the user's strategy in this conversation. Preserve the requested
rules, markets, timeframe, history period and risk limits. Never promote, activate
schedules or place account orders unless separately requested. Historical simulated
orders are not account orders; a no-live-order request does not forbid backtesting.

A ban on submission means authored draft only: do not submit or promote it.
A ban on running/promotion/trading does not ban implementation or explicitly
requested isolated tests. “No orders” is a runtime restriction, NOT a request for
an empty scaffold. Honor a separate explicit no-test instruction. Keep requested
deliverables pending until actual receipts exist.

## Default path — finish work, not a tour of every reference

For factor-based strategies or explicit research reuse, load `factor_library`,
search once for relevant definitions and export the selected exact versions.
Include the returned factors.json in the strategy bundle and calculate from
closed candles using nerya.sdk.factors.calculate_factor. Do not substitute a
similar factor for user-specified rules, inherit validation across markets, or
resolve latest during replay. Ordinary strategies need no empty factor file.

1. **Resolve scope once.** Reuse supplied markets and candidate IDs. For a requested
   crypto market-cap ranking use `market_data(action="ranked_universe", venue="binance",
   count=N, rank_by="market_cap", quote="USDT")`; keep its observed date and tradable
   exclusions. Do not launch additional ranking searches after a successful result.
   When `venue_mapping_complete:true`, use `market_ids` directly: do not call
   `list_symbols`, web search/fetch, or another ranking source to revalidate them.
   For a clearly fresh creation request with no candidate ID, do not grep/list the
   workspace or recall memory just to hunt for an older strategy with the same name.
   Check current candidates before following an ID recalled from old history.
2. **Implement the strategy.** Settle its actual rules, parameters, inputs and
   execution tests first; then author its separate review plan in step 3. When the
   request is sufficiently specified, prefer saving the completed bundle in one
   `strategy_draft_proposal(create_tuning=true, files={"main.py":..., "strategy.yml":..., "strategy.md":..., "subagents/strategy_tuner.agent.md":..., "tests/test_contract.py":...})`
   call (use only fields present in the tool schema).
   The tool saves and validates that actual bundle. Keep the returned ID; a successful
   inline validation needs no duplicate read/edit/validate cycle. For an existing
   candidate or intentionally incomplete scaffold, use `read_file` then
   `edit_file` / `write_file` on the exact returned paths. A scaffold is not an
   implemented named strategy. Do not silently replace AlphaTrend or another named
   algorithm with a generic moving-average template. If named-algorithm semantics are
   genuinely needed, use at most one authoritative search plus one fetch, then stop
   researching once the definition is sufficient. Use an existing paper account;
   call `account_list` at most once per turn and reuse that result. Never repeat it
   merely to reconfirm the same selected account. Choose one schedule form (cron OR
   interval), disabled. Pure script execution means `execution_mode:script`,
   `agent_task.enabled:false`, and no model calls in trading ticks; its separate
   review plan does not turn the trading script into an Agent strategy.
3. **Build the strategy-specific review plan.** Load
   `Skill(skill="strategy_author", file="templates/review-plan.md")` after settling
   the strategy, and adapt it to THIS strategy rather than copying stock prose.
   Save `strategy.yml::tuning` and its actual `subagents/strategy_tuner.agent.md`
   together in the SAME candidate; do not create a second trading strategy or a
   separate task. Supply these files in step 2's complete bundle, or edit the exact
   existing proposal paths. The separate review workflow is **review scheduler →
   evidence script → review Agent (Proposer)**. Include independent timing/timezone,
   lookback, evidence sufficiency, strategy-specific checks, permitted changes and
   validation/hold conditions. Keep `tuning.schedule.enabled:false` as well as the
   trading schedule disabled. Normal creation uses `tuning.enabled:true` to define
   the review capability, not to start it. An explicit no-AI/no-review requirement
   covering the whole strategy uses `create_tuning:false`, `tuning.enabled:false`
   and a documented opt-out instead; never silently widen an existing opt-out.
4. **Verify the saved version.** If the saved bundle was not already validated by
   the generation receipt, run `strategy_validate` after the final edit,
   then `strategy_submit_proposal` for the SAME candidate. A normal
   creation request includes verification, not activation. Honor explicit draft-only
   or no-test restrictions. Do not repeatedly validate unchanged files.
5. **Run the actual replay once.** Load `Skill(skill="backtest")` when its contract
   is not already present. `strategy_backtest` owns preflight, reusable local history,
   coverage checks, isolated replay and reports. If the candidate already declares
   `strategy.yml.backtest`, call `strategy_backtest` directly with its `proposal_id`;
   the tool reads those defaults itself. Do NOT inspect/cat/read the candidate merely
   to rediscover its replay settings. For a candidate without replay defaults, pass
   the intended user-requested `settings` directly, e.g.
   `{"window_days":365,"tf":"1h","warmup_bars":100}`.
   Use this field only if present in the actual tool schema; older runtimes and
   advanced existing settings use `config_path`. Do NOT orchestrate extra preflight,
   inspect, download and local-replay calls unless preparation fails or the user
   specifically requested download-only/offline operation. Keep strict coverage;
   never shorten the year, change timeframe or remove stops to get a green result.
6. **Inspect the receipt and finish.** Report actual dates, requested dates, markets,
   coverage, execution counts and research verdict. The tool automatically supplies
   the strategy/backtest card. Do not publish duplicate market/research charts merely
   to make a backtest card appear. A poor return is not a tool failure or permission
   to run an unrequested optimization loop. Repair a concrete error in the SAME
   candidate; do not retry unchanged input or recreate the strategy. If those fixes
   changed strategy behavior, update the review plan in the same candidate before
   final validation. Completion includes the saved review plan (or explicit opt-out),
   its independent schedule and disabled/active state, not a promise to add it later.

No housekeeping, deleted-test cleanup, environment probes or unrelated Git work is
part of this path. Existing loaded references remain usable: reread only when a
missing section, changed file, or actual error requires it. When passing `file`,
use `Skill(skill=..., file=...)`, not guessed host paths or tool names.

For a requested fixed order size, make `policy.max_single_order_usd` consistent
with `params.sizing.fixed_usd`; do not retain a scaffold's smaller hard-coded cap.
Persist `schedule: {type: cron, cron: '0 */4 * * *', enabled: false}` for a disabled
4h schedule (or the actual requested schedule). Root `schedule_enabled` is not
a runtime setting. Explicit daily risk limits remain authoritative and must not
be relaxed to improve a result. If no daily limit was requested, use 0 (uncapped
at this strategy layer, not a bypass of account-level limits), not a guessed cap.

## Default sizing and participation

For NEW trading strategies with no explicit sizing request, use **percentage of
current account NAV**, not a fixed 50/100/1000 USD order. Persist
`params.sizing: {method: pct_nav, pct_nav: 0.90}` for one position slot and pass
`ctx.config.params["sizing"]` unchanged to `open_position`. `0.90` means 90%,
NOT 0.9% and NOT 90. With K simultaneous slots, allocate `0.90 / K` per slot;
normally K = min(3, number of markets), and bind `policy.max_open_positions` and
`backtest.max_open_trades` to the same K. A ranked winner-only strategy uses K=1.
This is a 90% deployment budget with 10% headroom, not a promise to stay invested
or permission to use the same 90% on every asset. NAV is not free cash: existing
positions, other strategies, reservations, fees and account limits still apply.

Never inherit a template's 100 USD single-order or 1000 USD daily cap. When the
user supplied neither, set those NEW strategy-layer dollar caps to 0; do not
change existing strategy/account limits. Use `backtest.stake_amount.mode: unlimited`
to respect SDK sizing (it does NOT mean unlimited funds or leverage). Explicit
fixed sizes, lower risk budgets, observation-only rules and existing strategy
settings take precedence. Do not rescale an existing strategy without a request.

For an open-ended creation request, favor an **active, capital-efficient** design:
one meaningful entry signal, only necessary independent filters, a documented
exit and a re-entry rule. Avoid stacking arbitrary RSI/volume/trend/confidence
vetoes or tiny fixed take-profits that leave a trend strategy mostly in cash.
Keep named/user-specified algorithms intact. More exposure is not more alpha;
do not force trades, remove stops, add leverage or martingale, or tune until a
curve looks good. A normal creation still runs one verification replay.

For multi-asset allocation, Agent order fields, explicit risk budgets, weak
participation or an almost-flat curve, load `references/position-sizing.md`.
The final receipt review must distinguish no signals, rejected orders, small
notional, missing Agent execution and genuinely flat market returns.

## SDK essentials for finite script strategies

This section is the installed SDK contract for ordinary script authoring. Do not
use shell, Python inspection, repository globbing or installation paths to rediscover
these signatures. The application source directory is not the user workspace.
Read another reference only for a genuinely missing capability, not to confirm
the calls already documented here. Write operator-facing progress in the user's language.

A model already awakened to decide whether AI should run is NOT a script gate.

Use `from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask`.
For AlphaTrend use the SDK's tested pure function instead of inventing the
indicator again: `from nerya.strategies.indicators import alphatrend` and
`signal = alphatrend(candles, period=14, coefficient=1.0, offset=2)` (use requested
parameter values). `signal['alpha']` and `signal['lagged']` are aligned arrays;
`signal['buy'][-1]` / `signal['sell'][-1]` are crosses against the SAME line delayed
by two candles, NOT upper/lower price channels. ATR smoothing defaults to SMA of
true range; MFI is preferred and missing volume uses RSI. A spot strategy is
long/flat unless margin/short selling is explicitly requested. On a sell signal
close an existing long, never silently create a leveraged short position.

For a standard long/flat indicator strategy, use the shared finite execution
component. Do not handwrite timestamps, state compatibility branches, duplicate
MFI/RSI formulas, multi-market routing or crossover math that the SDK already owns:

```python
from nerya.strategies.indicators import alphatrend
from nerya.strategies.signal_runner import run_signal_strategy

def run(ctx):
    return run_signal_strategy(ctx, alphatrend)
```

Set the user's values in strategy.yml: `timeframe`, `markets`, `policy`, and
`params.indicator` (e.g. `{period:14, coefficient:1.0, offset:2}`), `params.lookback`,
`params.sizing`, `params.protection`, `params.confidence`. The component reads those
parameters unchanged and handles closed candles, market routing, dedupe, settled
positions and SDK entry/exit. AlphaTrend's MFI/RSI already selects the recurrence;
do NOT add another MFI>50 entry filter or redefine MFI unless explicitly requested.
This is reusable SDK execution, not an optimizer or an Agent Loop. Keep the normal
version-1 annotation comments on the small main.py and include tests of the saved
parameters, library signal invocation and entry/exit behavior.

SDK candles use `ts` (seconds) and explicit `ts_ms`, `timestamp_ms`, `open_time_ms`
aliases (milliseconds); `close_time_ms` is the close boundary. Never replace an
unknown timestamp with 0 and then use it as a dedupe key. `ctx.state.get/set` and
mapping access are equivalent, scoped state operations in runtime and replay.
The configured universe is `ctx.config.markets`; a candle callback is routed by
`ctx.trigger.get("market")`, NEVER by `ctx.config.markets[0]`. A scheduled callback
without a market may iterate all markets. Deduplicate CLOSED candles separately by
market/timeframe using `ctx.state`. No full-universe loop per single-market event.

Read `ctx.market.candles(market, timeframe=..., limit=...)`; candle rows are mappings.
Filter timestamps by the injected `ctx.clock.now_ms()`, preserve units and use only
completed candles. Return a real error for a failed data/state reader, not no-signal.
Positions are `ctx.portfolio.positions(market)`; use settled quantity/entry prices,
not an order submission receipt. `open_position(market=...,side="long",sizing={...},
confidence=...,reasoning_ref=...)` submits independently of `run()`'s return value.
An `ok` result or `open_long` label is not a fill. Keep SDK failures and rejections.
`ctx.result.error(message=..., kind="data_error")` is valid; `error(reason=...)` is not.

Expose tunable parameters in strategy.yml and actually read them. Explain the saved
parameters through the shared SDK: `ctx.config.params`, `ctx.config.timeframe`,
and `ctx.config.get(...)` work identically in runtime and replay; the older
`ctx.config.extras` mapping remains available. Do not catch AttributeError and
silently replace authored parameters with defaults. Review scheduling must remain
disabled until activation is requested; generating a review plan is part of creation,
not permission to optimize a losing backtest now. A loss is a valid research result.
Persist protection state until the settled position is actually flat, not merely
when a close request is submitted. Use entry/prior high-water state for historical
stops, never `price <= price - distance`, and never relabel a generic EMA as a named indicator.
Standard percentage/price stop-loss and take-profit rules, percentage trailing
stops and time limits can be passed unchanged to `open_position(protection=...)`;
the native replay has a recorded historical execution model for these rules.

### Concrete call shapes (examples, not strategy signals or parameter defaults)

```python
params = ctx.config.params                  # exact strategy.yml params
timeframe = ctx.config.timeframe            # exact strategy.yml timeframe
market = ctx.trigger.get("market")          # routed candle event
markets = (market,) if market else ctx.config.markets  # timer: all configured markets
bars = ctx.market.candles(market, timeframe=timeframe, limit=200)
position = ctx.portfolio.position(market)   # None, or settled size/avg_price
indicators = ctx.market.features(market, timeframe=timeframe, lookback=200)
entry = ctx.trading.open_position(
    market=market, side="long", sizing=ctx.config.params["sizing"],
    protection={"stop_loss": {"type": "pct", "value": 0.02},
                "take_profit": {"type": "pct", "value": 0.05}},
    confidence=0.8, reasoning_ref="the actual entry signal",
)
exit_receipt = ctx.trading.close_position(
    market=market, side="long", confidence=0.8, reasoning_ref="the actual exit signal")
```

Only call the entry/exit example in the corresponding actual signal branch.
No entry/exit is implied by loading this Skill. Use the user's values, not example
values. Named indicator calculations can be local pure-Python functions in main.py;
no provider imports, source inspection or installed indicator library is required.

Explain the saved
logic with concise `@nerya.version`, `title`, `description`, `logic`, `rationale`,
`scope`, `input`, `output`, `risk`, `validation` and `step` comments in main.py.
Comments describe actual behavior, not unsupported claims. Minimal tests exercise
entry, hold, exit and duplicate paths, not only import or permissive assertions.
`@nerya.version 1` is the annotation protocol version, NOT an edit counter. Do not
increment it when revising a strategy. Read every existing generated file before
overwriting it (including strategy.md and tests), or provide complete files in the
initial generation call to avoid a redundant scaffold/read/rewrite cycle.

Minimal complete annotation syntax for the SDK-delegating main.py (change prose
to describe actual configuration; `execute` is the stable step ID, not a number):

```python
# @nerya.version 1
# @nerya.title Script signal strategy
# @nerya.description Execute the configured indicator through the shared SDK.
# @nerya.logic Evaluate closed candles, then submit the configured entry or exit.
# @nerya.rationale Reuse tested signal and execution components without optimizing parameters.
# @nerya.scope Configured markets and timeframe only; no account configuration changes.
# @nerya.input Historical or live closed candles, declared parameters and settled positions.
# @nerya.output Trade submission receipts or explicit hold/error outcomes.
# @nerya.risk Historical OHLC assumptions do not guarantee live execution or profitability.
# @nerya.validation Verify this saved source and its requested historical window.
# @nerya.step execute | Execute strategy | Route closed candles through the shared SDK.
```

## Load only the reference matching this strategy

- Creation/edit completion: `templates/review-plan.md` for the separately authored
  strategy review plan. Every review invocation uses `references/review.md` to
  adapt its checks to that version and run's evidence without changing the strategy
  or future schedule automatically.
- Script-gated/scheduled/finite event Agent: `references/workflows.md` and the
  relevant `references/script-control.md` section. A Python gate runs BEFORE AI;
  skipped branches do not call a model. Native Agent replay checks dispatch/inputs,
  not historical model trading performance (`agent_execution:not_run`).
- Actual persistent/WebSocket listener: `references/continuous.md`; do not place an
  infinite listener in a cron tick. Finite candle events are not continuous mode.
- Detailed SDK, protection, exotic instruments: `references/specialized-contracts.md`.
  Never delete live `protection` without an explicitly implemented historical exit
  model. Bar-close exits are not equivalent to exchange intrabar stop execution.
- Detailed source/review annotations: `references/explanations.md`; only when the
  compact annotation contract above is insufficient.
- Advanced verification: `references/verification.md`; data-only/offline requests:
  `Skill(skill="backtest", file="references/history-data.md")`.
- Other edge cases: `references/authoring-contract.md`. It is not a mandatory read.
- Requested research charts (not duplicate backtest cards):
  `Skill(skill="research", file="references/visual-deliverables.md")`.

An observer declares `evaluation.mode:observation`, real input sources and
`policy.allow_direct_order:false`. `allowed_tools:[]` means no tools, not market-only
tools. Keep the Agent role editable. Requested input `outputs` must already be
published, not the future model answer. Keep historical observations separate from
current analysis, and fees/percentages/source limitations visible in the final answer.
