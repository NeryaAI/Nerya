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

IF the strategy package contains `backtests/research_backtest.py`,
`backtests/freeform_backtest.py`, or another supported freeform backtest script:
RUN `strategy_backtest` as usual. LOAD `references/full-playbook.md` for
freeform evidence rules.

IF real OHLCV candles exist but the loaded window is short:
RUN and report the backtest anyway. State lower confidence instead of claiming
the run is unavailable only because coverage is short.

IF normal OHLCV replay is impossible because the strategy code crashes:
REPAIR the strategy proposal so it uses the StrategyContext backtest
contract, then rerun `strategy_backtest`.

IF normal OHLCV replay is impossible because the market is not honestly
replayable as candles:
LOAD `references/custom_replay_template.md` and
`references/full-playbook.md`. Build an honest custom replay from durable
historical data with explicit limitations.

READ `report.md`, `metrics.json`, and chart artifacts.
REPORT verdict, risks, and whether promotion is blocked.
When summarising tool output, use `operator_summary_text`,
`operator_summary`, or `metrics_display` when present.

Never present random, synthetic, generated, or placeholder price series as a
successful backtest. When `allow_mock=false`, a backtest is only acceptable if
the data source is real historical market/event data. If that data is not
available, say the backtest is blocked instead of fabricating candles. A
standard-backtest waiver is an operator approval record, not performance
evidence.

## Scripts

- `scripts/backtest_run.py` for strategy/proposal replay.
- `scripts/freeform_run.py` for strategy-local SDK/freeform replay.
- `scripts/render_chart.py` when only chart rendering is needed.

## Lazy References

- `references/full-playbook.md` for the original detailed playbook.
- `references/config_schema.md` and `references/config.default.yml`.
- `references/metrics_glossary.md`.
- `references/chart_schema.md`.
- `references/custom_replay_template.md`.
