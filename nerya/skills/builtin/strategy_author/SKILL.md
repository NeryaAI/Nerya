<!-- nerya-skill-frontmatter-start -->
---
name: strategy_author
description: "Use to author, scaffold, validate, refactor, or backtest a Nerya strategy package before proposal promotion."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Strategy Author

Use when the user wants a trading strategy created or changed.

## Flow

DEFINE markets, accounts, timeframe, trigger, risk limits, and goal.
If the operator asks you to create/generate/build a strategy and backtest it
but leaves those details open, do not reply with a questionnaire. Choose
conservative defaults yourself for proposal/paper/backtest work: inherit the
session market context when present; otherwise use connector discovery and
pick a liquid market with real historical candles; use a paper account,
non-live mode, modest sizing, and a default preset backtest. Document those
assumptions in the proposal rationale or `strategy.md`. Ask the operator only
when the missing choice would make the action live, destructive, irreversible,
or honestly impossible.
For ordinary or generic strategy prompts, do not override `files.main.py`.
Let `strategy_generate_proposal` use its stock StrategyContext-compatible
template. Only provide inline file overrides when the operator asks for
behavior that the stock archetype cannot express.
When the requested logic needs custom data, provider-specific reads,
wallet/chain-native evidence, non-standard replay rules, or explicitly avoids
one of the stock archetypes, that means the stock archetype cannot express it:
draft the package files yourself with the Nerya strategy SDK and pass them via
`files`. In that lane, `strategy_generate_proposal` is only the proposal
packager/validator; do not rely on its generated `main.py` for the strategy
logic.
SELECT archetype: scalping, trend, news, sentiment, on-chain, rotation,
mean reversion, or custom.
GENERATE strategy package as a proposal, not direct workspace mutation.
VALIDATE static contract.
BACKTEST with `strategy_backtest({"proposal_id": "...", "preset": "default"})`
after validation unless the user explicitly skips it. The default preset
uses a 45-day replay window so the effective run remains longer than one
month even when a provider lacks the latest partial day.
SUMMARISE results, blocked risks, and next operator action.

## Market context inheritance

Treat the active chat/session market scope as part of the operator brief.
This is advisory context for your judgment, not a hard router. Preserve the
market scope already established by prior turns unless the operator
explicitly changes domains. Examples are examples, not defaults: do not
substitute BTC, crypto, futures, or another template market just because an
archetype mentions it.

Preserve the market scope that the session has already established.

Before selecting `markets`, write the assumption into `strategy.md` or the
proposal rationale, for example: `Market scope assumption: prior session
context is China-listed AI companies, so this proposal keeps an equity
market scope.`

## Backtest gate

After `strategy_generate_proposal` passes validation, run:

```json
strategy_backtest({"proposal_id": "<proposal_id>", "preset": "default", "allow_mock": false})
```

or from CLI:

```bash
python -m nerya.skills.builtin.backtest.scripts.backtest_run --proposal-id <proposal_id> --preset default
```

If the tool result includes `next_required_action`, follow it immediately;
do not ask another confirmation question first unless the user explicitly
asked to skip or customise the backtest. Only promote after the proposal has
backtest artefacts. For meme/on-chain strategies, an accepted custom/event
replay counts as the backtest artefact. If no standard OHLCV backtest or
durable replay source exists, promotion is allowed only with an explicit
operator-approved standard-backtest waiver; call `strategy_promote` with
`backtest_policy: "flexible_meme"`, `operator_approved: true`, and a
non-empty `approval_note`, and report that the strategy did not pass a
standard backtest.
Use returned `proposal_paths`, `strategy_root`, `metrics_path`, and
`report_path` for follow-up reads. Do not call `strategy_view` or read
`~/.nerya/strategies/<id>` for a proposal that has not been promoted, and do
not regenerate a strategy only because the promoted strategy path is absent.
If `coverage_ok` is false, state that the historical data coverage is
insufficient and do not call the run a valid one-month-plus backtest.
If `strategy_backtest` returns `ok:false` with `reason:no_historical_data`,
for ordinary markets stop and report the data-source gap. For meme/on-chain
markets, switch once to the flexible path: build a custom/event replay from
real wallet, DEX, holder, top-trader, or trade history when possible; if that
is also unavailable, ask for explicit operator approval of the waiver before
promotion. Do not keep regenerating variants, and do not retry with mock,
synthetic, random, or placeholder candles.
For on-chain meme, news, social, Polymarket, or other hard-to-replay markets,
bound the data search: use `connector_list`, `connector_view`, and at most one
real `market_data`/`strategy_backtest` attempt. If no durable historical
OHLCV/event series is available, report the honest data gap. Do not inspect
connector source files or docs repeatedly, and do not request shell just to
keep searching.
For wallet/on-chain strategy authoring, once `connector_list` and `data_api`
have shown a usable provider route, stop discovery and write the SDK proposal.
Do not call shell, glob, or raw file reads to enumerate local connector source
or workspace files just to learn data-source names; the provider catalog and
tool schemas are the source of truth. After proposal generation, read only the
returned `proposal_paths` / artifact paths needed for validation or repair.
For wallet-backed on-chain meme strategies, do not stop at `connector_list`.
First call `data_api(op="call", provider="wallet",
action="capability_catalog", args={"topic":"meme"})` or
`data_api(op="call", provider="wallet", action="meme_strategy_guide")`.
Use the returned `selection.selected_route.call` for historical candles; the
route must follow whichever wallet is installed and logged in. If no wallet is
ready, use the returned GOAT/self-custody fallback only after the install path
is approved/configured through `wallet_install(provider="self_custody",
mode="goat")`, and recommend installing OKX OnchainOS/XAgent/Bitget for richer
meme discovery and risk data.
Use OnchainOS actions such as `token_hot_tokens`, `memepump_tokens`,
`token_report`, `security_token_scan`, `token_holders`, `token_top_trader`,
`token_trades`, and `signal_list` for discovery/enrichment. Use
`market_data.get_candles` with wallet venues like
`OKX_ONCHAIN:<chain>:<token_contract>` for historical replay. Treat swaps,
transfers, signing, bridging, and DeFi investment commands as gated execution
paths only; they must go through the Nerya trading/risk/approval flow, not
through `data_api`.
If the operator says on-chain, DEX, wallet-flow, or chain-native, do not satisfy
that request with CEX proxies such as `binance:DOGEUSDT` unless the operator
explicitly accepts a non-on-chain proxy. A Binance/Coinbase meme pair can be
offered only as a clearly labelled alternative; it is not a valid on-chain
backtest. When no on-chain/DEX historical replay source is available, stop and
say the strategy cannot be honestly standard-backtested with current
connectors; it may enter paper/shadow/live progression only through the
explicit waiver and operator approval gates.

## Backtest-compatible code contract

For simple prompts, prefer the stock templates from `strategy_generate_proposal`
instead of hand-writing `main.py`.

For custom strategies, write `files.main.py` before calling
`strategy_generate_proposal`. Use the Nerya strategy SDK surface:
`StrategyContext`, `StrategyResult`, and when the strategy needs the runtime
Agent to inspect long-tail tools, `StrategyAgentTask`. Strategy code may gather
standard market context with `ctx.market.*`, position/risk context with
`ctx.portfolio` and `ctx.policy`, and then either return a direct
`ctx.result.*`/`ctx.trading.*` result or return `StrategyAgentTask.dispatch`
with a bounded evidence contract for the Agent. The prompt inside an agent task
should describe the required evidence and decision rules; it should not be a
placeholder that asks the Agent to invent the strategy later.

If you do override `main.py`, keep it inside the StrategyContext facade:

- get the market from `ctx.config.markets[0]`
- load candles with `ctx.market.candles(market, timeframe="1h", limit=160)`
  or derived indicators with `ctx.market.features(...)`
- inspect positions with `ctx.portfolio.positions(market)`
- size with `ctx.policy.default_order_usd`, `ctx.portfolio.equity_usd`, or
  explicit policy values
- place entries/exits through `ctx.trading.open_position`,
  `ctx.trading.close_position`, or `ctx.trading.submit_intent`
- return `ctx.result.*` / `StrategyResult`, not raw order-list dictionaries

Do not use the native `market_data` tool schema inside strategy code:
`ctx.market_data.get_candles`, `portfolio.get_account`,
`portfolio.get_positions`, and `ctx.account_id` are not valid package APIs.
They will fail validation/backtest.

When `strategy_backtest(..., allow_mock=false)` fails, repair the proposal and
rerun `strategy_backtest`. Do not replace it with a shell script, random price
series, or synthetic/placeholder candles. A custom replay is acceptable only
when it consumes durable historical data and states its limitations.

When reporting backtest results, use the tool's `metrics_display` values when
available. Raw keys ending in `_pct` are already percentage points:
`0.0274` means `0.0274%`, not `2.74%`.
Never multiply raw `_pct` fields by 100 or move the decimal in the final
operator summary.

## Lazy References

- `references/full-playbook.md` for the full authoring and validation gate.
- `references/scalping_cron.md`
- `references/trend_follow_subagent.md`
- `references/news_track_filter.md`
