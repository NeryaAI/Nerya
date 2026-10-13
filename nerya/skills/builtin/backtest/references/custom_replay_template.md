# Custom Replay Template

Use this when a strategy cannot be honestly replayed with the default OHLCV
engine, especially prediction markets, meme coins, DEX pools, and event-driven
strategies.

## Minimum Script Contract

Place one of these files in the strategy package:

- `backtests/research_backtest.py`, `backtests/freeform_backtest.py`, or
  `backtests/custom_backtest.py`.
- `scripts/research_backtest.py`, `scripts/freeform_backtest.py`,
  `scripts/custom_backtest.py`, or `scripts/custom_replay.py`.

`tests/test_main.py` is a bounded test, not an executable backtest entrypoint.
Run the saved package with native `strategy_backtest(engine="freeform",
allow_mock=false)` or auto selection. Do not invent a `backtests/custom_replay.py`
entrypoint; the runner does not discover it.

Use the replay-only public facade:

    from nerya.strategies.research import manifest, entrypoint, fetch_candles
    from nerya.strategies.research import write_json, write_text, write_csv
    from nerya.strategies.research import validate_financial_request, validate_financial_call

Only recognized replay entrypoints can import this module; it also checks the
runner context at execution. manifest() returns the saved configuration;
entrypoint() returns its actual saved callable. fetch_candles(market, *,
interval, start, end, count) takes integer epoch seconds, permits only a saved
market and bounded real public history, and never enables mock data. write_*
functions write relative artifact names confined to the replay output directory.
Do not read os.environ, load internal config, import nerya.data, use raw HTTP,
spawn subprocesses or read signer credentials in authored replay code.

Financial replay doubles must call validate_financial_request(request) and
validate_financial_call(method, *args, **kwargs) before simulating any prepare,
execute, get, reconcile or account_state result. These perform real contract
checks without credentials or side effects. A correctly typed simulated result
is still not an account fill. Use actual ResultBuilder for script returns.

The script should:

1. Load historical or fixture data from a checked-in JSON/CSV/JSONL file, or
   fetch a bounded public history when that is safe and deterministic enough.
   Fixtures must be derived from observed historical facts. Do not use random,
   synthetic, generated, or placeholder candles as evidence for performance
   when `allow_mock=false`.
2. Execute the saved strategy entrypoint/helpers with historically bounded
   inputs and real SDK result types. Model unavailable execution surfaces
   explicitly; permissive fake methods cannot validate SDK compatibility.
3. Step through events/bars/swaps in timestamp order.
4. Capture each decision and any simulated fill.
5. Write `result.json`, `report.md`, `equity.csv` (ts/equity rows) and
   `trades.csv` (trade rows or a header for zero trades) under
   `NERYA_BACKTEST_OUT_DIR`. The runner also supplies `NERYA_STRATEGY_ROOT` and
   `NERYA_WORKSPACE`; the research facade resolves them, not authored env reads.
   The financial artifact contract requires nonempty equity rows. If those
   cannot be established honestly, keep the backtest blocked and report
   bounded signal/branch checks separately; do not invent an equity curve.
   Preserve source, network, data hashes, package hash and requested/actual
   window provenance with the artifacts.

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
