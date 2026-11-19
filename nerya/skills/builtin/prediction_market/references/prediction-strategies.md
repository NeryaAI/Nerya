# Prediction strategies: public reads, share sizing and execution modes

Use this before drafting a prediction-market strategy. Read this Skill's
`references/prediction-markets.md` for protocol identity and live-order limits.
These interfaces are installed SDK contracts; source inspection is unnecessary.
Work in small saved edits. Once a draft exists, make the next tool call a
candidate edit (for example, save the requested sizing and schedule). Save
the entrypoint, any required helper, and its tests in separate calls. After
an interrupted turn, use the same proposal and resume at the first missing
edit. A stock candle-based entrypoint is still a scaffold. Keep the draft out
of review until the requested book logic is in place; for repairs, change
the affected function and add focused regression cases.

## Discover and bind

Use `connector_list(kind="prediction_market")`, then
`market_data(action="list_symbols", venue=<discovered provider id>)` and
`market_data(action="get_ticker", market=<returned market id>)`.
Discovery rows are outcome assets, not distinct event questions. Preserve the
selected outcome and decimal asset id, and save the market in editable parameters.
Public ticker/history support is declared by `public_ticker` / `public_klines`;
missing trading credentials do not block these reads or paper authoring.

For full identity/book constraints use this Skill's `scripts/inspect_prediction.py`.
Pass one JSON payload through `--json`, for example
`{"provider":"polymarket","market":"<discovered market id>","depth":3}`.
The script's compact `stdout_json.notes` retains identity, state, bid/ask/minimum/step/tick/time. Its full `book`
contains only the requested depth. Request history only when the strategy needs
it; `history.samples` are prices, not observed trade OHLCV or volume.
In strategy dispatch prompts, pass the configured venue-prefixed `market`
without a separate provider literal; the inspection script resolves its
provider through the registry.
If a compacted discovery list already shows a suitable outcome, use that row.
For omitted rows or inspection details, follow the saved-file recovery contract in
this Skill's `references/prediction-markets.md`; custom Agent allowlists also
need `read_file` for that evidence recovery.

## Save a strategy that can execute the requested paper trades

When the operator explicitly requested trading, pass `policy_overrides` to
`strategy_draft_proposal` with `allow_direct_order: true` and the operator's
`max_single_order_usd`. These are top-level draft-tool arguments; writing a
policy only inside supplied `strategy.yml` is insufficient because the producer
normalizes that file. Direct-order permission still retains Risk and Approval
Gates. Signal-only requests keep trading disabled.
For an existing candidate, edit its returned strategy.yml policy/params in place
and revalidate that proposal; normalization is a generation step, not a reason
to create another proposal merely to repair these settings.

Read the returned strategy.yml once to verify the saved account, paper mode,
policy and amount. For a requested fixed USD budget, store
`params.sizing: {method: fixed_usd, fixed_usd: <requested amount>}` and read that
same parameter in code. Replace an unrelated percentage-of-NAV scaffold default;
do not maintain a second hardcoded budget. For manual-only runs save
`schedule: {type: none, enabled: false}` with no timer fields. Keep tuning
disabled unless requested. Account collateral labels come from the account/SDK, not an
assumed USDC wallet.

## Finite script call shapes

```python
book = ctx.market.orderbook(market, account=account_id)
# bid, ask, ts_ms are scalars; bids/asks are [price, size] rows.
# tick_size/size_step are decimal strings; min_order_size is numeric.
best_bid, best_ask = book["bid"], book["ask"]
timestamp_ms = book["ts_ms"]
position = ctx.portfolio.position(market)  # None or StrategyPosition attributes
owned_shares = abs(position.size) if position else 0
budget_usd = ctx.config.params["sizing"]["fixed_usd"]
```

Use those SDK fields directly; raw venue JSON has a different timestamp and
level shape. Contract tests must use the SDK's array rows and `ts_ms`, not
invented dict levels or `timestamp`. Include string constraints such as
`size_step: "0.01"` / `tick_size: "0.001"` in fixtures; parse with Decimal,
since a numeric-only JSON type check would reject valid SDK metadata.
Validate positive bid/ask, a finite
nonzero timestamp no later than `ctx.clock.now_ms()`, freshness and known size constraints.
Sample the current time after the network read; comparing a newly returned
book with a pre-request clock can falsely classify it as future data. With
synchronous Agent confirmation, check freshness again after the model returns
and before submission; an expired quote needs a new observation/confirmation.
For a slow confirmation, fetch a fresh book and own position before submitting.
Proceed only if the exact approved side/limit/quantity remains valid against
that observation, budget, minimum and step. A changed order needs another
confirmation; do not silently reprice/resize or relax freshness to obtain a fill.
Keep the actual verdict in run metadata even when a later check holds. Allow
the script's execution deadline to cover configured model and data-read time;
that runtime budget is separate from quote freshness and the financial cap.
`max_single_order_usd` caps order notional, not total cash spent. For a BUY
with a cash spending limit, allow for the applicable fee upper bound before
sizing; hold if the fee basis is unavailable. Keep a verified fee bound in
its own strategy parameter: `params.sizing` accepts only SDK sizing fields.
A fee observed on one past fill is not evidence of a future upper bound.
Verify the rounded shares fit
the limit including fees. For an explicitly notional budget, use Decimal and
floor `budget_usd / limit_price` to `book["size_step"]`. For SELL,
also cap by this strategy's owned shares; apply the USD cap on exits too. An
amount below `min_order_size` is a precise hold reason, not permission to exceed
the budget. Missing market data is unavailable, not a zero price or a guessed
default. Keep a configured market stable while its orders/positions are pending.

For a notional-capped quote strategy, the quantity calculation applies to both
sides. The risk snapshot and executable price can differ, so size against the
larger observed mark; retain the actual quote in the submitted snapshot:

```python
from decimal import Decimal, InvalidOperation, ROUND_DOWN

# None/absent is unknown. A venue-reported zero minimum is distinct from None.
try:
    step = Decimal(str(book.get("size_step")))
    minimum = Decimal(str(book.get("min_order_size")))
except InvalidOperation:
    return ctx.result.hold(reason="Order book lacks usable share constraints")
if not (step.is_finite() and minimum.is_finite() and step > 0 and minimum >= 0):
    return ctx.result.hold(reason="Invalid order book share constraints")
ticker = ctx.market.ticker(market, account=account_id)
side = "sell" if owned_shares > 0 else "buy"
price = Decimal(str(book["bid" if side == "sell" else "ask"]))
mark = max(price, Decimal(str(ticker["mid"])), Decimal(str(ticker["last"])))
quantity = Decimal(str(budget_usd)) / mark
if side == "sell":
    quantity = min(quantity, Decimal(str(owned_shares)))
shares = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
if shares <= 0 or shares < minimum:
    return ctx.result.hold(reason="Requested budget or owned quantity is below this market's minimum")
```

The current share-based connector supports GTC/GTD/post-only limit BUY.
Use the fresh ask for an operator-requested marketable limit BUY and bid for
SELL. No prediction short or native TP/SL is implied by holding a No outcome.
Pass the actual signal/confirmation confidence explicitly, consistent with the
saved policy threshold. The SDK's omitted confidence is zero; outcome price is
not a substitute for signal confidence.

```python
receipt = ctx.trading.submit_intent(
    market=market, account=account_id, side=side,
    size=float(shares), size_unit="base", order_type="limit",
    limit_price=float(price), time_in_force="gtc",
    plan_action="open_position" if side == "buy" else "reduce_position",
    confidence=signal_confidence,
    reasoning=reason,
    market_snapshot=ticker,
)
```

Call only in the corresponding signal branch. Preserve the full receipt and
the pending-operation fence from strategy_author’s `references/script-sdk.md`; submitted and
pending approval are not fills. `ctx.result.hold(reason=...)` and
`ctx.result.error(message=..., kind="data_error")` are real result builders.
Use `execution_result(ctx, receipt)` from the canonical
strategy_author’s `templates/strategy_execution.py.j2` helper to return the actual trading status
and complete receipt. Reuse it when already in the candidate; otherwise copy
the helper through the normal candidate file tools. The authored-strategy
validator accepts its explicit `StrategyResult` / `StrategyResultStatus`
construction. An `ok` wrapper would lose rejection, approval and fill states.
Observe `ctx.trading.execution_status(receipt)` before another submission.

## Three execution modes

- Pure script: `execution_mode: script`, `agent_task.enabled: false`, no
  subagents and no model calls in the trading tick. The authoring conversation
  itself uses a model; that is separate from strategy execution.
- Script-owned Agent confirmation: retain script mode, declare the confirmation
  role, and follow strategy_author’s `references/trend_follow_subagent.md` for `ctx.subagents.run`
  and its `output` envelope. Adapt the signal to the requested book rules;
  borrowing the confirmation contract does not require an EMA strategy. Check
  the real model verdict, then let the script enforce budget and submit.
- Agent executor: use the existing Agent strategy scaffold and
  strategy_author’s `references/workflows.md`. Its entrypoint returns `StrategyAgentTask` at the
  top level and dispatches the operator's rules. Put decision/trading in the
  Agent task, not a hidden custom trading script or a precomputed answer.
  The Agent must be able to read the actual book minimum/step, not infer them
  from a ticker. Enable the adapter inspection path (`Skill`, `script_run`,
  `read_file`) when choosing `agent_execution.capabilities: custom` with an
  explicit `agent_profile.allowed_tools` list. For `capabilities: inherit`, omit
  that list. The bundled read-only inspection script is
  a data source, not a custom strategy trading script. Apply the same Decimal
  sizing, freshness, own-position and pending-order checks as script mode.
  In the task prompt, distinguish the strategy's owned shares from the account's
  combined position. Reconcile the configured account and outcome using the
  current strategy ID and its attributed shares. Other strategies' shares can
  make the account total positive when this strategy owns zero; those shares
  are neither a data conflict nor available to sell. If attribution is unknown,
  hold. Apply real account-wide risk limits separately, using the gate's result.

In every mode, read the saved market, account and amount from configuration.
Remove scaffold constants and fallback budgets duplicating those settings;
missing required bindings should produce a precise configuration error.

For a requested paper run of a buy-when-flat script (including one with AI
confirmation), include `tests/test_book.py` in the candidate. Start from
`templates/prediction_book_contract.py.j2` and adapt the fixture to the
candidate's parameters and verdict schema. Run the actual entrypoint with a
healthy book, then separately with unknown `size_step` and `min_order_size`:
the first case submits one budget-capped order; the other two return a reason
without submitting. Synthetic cases are neither a backtest nor paper fills.
For an Agent-only entrypoint, verify the dispatch contract instead; a scripted
order test cannot exercise the Agent's decision.

For a requested run, inspect each candidate's `strategy_validate` tool receipt.
Check its behavioral test counts against the current source revision before
submitting; `not_present` / `not_run` means no behavior was verified, even if
the structural check passed. Submission performs a separate structural check.
If `validation.ok` is false or there are blockers, edit the indicated file on
the same proposal and validate it again. `pending_review` describes the review
queue, not validation success. Once validation passes, submit and request the normal
`strategy_promote` tool authorization; pause for the operator's actual approval.
On approval continuation, replay the exact approved argument object, including
omitted optional fields. Adding or rewriting even `note` creates a different
permission fingerprint and requires a new authorization.
Bind that replay to the latest reviewed proposal ID and source revision; after
an iteration repair, an earlier applied proposal is not the approved candidate.
This existing path freezes the final reviewed files before applying them.
Do not assert `operator_approved` or waive a required backtest without the
operator's explicit instruction. A request to test paper execution is not
historical performance evidence.
When the operator explicitly says this is a paper run without a historical
backtest, use the existing promotion fields
`backtest_policy: "operator_waiver"`, `operator_approved: true` and a short
`approval_note` recording that instruction, then request normal tool approval.
Submission's `backtest_required: false` only describes draft acceptance; it
does not waive promotion's default historical-backtest requirement.

On `candidate_bundle_conflict`, check both the proposal state and the installed
strategy. If it is still editable, re-submit the same proposal for review. An
`approved` proposal that failed to apply is frozen: retain it as evidence,
stage its reviewed files in a new proposal for the same strategy ID, and verify
the new revision before requesting fresh approval. Do not change digests or
state files. Only a successful application and an installed strategy permit a
tick; a successful tool call can still contain `promotion.ok: false`.

After promotion, on a request to run once, call `strategy_run_tick` once with
the installed strategy ID. Script and script-driven Agent strategies return a tick
record. A paper Agent strategy returns an admission receipt: use its `next_read`
(`strategy_run_history` with the same strategy ID and run ID). Queued, running,
and awaiting approval are progress, not reasons to submit another tick. On a
later status question, reuse those IDs; if only the strategy is known, list its
recent `strategy_run_history` with `include_agent_runs: true`.

For “how much did it buy?”, match the run's order ID to `strategy_history`
fill rows. If the compact receipt shows `fill: "[object]"`, use `read_file`
on `strategies/<strategy_id>/history/fills.jsonl`. Sum `size_base` and
`notional_usd` from matching distinct fill IDs only. On a paper BUY with a
recorded USD fee, cash spent is `notional_usd + fee_usd`; the notional alone
does not include the fee. Compare the unrounded spend with a cash budget.
Report the book minimum from that run's observation, and the ledger value
from the matching strategy share in `portfolio_positions`, not the account total.
Missing evidence is unknown. A status question needs no new tick.
For changed market or budget, stage the edit as a
new revision of the same strategy and submit it for review before another run.
Record the actual run,
model calls (where required), risk decisions, order receipts and settled paper
positions. Authored contract tests and saved source are useful checks, not proof
that the strategy has run. Repair the same candidate when verification fails.
