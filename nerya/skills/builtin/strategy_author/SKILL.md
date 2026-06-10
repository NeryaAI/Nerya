<!-- nerya-skill-frontmatter-start -->
---
name: strategy_author
description: "Use to author, validate, refactor, or backtest a Nerya strategy package before proposal promotion."
version: 0.1.0
license: MIT
author: Nerya
tags:
  - strategy
  - trading
  - backtest
  - sdk
  - onchain
  - polymarket
  - custom-data
---
<!-- nerya-skill-frontmatter-end -->

# Strategy Author
Use when the user wants a trading strategy created, changed, validated, or reviewed.

## Flow
DEFINE markets, accounts, timeframe, trigger, risk limits, and goal. When a create/backtest request leaves details open, choose conservative paper/proposal defaults unless the missing choice would make the action live, destructive, irreversible, or honestly impossible.
KEEP every operator-named venue, market, and strategy concept verbatim in the package metadata: the strategy id/title and `markets` list must mention each requested venue (for example a Binance+Aster cash-and-carry request keeps both `binance` and `aster` plus the cash-and-carry/basis wording). If a requested venue has no usable provider yet, keep it in metadata, mark that leg `not ready`, and say so — do not silently swap in a different venue.
SELECT an archetype only when it fits: scalping, trend, news, sentiment, on-chain, rotation, mean reversion, prediction-market, or custom.
GENERATE strategy packages as proposals, not direct workspace mutation. Prefer `strategy_generate_proposal` stock templates for ordinary strategies. Provide inline `files` only when custom data, wallet/chain evidence, prediction-market evidence, or non-standard replay rules make the stock archetype insufficient.
VALIDATE static contract.
BACKTEST after validation with `strategy_backtest({"proposal_id":"<proposal_id>","preset":"default","allow_mock":false})`.
SUMMARISE proposal id, validation/backtest evidence, blocked risks, and the next operator action. Do not promote, approve, paper, shadow, or live trade unless the operator explicitly asks for that gate.

## Boundaries
Use to author SDK strategy code through proposal-aware tools, not direct workspace mutation.
Draft package files inline inside `strategy_generate_proposal.files`. Do not call `write_file`, `edit_file`, `list_dir`, shell, or a temporary workspace directory to stage a strategy package before proposal generation.

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
For missing low-risk details, do not reply with a questionnaire; choose non-live mode, modest sizing, and do not override `files.main.py` unless custom evidence requires it.
For custom strategies, write `files.main.py`; draft the package files yourself with the Nerya strategy SDK when there is prediction-market/Polymarket evidence. `strategy_generate_proposal` is only the proposal packager.
SDK notes: Use exactly `from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask`; do not import from nerya.sdk, do not import from nerya.strategy, and do not guess private submodules. Do not call StrategyResult.order. Do not call StrategyResult.dispatch. Do not call StrategyResult.batch. Return ctx.result.hold/skip/ok/error for terminal outcomes, call ctx.trading.submit_intent/open_position/close_position for trades, and use StrategyAgentTask.dispatch/skip/error for Agent-decision flows. Never pass `context=`, `session_key` must be a small object not a string, Never wrap the task with `ctx.result.agent_task`, use StrategyAgentTask, and Never multiply raw `_pct` fields by 100.
For on-chain meme, news, social, or wallet strategies, do not request shell just to inspect providers; stop discovery and write the SDK proposal. Do not call shell, glob, or raw file reads once the efficient evidence boundary is met; generate the SDK strategy package immediately.
Continue until a `strategy_generate_proposal` call with SDK `files` exists for custom hard-to-replay scopes; preserve preferred_provider and mark not ready instead of silently substituting another provider.
Wallet Meme Quick Path: when selection.mode` is `wallet_binding` and `market_data` already returned the exact chain:token, do not install a fallback; rely on the runtime scanner.
Use execution_mode: "agent_task" when the strategy needs Agent decisions.
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
