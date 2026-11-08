## SDK essentials for finite script strategies

The candle/spot examples below do not implement protocol supply/withdraw. For
Aave, copy `templates/aave_supply.py.j2` to candidate-local `aave_supply.py`,
call `aave_supply_tick` from `run(ctx)`, and load the financial request contract
in `references/mainstream-strategies.md`. Preserve `ctx.financial` action
identity; `ctx.trading.open_position/close_position` cannot deposit or withdraw.
Keep unavailable replay execution explicit; the helper's non-live hold is not
a simulated protocol fill.

This section is the installed SDK contract for ordinary script authoring. Do not
use shell, Python inspection, repository globbing or installation paths to rediscover
these signatures. The application source directory is not the user workspace.
Read another reference only for a genuinely missing capability, not to confirm
the calls already documented here. Write operator-facing progress in the user's language.

A model already awakened to decide whether AI should run is NOT a script gate.

Use `from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask`.
For plain EMA crossover, load the tested `templates/ema_cross.py.j2` and
`templates/ema_cross_contract.py.j2` through `Skill(skill="strategy_author", file=...)`.
Also save `templates/strategy_execution.py.j2` as `strategy_execution.py` beside
main.py; it owns the receipt/reconciliation contract below.
Use the EMA contract below; do not invent another recurrence, decorator or test API.
For AlphaTrend use the SDK's tested pure function instead of inventing the
indicator again: `from nerya.strategies.indicators import alphatrend` and
`signal = alphatrend(candles, period=14, coefficient=1.0, offset=2)` (use requested
parameter values). `signal['alpha']` and `signal['lagged']` are aligned arrays;
`signal['buy'][-1]` / `signal['sell'][-1]` are crosses against the SAME line delayed
by two candles, NOT upper/lower price channels. ATR smoothing defaults to SMA of
true range; MFI is preferred and missing volume uses RSI. A spot strategy is
long/flat unless margin/short selling is explicitly requested. On a sell signal
close an existing long, never silently create a leveraged short position.

For a standard long/flat indicator strategy, use the finite execution template.
It reuses the SDK candle/parameter/result helpers; it adds a durable submission
record because the installed `run_signal_strategy` only consumes a timestamp
and does not retain ambiguous submission receipts for reconciliation:

```python
from nerya.strategies.indicators import alphatrend
from strategy_execution import run_closed_signal

def run(ctx):
    return run_closed_signal(ctx, alphatrend)
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
The configured universe is `ctx.config.markets`; reject any trigger market outside
it BEFORE data, state, Agent or trading calls. A candle callback is routed by
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

### Receipt and retry contract

Trading methods return dictionaries, not ResultBuilder objects. Return a real
StrategyResult with the full receipt under `metadata.execution`; retain plan_id,
session_id, executor_id, approval_id, order_id, nested order_ids, risk decisions
and reservations. An `ok` wrapper loses the operational status. The installed
`StrategyResult.from_trade_envelope` defaults unknown statuses to submitted, so
classify before calling it:

| SDK status | Strategy result | Recovery |
|---|---|---|
| rejected, pending_approval, submitted, partial, filled, error | Preserve status | Retain receipt; pending/submitted/partial require reconciliation |
| created, reserving, ready, working, closing, canceling | submitted, raw status retained | Executor still in progress; reconcile |
| failed, canceled | error, raw status retained | May include partial effects; reconcile |
| missing/unknown/non-dictionary | error (execution_unknown) | Never assume no submission or fill |

Use `templates/strategy_execution.py.j2` for plain and confirmed signals. Its
state key includes signal, market and timeframe. Validate closed, strictly
ordered timestamps before evaluating signals. Only read-only data/position/Agent
failures BEFORE entering the SDK are known no-submission failures and leave the
candle available for re-evaluation. An analysis-only confirmation has no trading
tools or other side effects. A completed veto/no-signal consumes that candle.

Persist a pending operation BEFORE the trading call. A timeout or SDK exception
may follow order acceptance: retain the marker and block repeat submission even
on a later candle. Persist the entire returned receipt; do not clear on error,
rejection or cancellation labels alone. Pending approval is resumed by ID, not
submitted again. Read `ctx.trading.execution_status(receipt)`: it observes local
authority by the original IDs without polling a venue, reconciling or submitting.
Release pending only when `terminal:true`, `reconciled:true`,
`has_open_orders:false` and status is `filled`, `rejected` or `canceled`. Preserve
the receipt, IDs and observation as `last_submission`, and retain the processed
candle timestamp to prevent same-candle resubmission. Unknown, partial, missing
or pending-approval evidence keeps the fence; report `recovery` and obtain
authoritative reconciliation before another trade. Neither a changed position,
a timer nor a retry counter proves terminal execution.

State get/set is not an atomic lock: serialize invocations for each strategy/
market/timeframe. Concurrent exactly-once execution needs a runtime atomic claim
or SDK idempotency contract; report that capability gap instead of claiming it.
Test real receipt shapes and both failure boundaries, including duplicate/new
candles while pending, venue/symbol scope and still-open last bars. Candle stubs
must honor `limit`; derive fixture timestamps from the returned rows.

Expose tunable parameters in strategy.yml and actually read them. Explain the saved
parameters through the shared SDK: `ctx.config.params`, `ctx.config.timeframe`,
and `ctx.config.get(...)` work identically in runtime and replay; the older
`ctx.config.extras` mapping remains available. Do not catch AttributeError and
silently replace authored parameters with defaults. Review scheduling must remain
disabled until activation is requested. Generate a review plan only when requested
or useful to the chosen design. A loss is a valid research result.
Persist protection state until the settled position is actually flat, not merely
when a close request is submitted. Use entry/prior high-water state for historical
stops, never `price <= price - distance`, and never relabel a generic EMA as a named indicator.
Standard percentage/price stop-loss and take-profit rules, percentage trailing
stops and time limits can be passed unchanged to `open_position(protection=...)`;
the native replay has a recorded historical execution model for these rules.
Protection `type:"pct"` values and trailing `activation_pct`/`trail_pct` use fractions (`0.02` = 2%, `2.0` = 200%; stop-loss requires `0 < value < 1`): for an invalid-value error, preserve the producer's exact error surface/message and correct the parameter to the intended fraction while retaining native protection; generic `backtest_sdk_unsupported` alone does not establish unsupported protection or justify replacing it with close-price strategy exits.

### Concrete call shapes (examples, not strategy signals or parameter defaults)

```python
params = ctx.config.params                  # exact strategy.yml params
timeframe = ctx.config.timeframe            # exact strategy.yml timeframe
market = ctx.trigger.get("market")          # routed candle event
if market is not None and market not in ctx.config.markets:
    raise ValueError("unconfigured event market")
markets = (market,) if market is not None else ctx.config.markets
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

### Plain EMA crossover: tested numerical and execution contract

`templates/ema_cross.py.j2` is literal Python (no substitutions required): copy it
to the candidate's `main.py` and adapt the annotation prose to the requested scope.
It contains the small EMA/signal helper and `run_closed_signal` delegation to
the companion `strategy_execution.py` asset.
Do not import a Skill asset by an installation path. Keep `def run(ctx)` and
version-1 annotation comments; there is no strategy decorator to invent.

Save the requested periods under `params.indicator.fast_period` and
`params.indicator.slow_period`, with the requested `params.lookback`, `sizing`,
`protection` and `confidence`. Keep markets, timeframe, capital and risk limits
unchanged. Choose sufficient lookback for two defined EMA points: at least
`max(fast_period, slow_period) + 1`; honor any larger requested warmup. These
fixtures' 12/48 periods, prices and trade settings are NOT strategy defaults.
`run_closed_signal` removes a forming final candle before this check, so a
window with only `slow_period` closed candles returns the explicit
`insufficient_closed_history` hold reason and must not be described as a
market no-signal result.
If an existing manifest uses top-level `params.fast_period` / `slow_period`,
adapt the local wrapper to pass those saved values to the signal;
do not silently replace them with 12/48 or change the manifest's semantics.

The helper uses an SMA seed (`statistics.mean`) and the update
`previous += k * (value - previous)`, where `k = 2 / (period + 1)`. Output stays
aligned with the input, with `None` before warmup. Periods must be positive
integers; nonfinite input is an error. The installed native
`nerya.data.indicators.ema` pure-Python fallback currently uses
`value*k + previous*(1-k)`, which fails the constant-price regression, so it is
not a stable substitute for this reference. This is a small authoring asset,
not a second indicator library; reuse a shared SDK EMA once it satisfies this
same numerical contract. No new dependency is needed.

Crosses compare consecutive defined points: buy requires previous fast <= slow
AND current fast > slow; sell requires previous fast >= slow AND current fast
< slow. Equality on the current bar never signals. Do not round the lines or
add `isclose`/epsilon to forgive an already-positive/negative prior gap: that
can manufacture repeated entries while the lines remain on the same side.
The SDK runner consumes only the final CLOSED bar's signal, routes markets,
deduplicates per market/timeframe, checks settled positions and forwards saved
sizing/protection unchanged. Submission receipts remain distinct from fills.

Save `templates/ema_cross_contract.py.j2` as `tests/test_contract.py`. It uses
the real `ResultBuilder`, explicit-signature data/trading stubs and the actual
signal function. Do not use a blanket `MagicMock` context/result that accepts
nonexistent methods or wrong calls. Assert the concrete result's status and
reason as well as trade calls: `ok(reason=...)`, `hold(reason=...)` and
`error(message=..., kind=...)`. Extra `ok(summary=...)` data is metadata, not
the result's reason. Keep SDK errors and rejections observable.

The regression has 190 closes of 3000 followed by 3500 (entry) or 2500 (exit).
The weighted update drifts EMA48 to 2999.9999999999995 while EMA12 stays 3000;
it invents an earlier cross on a constant bar and misses the final entry.
Tests must prove there were no prior crosses and that the final bar crosses.
Also test constant equality, an already-crossed trend, insufficient warmup,
entry while flat, exit while long, duplicate callbacks and changed periods.
Retain genuine below-to-above/above-to-below coverage, not only equality cases.
Keep test prices and time local; candidate validation owns isolated execution.
After saving, use `strategy_validate` on the same proposal after the last edit;
no shell probe, network call, live order or activation is needed for these tests.

