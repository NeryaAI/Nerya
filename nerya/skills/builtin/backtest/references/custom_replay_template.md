# Custom Replay Template

Use this when a strategy cannot be honestly replayed with the default OHLCV
engine, especially prediction markets, meme coins, DEX pools, and event-driven
strategies.

## Minimum Script Contract

Place one of these files in the strategy package:

- `tests/test_main.py` for pytest replay smoke.
- `scripts/custom_replay.py` for operator-run replay.
- `backtests/custom_replay.py` for a strategy-local one-off replay.

The script should:

1. Load historical or fixture data from a checked-in JSON/CSV/JSONL file, or
   fetch a bounded public history when that is safe and deterministic enough.
   Fixtures must be derived from observed historical facts. Do not use random,
   synthetic, generated, or placeholder candles as evidence for performance
   when `allow_mock=false`.
2. Build a lightweight fake context that exposes only the surfaces the strategy
   uses.
3. Step through events/bars/swaps in timestamp order.
4. Capture each decision and any simulated fill.
5. Write `custom_replay_result.json` and `custom_replay_report.md`.

## Result Shape

```json
{
  "ok": true,
  "strategy_id": "example",
  "replay_kind": "event_fixture|swap_history|settlement_history|ohlcv_proxy",
  "window": {"start": "...", "end": "..."},
  "events_seen": 0,
  "signals": 0,
  "simulated_trades": 0,
  "final_equity_usd": null,
  "limitations": ["no historical orderbook ladder"]
}
```

## Report Sections

```markdown
# Custom Replay - <strategy_id>

## What was replayed
## What was stubbed
## Decisions / trades
## Metrics available
## Limitations
## Whether this is enough to promote
```

## Guidance

- Prefer a crude executable replay over a polished paragraph claiming the
  market is hard to backtest.
- Do not fabricate precision. If fills are unknown, report signals only.
- If real historical/event data is unavailable, mark the replay blocked rather
  than inventing data.
- For meme coins, a reserve/swap replay is better than a generic CEX candle
  proxy.
- For meme smart-money strategies, replay wallet labels, top-trader flow,
  holder concentration, liquidity changes, swap history, and token security
  snapshots when those histories are available. If only current snapshots are
  available, mark the replay blocked or signal-only instead of inventing fills.
- For prediction markets, settlement/outcome history plus event fixtures is
  better than pretending candles capture the information edge.
- If this replay cannot be built from durable historical data, the operator may
  still approve a standard-backtest waiver for promotion/live progression, but
  the report must say this is a waiver rather than performance evidence.
