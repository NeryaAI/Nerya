# Trend signal with synchronous Agent confirmation

## Delivery binding

When this guidance is loaded for a script-driven Agent request, the enclosing
`strategy_author` delivery uses the canonical workflow key
`strategy_authoring` from `references/delivery-contract.json`. Do not derive a
`workflow_id` from the task, market, model, strategy or backtest title: for
example, `binance_btcusdt_trend_paper_backtest` is a descriptive title, not a
workflow key. If the backtest Skill starts its own replay delivery, that
separate delivery may use `strategy_replay`; it must not replace the enclosing
authoring key. This remains true for paper replay and report generation.

For “signal first, Agent confirms, then trade”, prefer `execution_mode: script`
with `agent_task.enabled: false`. The script calls `ctx.subagents.run` only after
the deterministic entry gate, reads the returned envelope's `output`, and owns
the subsequent SDK entry/exit. Script execution may include a subagent call;
assert zero model calls specifically on branches that should not need one.

Use `execution_mode: agent` with `agent_task.enabled: true` when handing control
to the Agent executor. Return `StrategyAgentTask.dispatch/skip/error` at the top
level. `ctx.result.ok(agent_task=task)` stores the task in metadata and the executor
rejects the StrategyResult. Ordinary `ctx.result.ok/error/hold/skip` returns also
violate that entrypoint contract, including after an exit. Dispatch has no
synchronous verdict callback. An analysis-only Agent profile cannot complete an
executable confirmation-before-entry request by returning suggestions alone.

## Before the first draft

For the canonical trend-confirmation scaffold, call `strategy_draft_proposal`
with `strategy_class: "trend"`, `execution_mode: "script"`, and an explicit
nonempty `subagents` list; its first role becomes `params.confirmation_agent`.
Pass the requested candle `timeframe` as a top-level field. Omit `files` to use
the canonical package: a supplied `files["main.py"]` selects caller-authored
logic and bypasses this scaffold. Reuse the returned helper and role files;
read a candidate file before editing it. Agent-executor dispatch uses the
separate `agent/true` contract above.

## Candidate settings

Preserve the user's markets, accounts, timeframe, periods, sizing, protection and
limits. For synchronous confirmation use `mode: paper`, `execution_mode: script`,
`agent_task: {enabled: false}`, `subagents: [market_analyst]` and
`policy.allow_direct_order: true`. Direct-order permission enables the SDK call;
Risk Gate and Approval Gate still apply. Keep both trading and review schedules
`enabled: false`. Authoring and isolated tests do not activate account trading.

## Confirmation step

The trend scaffold already copies these canonical assets into the SAME candidate;
reuse the returned candidate files. Use a revision-bound source-asset copy only
when one is missing or the implementation actually needs that asset changed; do
not recopy unchanged helpers or rewrite their annotations as a prerequisite:

- `templates/ema_cross_confirm.py.j2` as `main.py`.
- `templates/ema_cross.py.j2` as `ema_cross.py`.
- `templates/strategy_execution.py.j2` as `strategy_execution.py`.

The trend scaffold with `execution_mode: script` and an explicit `subagents`
list emits this package. Set `params.confirmation_agent` to its declared role and
save `confirmation_min_confidence`, `indicator.fast_period/slow_period`,
`lookback`, `confidence`, sizing and protection. Its EMA 12/48 and 15m fallback
are draft defaults, not inferred user choices; preserve explicit saved settings.
`run_closed_signal` owns configured-market routing, closed candles, settled
positions and submission state. The confirmation callback runs only for an
eligible entry. A downward cross with a settled long exits without Agent approval;
a downward cross while flat holds. See `references/script-sdk.md` for receipt
states and reconciliation. Run contract tests with real SDK dictionary receipts.

Use the real ResultBuilder for hold/error outcomes. Return the execution helper's
StrategyResult directly: putting a receipt in `ctx.result.ok` metadata flattens
failure and approval states. SDK exceptions remain failures and retain the
pending submission marker; a submission receipt is not a fill. Read the verdict from
`envelope['output']`, never environment variables or outer-envelope verdict fields.
Ship `subagents/market_analyst.agent.md` describing the supplied inputs and JSON
output: decision ENTRY|HOLD, numeric confidence 0..1, drivers and veto_reason.
The script enforces the saved threshold, sizing and protection.
Confirmation context projects closed candles to timestamp/OHLCV only, retaining
the full signal history. `params.confirmation_context_bars` defaults to 60 and
is capped at 60; a shorter explicit value changes the model's context and must
be disclosed. Invalid or missing OHLCV fails confirmation before a model call.
This does not change EMA periods, confirmation rules or authorized call budgets.

Align custom reviewer requirements with `main.py`'s actual payload: this scaffold
supplies `market`, `timeframe`, closed OHLCV `candles`, EMA `signals` with
`fast/slow/buy/sell` arrays, and `ask`. Keep additional indicators such as RSI/ATR
optional unless the strategy requires them. When required, specify their formula
and warmup, derive them only from the supplied closed prefix when sufficient, or
compute them in the script and pass them explicitly. An absent precomputed
optional indicator alone is not a missing-data veto; genuinely insufficient
inputs for a required rule warrant HOLD with the precise gap.

## Required behavior evidence

Use real ResultBuilder and explicit-signature stubs, not a blanket MagicMock
context/result. Assert exact counts, arguments, reasons and return statuses:

- Prove the positive fixture's previous fast <= slow and current fast > slow.
  ENTRY with adequate confidence must cause one confirmation and one entry with
  saved sizing/protection. Do not infer a cross from a price bump.
- Prove the negative fixture's previous fast >= slow and current fast < slow.
  Settled long quantity must cause exactly one exit and zero model calls.
- HOLD, low confidence, missing output, failed envelopes, invalid decisions and
  boolean/nonfinite/out-of-range confidence must cause zero entries.
- No signal, warmup, holding without an exit and duplicate closed candles must
  cause zero LLM/subagent calls and zero entries; test dedupe in the caller.
- Preserve trade rejection and pending-approval receipts. An assertion accepting
  any object with status, either skip or exit, or only a captured dictionary
  does not prove the return/executor contract.
- On data/position/read-only Agent failures before submission, retry the same
  candle without a consumed marker. On SDK exceptions, pending/unknown outcomes
  or partial execution, preserve state and IDs across subsequent candles and
  reconcile before another trade. Prove both paths; never clear state merely
  because a call raised. Confirmation profiles must be analysis-only with no
  trade tools or side effects for the pre-submission retry rule to apply.
- Foreign venue/symbol triggers must stop before market/Agent/trade calls.
  Exclude the still-open final candle; dedupe keys include market AND timeframe.

Stubbed verdicts prove branches only. A valid fixture is an envelope such as
`{"ok": true, "output": {"decision": "ENTRY", "confidence": 0.8}}`;
an empty list becomes an empty envelope and must fail confirmation. Even a
completed OHLCV replay with a fixed verdict is not historical Agent performance;
report `performance_evidence:false` for that claim. `replay` availability must
come from the backtest capability contract, not an error-message suggestion.
If real historical Agent decisions are unavailable, report the capability gap
and keep the requested AI-performance acceptance incomplete. Keep synthetic
fixtures separate from historical data and account execution.
