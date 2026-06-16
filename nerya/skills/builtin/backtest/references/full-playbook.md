---
name: backtest
description: "Replay Nerya strategy packages over historical candles before promotion; emits CSV, metrics.json, report.md, chart.json, and an interactive equity-curve + price chart_block the chat splices into the agent's response."
---

# Backtest Skill

Use this skill whenever a strategy has passed static validation and historical
OHLCV data is available. Backtesting is the promotion gate before moving a
strategy toward paper/shadow/live operation.

## Non-OHLCV / hard-to-replay markets

Prediction markets, on-chain meme coins, thin DEX pools, and event-driven
strategies often cannot be replayed honestly by the default candle engine. Do
not present that as "no backtest" when durable historical data exists. The
agent must still write the simplest useful replay script it can, run it, and
show the result. If no real historical/event data is available, report the
backtest as blocked; do not fabricate a price series.

Required fallback when the default CLI cannot produce a meaningful run:

1. Create a strategy-local script such as `tests/test_main.py`,
   `scripts/custom_replay.py`, or `backtests/custom_replay.py`.
2. Use whatever durable history is available: sampled OHLCV, swaps, pool
   reserves, orderbook snapshots, settlement history, headline/event JSONL,
   Polymarket trades, or a hand-built fixture from observed historical facts.
   Never use random, synthetic, generated, or placeholder candles as evidence
   for performance when `allow_mock=false`.
3. Stub surfaces that cannot be replayed, for example LLM/team/news verdicts,
   with explicit fixture payloads.
4. Emit at least:
   - `custom_replay_result.json`
   - `custom_replay_report.md`
   - a trades/signals CSV if the strategy can produce entries/exits
5. State the limitations plainly: missing liquidity, survivorship bias,
   coarse timestamps, stubbed LLM/team decisions, no intra-block ordering, or
   no historical YES/NO ladder.

The fallback can be simple, but it must be executable and must demonstrate the
strategy logic over historical data or fixtures derived from real events. A
paper-trade plan alone is not enough unless the user explicitly accepts "no
replay possible" after seeing the attempted script and why it cannot run.

## Freeform and short-window runs

When a strategy package contains `backtests/research_backtest.py`,
`backtests/freeform_backtest.py`, or another supported freeform script, run
`strategy_backtest` normally. The harness executes the strategy-local script
first and accepts the run when it emits capital-curve and trade-detail
artifacts, such as `equity.csv` plus `trades.csv`, or `result.json` with
`equity_curve` plus `trades`.

Do not force a timeframe, candle window, or stock OHLCV template in the
freeform lane. The script may use the provider SDK or data source the strategy
actually depends on; report limitations honestly.

If real OHLCV candles exist but the loaded window is short, run and report the
backtest anyway. The default asks for roughly six months of history for general
CEX strategies, but short-lived meme/on-chain/DEX pool strategies use a
one-week requested window unless an explicit config says otherwise. Always
accept the maximum real window the source can return.
For meme coins, a one-week or shorter window can still cover a meaningful
launch-to-drawdown lifecycle, so describe it as a short-window real-data
backtest with lower confidence. Do not say "standard backtest unavailable"
solely because `recommended_coverage_ok=false`.

For meme/on-chain markets, custom/event replay is often the preferred evidence
path: reserve, swap, holder, top-trader, wallet-flow, and signal histories are
more representative than generic OHLCV candles. If no durable replay data
exists, return a blocked result and state that promotion/live progression needs
an explicit operator-approved standard-backtest waiver; never label the waiver
as a passed standard backtest.

For meme smart-money strategies, a no-trades standard OHLCV replay can be an
engine-fit limit for agent_task/custom-data logic. Do not rewrite the thesis
into trend/scalping just to force standard trades. Paper review can continue
when validation passes and real K-line, custom/event replay, or freeform SDK
backtest evidence exists; shadow/live progression still requires explicit
operator approval.

If the backtest output includes `paper_review_allowed` or `review_gate`, copy
that gate conclusion. Do not override it with a manual FAIL/no_trades
rejection. If `kind=freeform_backtest`, summarize it as a completed
strategy-local SDK research backtest with capital-curve and trade-detail
artifacts, not as a standard OHLCV replay. If `strategy_backtest` returns
`ok:true`, never describe the backtest as unavailable, impossible, or not
applicable. If a completed run has zero trades, explain that the replay
completed but the decision logic did not produce simulated fills.

When summarising tool output, use `metrics_display` when present. Raw metric
keys ending in `_pct` are already percentage points: `0.0274` means `0.0274%`,
not `2.74%`. If `operator_summary_text` or `operator_summary` is present, copy
those display strings for the user summary. Never multiply raw `_pct` fields
by 100 or move the decimal.

## Default CLI

```bash
python -m nerya.skills.builtin.backtest.scripts.backtest_run --strategy-id <id> --preset default
```

For in-flight strategy proposals that have not been promoted yet, use:

```bash
python -m nerya.skills.builtin.backtest.scripts.backtest_run --proposal-id <proposal_id> --preset default
```

The run creates:

```text
<workspace>/strategies/<id>/backtests/<YYYYMMDD_HHMMSS>/
  config.yml
  ohlcv_indicators_portfolio.csv
  trades.csv
  analysis_by_reason.csv
  rejected_signals.csv
  metrics.json
  report.md
  chart.json
  logs/engine.log
```

After the command finishes, read `report.md` and summarize only the verdict,
`total_return_pct`, `max_drawdown_pct`, `sharpe_ratio`, and
`total_missed_profit_pct`.

## Programmatic path

Tests can call the same engine in-process:

```python
stats = ctx.backtest_replay(run, markets=["BINANCE:BTCUSDT"], window_days=180, tf="1h")
```

No files are written unless `artefacts_dir=...` is passed.

## Config

Use `references/config.default.yml` for the default preset and
`references/config_schema.md` for field meanings. Custom runs pass:

```bash
python -m nerya.skills.builtin.backtest.scripts.backtest_run --strategy-id <id> --config path/to/config.yml
```

Use `references/custom_replay_template.md` when the market cannot use the
standard OHLCV engine.

## Verdict

- `PASS`: drawdown stays within policy, missed profit is controlled, and upside
  capture is acceptable.
- `WARN`: one gate is weak but return beats benchmark.
- `FAIL`: negative return or risk breach.

Live promotion should not proceed automatically on `WARN` or `FAIL`.
