# Strategy authoring edge cases

The root `SKILL.md` owns the default creation and completion path. Consult this
reference only for the relevant implementation or recovery question.

## Candidate and resource bindings

Save and improve the same editable proposal. Preserve the user's explicit
market, chain, strategy rules, timeframe and risk limits. Reasonable reversible
defaults are Agent decisions; state them without turning each into a question.
Accounts, credentials, pool addresses, ranges and data can remain pending using
the advertised draft interfaces. Record what still needs binding before
execution; do not invent an identifier or claim an unbound strategy is runnable.

Choose templates by their actual interfaces or implement the requested behavior
directly. Read the files relevant to an edit and retain required imports;
template copying, a whole-package reading pass and a separate explanation for
every script are not completion requirements. Explain the strategy's behavior
and limitations to the user. Optional annotations should match the saved code.

Creation completes with a saved candidate and its source binding. Validation,
tests, submission, tuning, replay and reports are separate actions, performed
when requested or useful within existing authority. Create/draft defaults to
`validate:false`; explicit `validate:true` requests diagnostics. Default to no
test execution. Independent `strategy_validate` retains real failure results;
submission uses static diagnostics with `smoke_test=False, run_tests=False` and
can enter `pending_review` despite failures. Diagnostics remain visible without
preventing draft saving or review submission.
An incomplete scaffold is a saved draft, not an implemented algorithm.

## Match the execution intent

| Intent | Runtime contract |
| --- | --- |
| Pure script ticks | `execution_mode: script`, `agent_task.enabled:false`; no model calls in the tick. |
| Indicator qualifies before AI | Agent mode/enabled; Python gate and durable dedupe precede `StrategyAgentTask.dispatch`. |
| Scheduled AI without filtering | Agent mode/enabled; thin dispatch adapter and editable `agent_profile.role`. |
| Observation only | `evaluation.mode: observation`, no trading calls/tools and `policy.allow_direct_order:false`. |
| Simulated trading requested | Preserve buy/sell rules in the isolated paper/replay context; simulated fills do not authorize account orders. |

Keep new schedules disabled. Review/tuning is optional; respect existing opt-outs
and add it only when requested or needed by the design. A model awakened to decide
whether AI should run is not a script gate.

For scheduled Agents, provide actual collected inputs or permitted read-only
tools so the strategy can obtain them later. `allowed_tools: []` means no tools.
`dispatch.outputs` selects already published inputs, not the future Agent answer.
A quote fetched by the authoring Agent does not prove future strategy input
collection. Use `references/workflows.md` for the applicable routing interface.

An actual persistent feed loop uses the supported continuous execution path;
finite candle events remain routed Agent invocations. Use
`references/continuous.md` when designing a listener. Any chosen listener test
must be bounded and isolated; authoring never implies starting the service.

## SDK behavior and optional checks

Use public imports from `nerya.strategies` and actual installed call shapes.
`ctx.result.error(message=..., kind=...)` differs from
`StrategyAgentTask.error(reason=...)`. `ctx.portfolio.positions(market)` is a
list; accounts are in `ctx.config.accounts`. Read `references/script-sdk.md`
when these interfaces are needed rather than inventing helpers or imports.

Process eligible closed candles with the injected clock, align indicator series
by timestamp and persist dedupe state. Preserve zero/false input values.
Data/state failures remain errors; insufficient history is not proof of no
signal. A trading call can submit independently of the entrypoint's return
value; a decision label, `ctx.result.ok()` or dispatch intention is not a fill.
Inspect actual receipts and settled positions before claiming execution.

When checks are useful or requested, select the relevant branches: entry, hold,
exit, duplicate events, input failure or Agent dispatch. Exercise the actual
candidate and SDK contract; permissive fakes or import-only assertions do not
establish behavior. Fixtures are branch evidence, not historical market data.
Preserve process failures, sandbox isolation and any execution approval.

## Requested research and honest evidence

For an explicit historical backtest/replay request, load `backtest` and let it
manage period selection, data reuse, simulation and partial results. Explicit
dates and risk rules stay intact; disclose supplemental windows and missing data.
A creation request does not imply historical research or full LP reconstruction.
Inspect the exact error before changing an execution model; unsupported replay
behavior does not authorize deleting protection or changing the user's strategy.

Describe saved files, review state, validation, behavior tests, replay/model
execution and trading permission separately. Checks that failed, were not run or
became stale remain so; do not manufacture passing checks. A report path in prose
is not a report receipt, and `performance_evidence:false` remains a limitation.

On interruption, reconcile the candidate, source revision and the relevant
producer receipts before continuing. Read only what the next action needs.
Keep pending approvals untouched and resolve uncertain effects through their
original operation; never replay an uncertain write/model request or switch
tools to bypass its boundary. Preserve the candidate and explain remaining work.
