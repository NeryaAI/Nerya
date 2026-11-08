# Historical data and backtest execution contract

## Owners and boundaries

`nerya.data.history_store.HistoryStore` owns normalized, reusable historical
candles. Backtest Skill scripts own download orchestration, coverage checks,
preflight and replay. Native tools and CLI commands are adapters to those scripts.
The Agent Loop remains a generic tool scheduler: no market names, strategies,
retry recipes or backtest rules are added to it.

## Data contract

* Dataset identity is venue-qualified market + exact, case-sensitive timeframe.
  Spot and derivative venues, different markets and timeframes never share rows.
* UTC timestamps are candle OPEN times in seconds; source milliseconds and
  microseconds are normalized before filtering. Requested start is inclusive;
  requested end is exclusive. Only closed candles may be stored for replay.
* SQLite transactions and a unique dataset/timestamp key provide atomic batch
  commits and deduplication. Each successful download segment is durable, even
  when a later segment fails. No external database server or PyArrow is required.
* Existing JSON files with a misleading .parquet extension are read only for
  explicit legacy migration. Their missing provenance is not upgraded to verified
  history. New data is never JSON disguised as Parquet.
* The downloader requests only missing intervals, including leading, trailing
  and internal gaps. Empty/failed downloads do not mark an interval as covered.
  Mock/paper/sample responses never enter the historical store.
* A local-only read never performs network IO. Inventory, coverage, row hashes,
  source metadata and missing ranges are queryable without starting a strategy.
* A short response or a deadline is not evidence that an exchange lacks history.
  Partial downloads are explicitly partial and can be retried from durable rows.

## Replay contract

1. Validate target, configuration, source syntax/imports and known SDK surfaces.
2. Prepare historical data independently of strategy execution.
3. Verify requested timeframes, warmup, per-market boundaries and gaps. The default
   `allow_partial` policy retains gaps in the report; select `strict` when the
   study requires full coverage. Do not treat exchange closures as gaps in a 24/7 market.
4. Bind execution to a source revision and the exact loaded dataset hashes.
5. Execute with bounded cancellation/timeout checks and publish phase progress.
6. Persist either completed metrics/report or a structured failure receipt. Never
   leave a config-only directory looking like a successful run. Never remove
   stop-loss/take-profit logic just to make a test pass.

Execution success, coverage, strategy performance and real Agent model execution
are separate facts. A profitable short-window run is not a full-year result;
an observation replay is not an Agent trading backtest. Downloads do not activate
strategies, schedulers or account orders.

## Execution platform

Candidate smoke tests and behavioral tests, strategy-isolated replay, and freeform
replay use macOS Seatbelt (`sandbox-exec`). Without that backend these workers
return unavailable; they do not execute candidate code on the host as a fallback.
A successful static inspection or saved draft is not a passing execution receipt.
