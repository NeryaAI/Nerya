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
stats = ctx.backtest_replay(run, markets=["BINANCE:BTCUSDT"], window_days=45, tf="1h")
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
