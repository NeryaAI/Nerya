<!-- nerya-skill-frontmatter-start -->
---
name: strategy_author
description: "Use to author, validate, refactor, or backtest a Nerya strategy package before proposal promotion."
version: 0.2.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Strategy Author
Use when the user wants a trading strategy created, changed, validated, or reviewed.

## Flow
DEFINE markets, accounts, timeframe, trigger, risk limits, and goal. When a create/backtest request leaves details open, choose conservative paper/proposal defaults unless the missing choice would make the action live, destructive, irreversible, or honestly impossible.
IF the operator asks for a draft/proposal scaffold only, or explicitly says not
to edit, submit, promote, run, or trade, call `strategy_draft_proposal` once,
report its returned proposal paths and validation result, and stop. Do not
follow its implementation `next_steps` in that turn.
KEEP every operator-named venue, market, and strategy concept verbatim in the package metadata: the strategy id/title and `markets` list must mention each requested venue (for example a Binance+Aster cash-and-carry request keeps both `binance` and `aster` plus the cash-and-carry/basis wording). If a requested venue has no usable provider yet, keep it in metadata, mark that leg `not ready`, and say so — do not silently swap in a different venue.
SELECT an archetype only when it fits: scalping, trend, news, sentiment, on-chain, rotation, mean reversion, prediction-market, or custom.
SCAFFOLD the package as a draft proposal with `strategy_draft_proposal` (new strategy) — or `strategy_draft_proposal` with `from_strategy_id` to iterate an existing promoted strategy. This stages template/seed files under the proposal's `after/strategies/<id>/` tree and returns `proposal_id` + `proposal_paths`; it does NOT enter the pending-review queue and writes no inline code.
AUTHOR the strategy by editing the staged files with `read_file` + `edit_file` / `write_file` at the returned `proposal_paths`. Read the file-format reference below before writing each file. This is the implementation step — the scaffold templates are only a starting point and will not encode the user's logic on their own.
VALIDATE with `strategy_validate({"proposal_id":"<proposal_id>"})`; fix every blocker by editing the staged files and re-validating until `ok=true`.
SUBMIT with `strategy_submit_proposal({"proposal_id":"<proposal_id>"})` once validation is clean; only then does the package move from `draft` into the pending-review queue for operator approval.
BACKTEST after submission with `strategy_backtest({"proposal_id":"<proposal_id>","preset":"default","allow_mock":false})`.
SUMMARISE proposal id, validation/backtest evidence, blocked risks, and the next operator action. Do not promote, approve, paper, shadow, or live trade unless the operator explicitly asks for that gate.

## Boundaries
Use to author SDK strategy code through proposal-aware tools, not direct workspace mutation.
Scaffold with `strategy_draft_proposal`, then author the package files with `read_file`, `edit_file`, and `write_file` against the returned `proposal_paths` under `evolution/proposals/<id>/after/strategies/<id>/`. Do NOT write into the live `strategies/<id>/` tree (that path is proposal-only — the workspace guard refuses it), and do not stage a package in a temporary directory or under `~/.nerya`.

### Market context inheritance
Treat active session market scope as advisory context for your judgment, not a hard router.
Preserve the market scope that the session has already established unless the operator changes domains.
Examples are examples, not defaults. State the Market scope assumption when you inherit context.

For hard-to-replay markets, use real provider/event evidence or a strategy-local freeform backtest. Never use mock, random, synthetic, or placeholder candles as performance evidence.

Hard-to-replay scopes include: - meme; - wallet; - onchain; - polymarket; - prediction-market.

If no durable replay source exists, report the data gap and the explicit operator-approval waiver needed before promotion. A waiver is not a passed backtest.

When the operator names a `prp_*` proposal id, Resolve and operate on the exact proposal first.
Use returned proposal_paths, pass `proposal_id` into validation/backtest calls, and prefer `strategy_backtest({"proposal_id":"<proposal_id>","preset":"default","allow_mock":false})`.
Use raw paths or CLI forms such as `--proposal-id <proposal_id>` only when the action is still proposal validation and the proposal-aware tool asks for that evidence.
Do not substitute a promoted `strategy_id` for a proposal id.

Promotion changes the workspace. Do not promote, approve, paper, shadow, or live trade unless the operator explicitly asks for that gate.

## Critical Contracts
If `recommended_coverage_ok` is false but real candles were used, call it an attempted short-window real-data backtest. Do not call the standard backtest unavailable, do not rewrite the thesis into trend/scalping, and Paper review can continue; Shadow/live progression still requires explicit operator approval.
If a result returns `paper_review_allowed` or a `review_gate`, Do not override it with a manual FAIL/no_trades rejection. If `strategy_backtest` returns `ok:true`, call it completed standard OHLCV when appropriate.
Do not treat reason:no_historical_data, a promoted strategy path is absent, or zero trades as permission to regenerate a strategy only because promotion has not occurred.
For missing low-risk details, do not reply with a questionnaire; choose non-live mode, modest sizing, and do not edit `main.py` away from the requested thesis unless custom evidence requires it.
For custom strategies, author `main.py` by editing the staged file; draft the package files yourself with the Nerya strategy SDK when there is prediction-market/Polymarket evidence. `strategy_submit_proposal` only validates and queues the package — it does not write your strategy logic.
SDK notes: Use exactly `from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask`; do not import from nerya.sdk, do not import from nerya.strategy, and do not guess private submodules. Do not call StrategyResult.order. Do not call StrategyResult.dispatch. Do not call StrategyResult.batch. Return ctx.result.hold/skip/ok/error for terminal outcomes, call ctx.trading.submit_intent/open_position/close_position for trades, and use StrategyAgentTask.dispatch/skip/error for Agent-decision flows. Read the configured account via ctx.config.accounts[0] — there is no ctx.account_id. Read positions via ctx.portfolio.positions(market): it returns a list, so iterate the rows or select one before reading fields — never call .get on it. Never pass `context=`, `session_key` must be a small object not a string, Never wrap the task with `ctx.result.agent_task`, use StrategyAgentTask, and Never multiply raw `_pct` fields by 100.
For on-chain meme, news, social, or wallet strategies, do not request shell just to inspect providers; stop discovery and write the SDK proposal. Do not call shell, glob, or raw file reads once the efficient evidence boundary is met; author the SDK strategy package immediately by editing the staged files.
Continue until a `strategy_submit_proposal` call for a validated SDK package exists for custom hard-to-replay scopes; preserve preferred_provider and mark not ready instead of silently substituting another provider.
Wallet Meme Quick Path: when selection.mode` is `wallet_binding` and `market_data` already returned the exact chain:token, do not install a fallback; rely on the runtime scanner.
Use execution_mode: "agent" when the strategy needs Agent decisions.
On-chain means on-chain: do not satisfy
that request with CEX proxies; generic chain markets are not a valid on-chain
backtest.
Do not copy those low-level action names into StrategyAgentTask` prompts or operator-facing strategy docs.
Do not call
`strategy_promote` during an ordinary proposal/backtest request, and do not set `operator_approved: true` yourself.

## Lazy References

- `references/full-playbook.md` for full package structure, SDK contract, wallet/on-chain rules, proposal-id handling, and backtest repair gates.
- `references/scalping_cron.md`
- `references/trend_follow_subagent.md`
- `references/news_track_filter.md`
