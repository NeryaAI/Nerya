# Percentage sizing and capital-efficient strategy design

This is the shared authoring contract for script, script-gated Agent and event
Agent strategies. It changes NEW defaults, not existing holdings or permissions.
An explicit user size, risk budget or named algorithm always wins. Do not change
Agent Loop, enable trading, or reinterpret an observer as a trading strategy.

## Persist the allocation, not an example dollar amount

Use the existing SDK `SizingPolicy(method="pct_nav", pct_nav=fraction)`.
The denominator is current account net asset value (NAV), including marked
positions, not starting capital, profit, or the free cash balance. The budget
checker still constrains spendable cash, reservations and account limits. Never
manually read a balance once and hard-code its dollar equivalent into main.py.
Report the requested fraction and actual filled notional separately when resized.

Default deployment budget: 90% of NAV, with 10% unallocated headroom. This is an
authoring target, not a hard portfolio exposure guarantee after price movements.
Single market or ranked winner-only: one 90% slot. Independent basket: default
K=min(3, market count), each slot gets 90%/K (two slots: 45%; three: 30%). Persist
the SAME K in strategy policy and replay. Do not divide by all 100 research
candidates when only three can be held, or allocate 90% to every one of them.
Only fill a slot on a valid signal. Do not repeatedly add a full slot to an
already held/pending position; exits close the settled quantity, not a new NAV
percentage. Multiple strategies sharing an account need an explicit shared
allocation: do not promise each of them 90% of the same account simultaneously.

Example allocation fragment for a NEW single-slot strategy (retain the rest of
the real manifest, requested fees, dates, indicator and protection settings):

```yaml
params:
  sizing:
    method: pct_nav
    pct_nav: 0.90
policy:
  max_single_order_usd: 0
  max_daily_notional_usd: 0
  max_open_positions: 1
backtest:
  max_open_trades: 1
  stake_amount:
    mode: unlimited
```

Zero dollar caps above mean no EXTRA strategy-layer dollar cap. They do not
disable Risk Gate, Approval Gate, account limits, cash checks or stop protection.
Never copy this fragment over explicit limits or an existing strategy. A real
100 USD account cap must remain and be reported as the reason a 90% target is
not achievable; it must not be silently lifted. Never use a stale
`max_notional_usd: 100` on percentage sizing. `stake_amount.mode: fixed`
overrides SDK notional during replay; do not use it for ordinary percentage
verification. It is allowed only for a deliberately requested fixed-stake study.

Script entries use the saved sizing:

```python
receipt = ctx.trading.open_position(
    market=market, side="long", sizing=ctx.config.params["sizing"],
    protection=ctx.config.params.get("protection"),
    confidence=confidence, reasoning_ref=signal_reason,
)
```

Use this only in an actual entry branch, with the authored exit/protection
contract. For `run_signal_strategy`, putting sizing in params is sufficient;
do not rely on its legacy fixed-dollar fallback for new strategies.

For Agent `risk_check` / `trade_intent_submit`, use their ACTUAL advertised
`size_pct_nav` field, sourced from saved `params.sizing.pct_nav`. Do not invent
`size_unit: percent`, or assume those tools accept the SDK's `sizing` object.
Pass the same requested size to the risk check and submission. Do not populate
both `size: 100` and `size_pct_nav`. `max_size_pct_nav` is a limit, not a substitute
for the requested size. Missing/stale NAV is blocked sizing, never a fallback to
100U. When the user explicitly chooses fixed sizing, map `fixed_usd` to the
tool's `size` + `size_unit: usd` instead. Keep the saved sizing in dispatched
Agent context so script execution and Agent orders share one source of truth.

## Active design without manufacturing trades

For open-ended requests, prefer a signal with regular opportunities in the
requested market/timeframe over a conjunction of unrelated rare conditions.
One entry driver plus necessary data/liquidity/regime checks is a starting
design, not a mandate to discard economically justified filters. Size changes
must not alter the indicator or historical signal timestamps.

For trend strategies, explain how a valid ongoing trend is entered after
warmup, how exposure survives ordinary noise, and when re-entry is permitted
after an exit. A state-based continuation rule may be appropriate for a NEW
open-ended trend thesis; never quietly add it to a named crossover strategy.
Avoid tiny scalp profit targets in a trend mandate. Exits must still control
risk. Defaults do not authorize leverage, shorting spot, averaging down or
unbounded position growth. Long-only is long/flat unless requested otherwise.

Respect explicit maximum loss budgets: a 90% position with a 2% stop has roughly
1.8% of NAV price risk before fees/slippage/gaps. If the user permits only 1%,
90% sizing is inconsistent; reduce the declared percentage to at most
`0.01 / 0.02 = 0.50` before costs, explain the tradeoff, and keep the requested
stop. Do not pretend a stop guarantees that loss ceiling, tighten it solely to
fit the allocation, or use unsupported native `risk_to_stop` replay sizing.

## Diagnose a flat curve before calling it a strategy result

Read the existing replay receipt first, without an extra tool tour:

1. Verify actual/requested dates, warmup, source coverage and price variation.
   Agent `agent_execution:not_run` is no performance evidence, not 0% return.
2. Separate signal/dispatch counts from order attempts, queued, filled,
   rejected, SDK errors and forced closes. For no fills, name the failed gate:
   no signal, cap, insufficient cash, precision/minimum size, or unsupported SDK.
3. Compare entry notional/NAV with the saved percentage and check for fixed
   replay overrides or 100U remnants. Examine `exposure_pct` and turnover.
   `exposure_pct` measures time/rows with a position, NOT the fraction of capital
   deployed. Compute capital utilization only from recorded position marks and
   equity, with a documented denominator; absent evidence is unavailable, not 0.
4. Compare net strategy return, drawdown, fees, up/down capture and alpha against
   the SAME-period, SAME-universe benchmark. Distinguish cash drag, early exits,
   signal quality and costs. More size magnifies losses as well as gains; it
   cannot repair a negative edge or guarantee benchmark outperformance.

Zero trades in an intended active trading strategy must be called
**unvalidated trading participation**, with the observed reason, not delivered
as a successful flat equity curve. Real flat results and losing results remain
visible. Never alter chart scales/data, smooth away losses, inject synthetic
trades, remove quiet dates, shorten the period, or rescale the benchmark.

Ordinary creation retains one baseline replay. Fix a concrete implementation or
sizing mismatch in the SAME candidate and rerun with recorded reasons. Poor
returns alone do not trigger an optimization loop. When optimization is requested,
freeze the baseline and budget a small explicit comparison (e.g. at most three
variants), change one hypothesis at a time, retain losing trials, and evaluate
costs and an untouched chronological validation period. Further mutable cycles
use `quant-strategy-loop`; no claim of alpha from a higher in-sample position size.
