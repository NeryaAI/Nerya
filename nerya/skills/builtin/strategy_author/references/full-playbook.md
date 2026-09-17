# Strategy authoring: advanced navigation

The canonical lifecycle is in `../SKILL.md`. For ordinary script, indicator-gated Agent and scheduled Agent strategies, read `workflows.md` once and implement. Do not load all references or unrelated strategy histories.

- `workflows.md`: actual SDK signatures, manifest/card bindings, closed-candle processing, persistent dedupe, schedule and Agent contracts; executable patterns for all three workflow types.
- `specialized-contracts.md`: market inheritance, wallet/on-chain/prediction-market constraints, trading SDK safety, real-data backtests, exact candidate handling and approval gates.
- `scalping_cron.md`, `trend_follow_subagent.md`, `news_track_filter.md`: domain examples only when they match the user's thesis. Examples never override the current SDK/permission contract or silently choose markets.

## Package

Use the `proposal_paths` returned by `strategy_draft_proposal`. The staged package normally contains `strategy.yml`, `main.py`, `strategy.md`, `limits.yml`, `workflow.json`, and `tests/`. Add package-local scripts or `subagents/<name>.agent.md` only when the logic needs them. The runner, not authoring, writes run histories and state. Active `strategies/<id>/` is not a scratch directory.

Scaffold → author real files → validate the edited candidate → submit for review. Only a separately authorized promotion applies the proposal and compiles its schedule. Do not call an active-package tick API with a proposal id or invent a dry_run argument. Do not auto-promote to demonstrate a graph.

## Backtesting

Preserve `backtest_required` and `next_required_action` returned by the runtime. When the user requested a backtest, call `strategy_backtest` with the exact `proposal_id`, documented preset and `allow_mock:false`, then inspect the returned evidence. Use a documented, strategy-local replay when the standard replay does not fit, and label every fixture/stub and data gap. No network unit test or fake Agent decision is performance evidence. A missing provider, zero trades or a pending proposal is not permission to silently replace the thesis, omit gates, or promote.

For observation-only workflows first verify branch behavior and output; zero orders are the requirement, not failure. Model generation, runtime Agent execution, scheduling and approval are separate stages. Report each stage from actual evidence.

## Evolution

Use only the review/evolution fields described in workflows.md and the current manifest schema. Keep tuning independent from the trading/observation schedule, disabled until activation is requested, and approval mandatory. Do not add automatic AI review to a no-AI strategy. Shared accounts, credentials, runtime limits and historical evidence remain protected.
