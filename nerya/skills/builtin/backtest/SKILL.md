---
name: backtest
description: "Replay Nerya strategy packages or in-flight strategy proposals over historical candles before promotion."
version: 0.1.0
license: MIT
author: Nerya
---

# Backtest

Use after a strategy validates and before promotion, or when the user
asks whether a strategy would have worked historically.

## Flow

IF strategy exists:
RUN `backtest_run --strategy-id <id> --preset default`.

IF strategy is still a proposal:
RUN `backtest_run --proposal-id <proposal_id> --preset default`.

IF normal OHLCV replay is impossible because the strategy code crashes:
REPAIR the strategy proposal so it uses the StrategyContext backtest
contract, then rerun `strategy_backtest`.

IF normal OHLCV replay is impossible because the market is not honestly
replayable as candles:
LOAD `references/custom_replay_template.md` and build an honest custom replay
from durable historical data with explicit limitations.
For meme/on-chain markets, custom/event replay is the preferred evidence path:
reserve, swap, holder, top-trader, wallet-flow, and signal histories are more
representative than generic OHLCV candles. If no durable replay data exists,
return a blocked result and state that promotion/live progression needs an
explicit operator-approved standard-backtest waiver; never label the waiver as
a passed standard backtest.

READ `report.md`, `metrics.json`, and chart artifacts.
REPORT verdict, risks, and whether promotion is blocked.
When summarising tool output, use `metrics_display` when present. Raw metric
keys ending in `_pct` are already percentage points: `0.0274` means
`0.0274%`, not `2.74%`.
If `operator_summary_text` or `operator_summary` is present, copy those
display strings for the user summary. Never multiply raw `_pct` fields by 100
or move the decimal.

Never present random, synthetic, generated, or placeholder price series as a
successful backtest. When `allow_mock=false`, a backtest is only acceptable if
the data source is real historical market/event data. If that data is not
available, say the backtest is blocked instead of fabricating candles. A
standard-backtest waiver can be used only as an operator approval record for
promotion; it is not performance evidence.

## Scripts

- `scripts/backtest_run.py` for strategy/proposal replay.
- `scripts/render_chart.py` when only chart rendering is needed.

## Lazy References

- `references/full-playbook.md` for the original detailed playbook.
- `references/config_schema.md` and `references/config.default.yml`.
- `references/metrics_glossary.md`.
- `references/chart_schema.md`.
- `references/custom_replay_template.md`.
