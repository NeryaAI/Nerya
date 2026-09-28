<!-- nerya-skill-frontmatter-start -->
---
name: strategy_author
description: "Create, edit, debug and validate Nerya strategies directly in the main Agent conversation: trading backtests, 观察策略/盯盘, scripts, indicator-gated Agents, scheduled Agents and review evolution. Complete requested tests, not only files."
version: 0.15.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Extended strategy authoring contract

This is an on-demand reference, not a mandatory checklist for every strategy.
The root SKILL.md owns the default short path. Load only the applicable section.

Use to author SDK strategy code and validate its actual execution contract.
Keep session context advisory and preserve the user's established market scope.

Order evidence and multi-market scope are explicit SDK contracts. A candle
callback's market is `ctx.trigger.get("market")`; `ctx.config.markets` is the
full configured universe, never a per-callback singleton. A scheduled callback
without a market may iterate that universe, with per-market closed-bar dedupe.
An actual trading SDK call submits independently of `run()`'s return value.
Never claim `return ctx.result.ok()` discards submitted orders. Conversely,
an `open_long` action label is a decision, not a submitted or filled order.
Use an allowlist of actual receipt statuses, preserve rejected/error reasons,
and read settled positions before persisting entry prices or clearing stops.
Initialize compare-and-set with the actual stored value, including `None`.

## Explain strategies before showing source

The root Skill supplies the normal `@nerya` annotation contract. Only load
`Skill(skill="strategy_author", file="references/explanations.md")` when its
additional review detail is needed; it is not a mandatory call for every script. Explain actual logic,
design reasons, change scope and evidence limits in the user's language. Keep
comments synchronized with saved code; the workflow displays them as logic steps.
Read back the final annotations and pass strategy validation before delivery.

For bounded train/calibrate → replay → review → propose cycles after the first
valid baseline, load `Skill(skill="quant-strategy-loop")`. Its confirmed goal,
cycle budget, chronological holdout and stop gates remain mandatory; an authoring
request alone does not authorize starting an optimization loop.

**Stay in the main Agent conversation.** The user states intent; you implement and verify the actual candidate. The workflow page is a view/editor of the resulting files, not a creation wizard or a place the user must visit to finish your task.

For requests such as “市值前 N / top N by market cap”, keep universe discovery
bounded so research cannot consume the authoring turn. Load `markets`, then for
crypto market-cap requests use exactly one
`market_data(action="ranked_universe", venue="binance", count=N,
rank_by="market_cap", quote="USDT")` call. Use its ordered `market_ids` directly
for the candidate. Do not call data_api, web search/fetch, `run_shell`, ad-hoc
urllib/curl scripts, Git checks or cached-web-artifact hunting after that call
succeeds. Once the requested tradable universe is resolved, move immediately to
the candidate; do not repeatedly re-verify the same symbols.

## Research visuals during authoring

For an explicitly requested additional research visual, load
`Skill(skill="research", file="references/visual-deliverables.md")`. A strategy
and its standard backtest already publish their own cards; do not call research
tools merely to duplicate them. For the separate requested research visual, publish those
instruments with observed price data in each relevant authoring reply, including
follow-ups. The reply ends with a price/change/sparkline card; its observed
candlesticks and indicators open on the right. Never attach this to the composer. Publish signal comparisons, flow studies and available
backtest evidence as interactive charts on the left. Execute the publisher
and check its receipt: do not merely describe charts that were never emitted.
Chart publication does not replace proposal validation or an actual backtest,
and never authorizes activation, an order or a change to the Agent Loop.

## Continuous / WebSocket strategies

A finite Agent invoked by `market.candle_closed` or another routed event is NOT
a continuous listener. It uses Agent mode plus a thin task builder and the
normal trigger executor. Only an actual long-lived feed loop uses `ctx.stream`.
For observation-only Agents with no tools, specify a plain-language final-answer
contract in the strategy profile/task: analyze the supplied data directly, no
file generation or textual tool-call markup. Model end_turn alone is not proof
of a valid result. Preserve actual stopped reason and rejected/failed outcomes.
Pass a canonical UTC data cutoff as `data_as_of` when publishing archived data;
do not call the run's `captured_at` the time of the market observation.
Load `Skill(skill="backtest")` before replay to distinguish script performance,
Agent dispatch/input replay and actual model execution. Use one schedule form
(cron OR interval), never both; a disabled schedule is still a valid candidate.

For 持续运行、常驻、实时监听、WebSocket or event-driven scripts, read `references/continuous.md` first. Use `runtime.mode: continuous`, a reviewed named feed and `ctx.stream`; never put an infinite listener inside a cron tick. After authoring, validate and submit the inactive candidate, then execute bounded isolated listener/event tests. Continuous candidates are not OHLCV backtests: test finite signal logic separately. Do not promote/start a production candidate simply to satisfy verification. For explicit start/stop requests use `strategy_service` (start requires the current package hash). Runtime status and actual order/fill receipts, not a drawn edge or returned dispatch, prove execution.

## Completion contract — apply before ending the turn

A ban on submission means authored draft only: do not submit or promote it.
A ban on running/promotion/trading does not ban implementation or explicitly
requested isolated tests. “No orders” is a runtime restriction, NOT a request
for an empty scaffold. Honor a separate explicit no-test instruction.

**“暂时别开启自动运行 / 不要下单” prohibits activating schedules and account orders, NOT the requested validation, branch tests, submission for review or isolated historical replay.** Those steps are already requested. Do not ask the user whether to continue them, offer a menu of unfinished work, or say “已完成” after only writing main.py. A normal authoring request includes final-file validation and submission; honor an explicit draft-only/no-test instruction instead when present.

Keep these deliverables pending until their actual receipts exist:

| Deliverable | Receipt needed before calling it done |
| --- | --- |
| Implemented strategy | Actual files at the returned proposal paths, not unchanged scaffold. |
| Final code checked | Successful `strategy_validate` after the last source change. |
| Review candidate | Successful `strategy_submit_proposal` for that candidate; not promotion. |
| Requested/required replay | `strategy_backtest` executed on that candidate with the intended config; inspect outcome. |
| Requested branch tests | Test code actually executed and exit/result inspected, not only a test file or plan. |

**Before replying, compare this table to the tools you actually ran. If a requested receipt is missing and the action is available, the NEXT ACTION IS THAT TOOL, not a final response.** A known execution error means inspect and repair the same candidate, then retry the affected check. A genuinely unavailable source, explicit approval refusal, uncertain side effects or exhausted budget is a real boundary: report that precise limitation and preserve work. A routine file write, disabled scheduler, poor return or untested branch is not such a boundary.

## Scheduled observation must have real inputs and finish submission

For scheduled analysis without a gate, read the scheduled-Agent section of `references/workflows.md` BEFORE editing; the routing examples in script-control.md are not a substitute. Use a thin dispatch with declared data sources in `agent_context.sources` and an editable `agent_profile.role`. Keep read-only market tools available, or provide actual collected snapshots. `allowed_tools: []` means NO tools, not “observation tools”; removing all tools AND supplying no data leaves an analyst unable to inspect markets. “No orders” is not “no market tools”. Do not change the requested real market to mock merely because an existing paper account has a mock execution venue; account execution and public data are different.

`dispatch.outputs` selects **already published upstream inputs**. It is NOT the name of the Agent's future answer. Never select `outputs=['observation_summary']` unless the script has actually published that input. For a thin scheduled adapter omit outputs or pass []; let the runtime store the Agent's answer normally. Verify one dispatch plus context collection without enabling a scheduler, using tests for the adapter/input contract. For a local-time schedule prefer the explicit local cron plus named timezone. A live market quote fetched by the MAIN Agent during authoring does not prove that the STRATEGY Agent will receive data later.

Once final-file validation succeeds, immediately submit THAT candidate and inspect the required historical replay. Submission only puts it in review; it is not schedule activation or permission to trade. Never stop with “I can next submit it” when creating a strategy is the current authorized task. Keep any missing market data or replay coverage visible rather than skipping the replay itself.

## Preserve the user's execution intent

| Intent | Required runtime contract |
| --- | --- |
| Pure script execution | `execution_mode: script`, `agent_task.enabled:false`, `ctx.result.*`; no model/team/Agent calls in trading ticks. Author a separate, inactive review plan. |
| Explicit whole-strategy no-AI/no-review | Also keep `tuning.enabled:false` and document the review opt-out; do not add an automatic review Agent. |
| Indicator must qualify BEFORE AI | `execution_mode: agent`, `agent_task.enabled:true`; Python gate + durable dedupe BEFORE `StrategyAgentTask.dispatch`; other branches return stop/skip/error. |
| Scheduled AI without filtering | Agent mode/enabled, thin dispatch adapter and editable Agent role; no indicator pre-filter. |

A model already awakened to decide whether AI should run is NOT a script gate.

A genuine observer uses `evaluation.mode: observation`, no trading calls/tools and `policy.allow_direct_order:false`. A requested **trading backtest** is different: implement the intended buy/sell rules and matching direct-order policy in an unpromoted paper candidate using `evaluation.mode:trading`. “No account orders; replay simulated fills only” does not mean remove trading logic. No authoring/replay request authorizes promotion, real account orders or schedule activation.

## Short execution path

For historical verification, load
`Skill(skill="backtest", file="references/history-data.md")`. Keep creation and
data preparation separate: preflight the saved candidate/config before long
downloads, prepare reusable history with `historical_data`, then replay locally.
Do not claim a requested one-year backtest is complete when its strict data gate
reports missing candles. Preserve the candidate and downloaded segments on errors.

Do not introduce cleanup tasks. A scaffold test file is not a reason to delete files or request destructive shell access. Unless explicitly requested, keep the existing tests; correct their contents through normal file edits when necessary. Never delete tests to silence a failure or because the strategy is observation-only. Finish native validation/submission/replay before unrelated housekeeping.

1. **Read only relevant references through `Skill`.** Start with `references/workflows.md`; use `references/script-control.md` for script routing/input selection, `references/specialized-contracts.md` for trading/special markets, `references/verification.md` for testing. If a read returns `next_offset`, read the relevant continuation before copying an incomplete example. Do not use host-path `read_file` for skill references.
2. **Resolve and retain the candidate.** Preserve market, timeframe, local clock and restrictions. Choose existing reversible defaults for omitted details; ask only about a material unresolved choice. Use `strategy_draft_proposal` for new work, retaining its exact proposal ID/paths. Existing candidate or continuation: operate on that candidate, never silently recreate or delete it. Never edit the active package directly. Proposal IDs recalled from search/history are historical references, not proof the candidate still exists: confirm current proposals before reading or editing a recalled `prp_*` path.
3. **Write the real files and then the separate review plan.** Read before editing; replace incompatible scaffold logic, including a stale `build_agent_task` that overrides `run`. Write one complete substantial file per tool batch and immediately continue afterward; that is not a one-file-per-user-message limit. Expose parameters in the manifest and actually consume them. Write requested branch tests, not just explanatory comments. Once strategy behavior is settled, adapt `templates/review-plan.md` into tuning and its package-local prompt file in the same candidate. Include the independent scheduler, evidence window, tailored checks and hold/validation conditions. Refresh affected plan sections after behavior changes; a generic scaffold is not completion.
4. **Prepare nondefault replay settings.** Read `Skill(skill="backtest", file="references/config_schema.md")` when necessary; this schema belongs to the backtest Skill, not strategy_author. Write supported settings for requested `window_days`, `tf`, warmup/sizing/costs BEFORE the first replay; keep the exact workspace-relative path. Passing `config_path` is mandatory to use that file: merely creating backtest.yml does not change the tool's default preset. Ensure the file exists and warmup covers the authored indicator's closed-bar needs.
5. **Validate → submit → replay.** Run final-file `strategy_validate`; fix blockers and revalidate, then submit for review. Execute requested/required `strategy_backtest(proposal_id=..., allow_mock=false, config_path=<prepared path>)`. Required-action suggestions do not override the user's config. Fix concrete code/config failures in the SAME candidate, then revalidate/replay. Do not loop on unchanged failing payloads. Economic FAIL is a research outcome, not permission to alter the requested strategy. If OHLCV replay rejects live `protection`, preserve stop/take-profit semantics with explicit historical close logic; never make the test pass by deleting a requested risk rule without an equivalent replay path.
6. **Execute requested branch tests.** Use the installed interpreter and actual workspace/conversation cwd. Preserve process exit status. Do not guess host .venv paths, append echo or pipe away test failures. The signal/no-signal/duplicate branches require actual tests when requested. Prefer standard-library assertions/unittest for a self-contained test when no framework is available; do not install dependencies or cross a denied boundary. Do safe native validation/replay before an optional action that genuinely needs approval.
7. **Inspect and finish here.** Check the latest files and actual report. Reply with the implemented behavior, performed tests, actual data dates/coverage, any genuine gaps, and `/strategies?strategy_id=<id>&proposal_id=<id>`. Keep the candidate and schedules inactive. Do not offer to do unfinished authorized checks in another turn.

## Final-file behavior audit — not just successful tool calls

Before final validation, read back **strategy.yml AND main.py** from the same candidate. For an observer the saved YAML must still contain `evaluation: {mode: observation}`, declared `data_sources` with editable parameters, disabled schedule, no-order policy, and (for Agent mode) an editable `agent_profile.role`. Writing these once does not count if a later whole-file replacement drops them. Choose a real existing account before drafting; a missing accounts argument is not a planning strategy. Code must read the saved parameters, not substitute hidden indicator/timeframe/limit constants or bury the Agent's role in Python.

**Closed-candle processing:** feeds and replay can include a forming final candle. Filter the whole returned series to candles whose opening timestamp plus timeframe is no later than `ctx.clock.now_ms()`, sort/deduplicate by timestamp, and compute on the newest eligible closed candle. Do NOT return skip merely because `candles[-1]` is open: this can skip every replay tick although the preceding candles are closed. Preserve timestamp units explicitly. No eligible closed bars/history shortage can skip; a failed reader or state store must return an error, not disguise failure as no signal. Do not interpret an all-skip replay as evidence of no cross: inspect recorded skip reasons and verify the gate actually reached indicator calculation.

**Test actual entrypoint behavior, not permissive helper assertions.** A gated strategy needs deterministic assertions that `run(ctx)` returns dispatch on a known qualifying closed signal, skip on a nonqualifying input, dispatch on a closed signal followed by a forming candle, and skip on the same signal with the SAME persisted state after dispatch. Verify selected payload and no model/order calls on stopped paths. `assert state in ('bullish_cross','no_cross')`, `else True`, import-only tests, or checking a signal timestamp do not test dispatch or dedupe. Include an exact positive-path assertion and negative controls. Controlled inputs are appropriate for branch tests, but label them; the separate historical replay must use real data. Standard-library unittest avoids inherited project pytest markers when a generated standalone test has no marker. Execute tests with their real exit code, never `; echo`, terminal-plugin suppression or a tail pipeline that hides failures.

For ordinary create requests, complete final validation, submission and required replay in the same conversation. For script gates, run the above minimal branch tests as part of checking the implementation. On concrete failures repair the SAME candidate and rerun affected checks; don't tell the user to turn on a schedule to diagnose a gate. In the final response distinguish tool success, behavior tests, replay verdict/coverage and actual Agent execution. A WARN/FAIL or fallback must remain visible; do not claim “validated successfully” without that qualification or say zero orders proves the gate works.

## Critical SDK constraints

Use public imports `from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask`. For script errors the actual SDK is `ctx.result.error(message=str(exc), kind="data_error")` or `StrategyResult.error(message=...)`, NOT error(reason=...). `ctx.result.skip(reason=...)` is the SDK alias for a HOLD result; the replay facade may label that path skip. For Agent tasks use the distinct `StrategyAgentTask.error(reason=...)` factory. Test reader-failure branches with the installed `ResultBuilder`, not a permissive fake that accepts every keyword. Do not invent ctx.flow, ctx.account_id, StrategyResult.order/dispatch/batch or import internal provider/SDK APIs. `ctx.portfolio.positions(market)` is a list; accounts are in `ctx.config.accounts`. Convert deque/iterables to a list before slicing.

Use real source configuration, with one connection per source and multi-market/timeframe `market_series/v1` when requested. Parameter changes must affect the consumer; no fabricated provider or market data. Use injected ctx.clock, closed candles and persistent ctx.state dedupe. Tests must assert selected branches, not only that the module imports.

`StrategyAgentTask.dispatch(context=..., sources=..., outputs=..., roles=..., path=...)` chooses actual inputs and initial roles. Omitted/None inherits; [] chooses none. Publish with `ctx.inputs.publish`; preserve false/zero. Respect include_script_outputs:false. Stop/skip/error must not auto-fetch downstream inputs or call a model. Follow script-control.md for routing and review changes.

`agent_task.enabled` enables the Agent entrypoint. Do not invent `agent_execution.enabled`: `agent_execution` controls budgets/capabilities/team only; absent/null budgets inherit the main Agent. `llm_policy.max_calls_per_run` is Python ctx.llm's limit, NOT Agent iterations. Use declared roles and `agent_execution.team` for parallel work; keep expensive analysis outside the short script gate. Honor user/role restrictions, do not change global permissions or budgets.

Local-time schedules require the actual timezone: Beijing 09:00 is `0 9 * * *` with `Asia/Shanghai`. Keep `schedule.enabled:false`. A drawn schedule or pending_review candidate is not an installed/running job. Review produces materialized, validated candidate changes, not silent application. Workflow annotations are display metadata, never another execution engine.

## Evidence is narrower than completion language

Read start_utc/end_utc, requested window, actual period, fallback, errors and order attempts. Thirty days from March do not prove the latest thirty days in September. A source gap is not synthetic success. `ok:true` is tool execution, not a profitable or fully covered strategy. Replay `agent_execution:not_run` means no actual Agent analysis, even with many dispatch intentions. Do not infer absent counts as zero or multiply _pct fields by 100. A disabled schedule does not need enabling to complete authoring and verification.


Canvas authoring and context defaults: see [workflows](references/workflows.md#strategy-canvas-and-context-contract).
