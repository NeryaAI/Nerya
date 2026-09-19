# Market scope, trading and evidence contracts

These constraints extend SKILL.md; they do not replace its proposal-first lifecycle.

## Market context inheritance

Treat active session market scope as advisory context for your judgment, not a hard router. Preserve the market scope that the session has already established unless the operator changes domains. Examples are examples, not defaults. Record the Market scope assumption in strategy.md. Keep every operator-named venue, market and thesis in metadata. For example, Binance+Aster cash-and-carry retains both legs and the basis thesis; an unsupported leg is not ready instead of silently substituting another provider.

For missing low-risk details, do not reply with a questionnaire; choose non-live mode, modest sizing. Do not edit main.py away from the requested thesis to fit a convenient connector or template. When selection.mode is wallet_binding and market_data already returned the exact chain:token, rely on the runtime scanner rather than installing a fallback. On-chain means on-chain: CEX proxies are not an on-chain backtest.

## Exact candidate and approval gates

When the operator names a prp_* proposal id, resolve and operate on the exact proposal first. Use returned proposal_paths and pass proposal_id into validation/backtest calls. Do not substitute a promoted strategy_id. Do not regenerate a strategy merely because a promoted strategy path is absent, because reason:no_historical_data was returned, or because it made zero trades.

Promotion changes the workspace. Do not approve, promote, install schedules, paper/shadow/live trade or set operator_approved:true yourself during an ordinary authoring request. A user asking for implementation is not asking for activation. Honor explicit scaffold-only, keep-draft and do-not-run constraints separately.

For a requested backtest use strategy_backtest({"proposal_id":"<proposal_id>","preset":"default","allow_mock":false}), with config_path pointing to a supported replay configuration whenever the user specified a window, timeframe, warmup or sizing different from the preset. Keep simulated buy/sell fills when those are requested; evaluation.mode=trading is distinct from submitting real account orders. A pending paper candidate can contain trading logic without being activated. The CLI --proposal-id <proposal_id> form is appropriate only when the action is still proposal validation/backtest and the documented tool needs it. Do not invent strategy_run_tick(proposal_id=..., dry_run=true): the active-package tick route is a different operation.

## Evidence and backtest interpretation

Historical-data scopes requiring special care include meme, wallet, onchain, polymarket and prediction-market. Use the actual provider/event history or a clearly labelled strategy-local replay. Never present mock, random, synthetic or placeholder candles as performance evidence. Branch/unit fixtures and stubbed Agent decisions demonstrate behavior only; they do not establish returns or real model execution. Do not quietly rewrite a hard-to-replay market into trend/scalping or a CEX proxy.

If recommended_coverage_ok is false but real candles were used, call it an attempted short-window real-data backtest; do not call the standard backtest unavailable. Paper review can continue when the runtime explicitly allows it; shadow/live progression still requires the explicit operator approval and gates returned by the runtime. Honor paper_review_allowed and review_gate; do not override them with a manual FAIL/no_trades rejection. If strategy_backtest returns ok:true, describe what actually completed (standard OHLCV where applicable), not a broader claim.

When no durable replay source exists, state the gap and required operator-approval waiver before promotion. A waiver is not a passed backtest. Observation-only strategies intentionally place zero trades: test signal/trigger correctness, dedupe, no-model/no-order branches and output instead of claiming a profitable trading backtest. Do not bypass any backtest_required or next_required_action returned by submission.

## Trading SDK boundaries

Use `from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask`. Do not import from nerya.sdk, nerya.strategy, internal API/tool modules, exchange or LLM provider libraries. No raw HTTP, environment secrets, process spawning or private file writes. SDK access is scoped and audited.

Positions come from ctx.portfolio.positions(market), a list. Iterate/select a row; never call .get on the whole list. Accounts come from ctx.config.accounts; there is no ctx.account_id. Trades, when requested and permitted, go through ctx.trading.submit_intent/open_position/close_position. There is no StrategyResult.order, StrategyResult.dispatch or StrategyResult.batch. ResultBuilder factories take keyword arguments. Never multiply raw _pct fields by 100.

Use StrategyAgentTask.dispatch/skip/error for Agent flows. dispatch(context={...}) is supported for actual upstream values. Never wrap it in ctx.result.agent_task, and use a small object session_key, not a string. ctx.session_key does not exist and session routing is not durable deduplication; use ctx.state. Keep low-level trading action names out of user-facing docs and execution briefs; profile allowed_tools is the machine policy, not the operator explanation.

For custom wallet, meme, news, social or prediction-market strategies, stop discovery when the actual market and provider evidence are sufficient. Do not search unrelated histories, use shell/glob or read arbitrary workspace trees to invent SDK signatures. Author at the returned candidate paths, validate, and submit if the user requested an implemented proposal. Preserve preferred_provider and mark unavailable capabilities not ready.
