# Backtest Config Schema

`config.default.yml` is the source of truth. All numeric bps values are basis
points.

| Field | Type | Default | Notes |
|---|---|---:|---|
| `initial_capital_usd` | number | 10000 | Starting cash. |
| `warmup_bars` | integer | 50 | Bars available before decisions start. |
| `min_backtest_days` | integer | 0 | Legacy recommendation field only. Default is no minimum gate; any non-empty real-data window is valid. |
| `window_days` | integer | 180 | General requested replay window. The loader asks for roughly six months by default and uses the maximum real history the source can return when less is available. |
| `short_lived_window_days` | integer | 7 | Requested replay window for short-lived meme/on-chain/DEX pool strategies when no explicit config is passed. |
| `tf` | string | `1h` | Candle interval. |
| `markets` | string[] | [] | Filled from `strategy.yml` by CLI. |
| `indicators` | map | SMA/EMA/RSI/ATR | Period lists by indicator name. |
| `fee_bps` | number | venue defaults | Scalar fee shorthand; explicit `fee_bps_by_venue` entries override it. |
| `slippage_bps` | number | venue defaults | Scalar slippage shorthand; explicit `slip_bps_by_venue` entries override it. |
| `fee_bps_by_venue` | map | venue defaults | Fee charged on each fill. |
| `slip_bps_by_venue` | map | venue defaults | Simulated adverse slippage. |
| `fill_mode` | string | `entry_current_open__exit_next_open` | V1 locked. |
| `allow_short` | boolean | false | Short support is gated. |
| `max_open_trades` | integer | 1 | Open-position cap. |
| `stake_amount.mode` | string | `unlimited` | `fixed` or `unlimited`. |
| `stake_amount.fixed_usd` | number/null | null | Required when mode is `fixed`. |
| `mock_surfaces.<name>.mode` | string | `error` | `error` rejects calls; `stub` returns supplied simulated payloads; `replay` is reserved and raises on use (not implemented). |
| `thresholds.drawdown_episode_min_pct` | number | 3.0 | Episode threshold. |
| `thresholds.missed_profit_min_move_pct` | number | 5.0 | Benchmark move trigger. |
| `thresholds.missed_profit_max_noise_pct` | number | 2.0 | Reserved noise filter. |
| `risk_free_daily` | number | 0.0 | Used for Sharpe/Sortino. |
| `benchmark_mode` | string | `buy_hold_equal_weight` | V1 benchmark. |

## Full Sample

```yaml
initial_capital_usd: 10000
window_days: 180
short_lived_window_days: 7
tf: "1h"
markets: ["BINANCE:BTCUSDT"]
stake_amount:
  mode: fixed
  fixed_usd: 1000
mock_surfaces:
  news: {mode: stub, payload: []}
  llm: {mode: error}
```

## Mock inputs and evidence

For an explicitly requested script-gated Agent replay, use `agent_replay: true`
only when the operator has configured a bounded `backtest.retrospective_replay`
authorization. This separate mode uses the saved candidate entrypoint, one
declared confirmation role, historical prefixes and host-authenticated model
decisions. It does not change `mock_surfaces.subagents.mode` or substitute a
fixed verdict. Missing authorization, exhausted budget, uncertain model calls
or mismatched tape/source stop the run. Keep the same run's artifacts for
reconciliation; a second run is not permission to repeat uncertain calls.

The operator's `nerya.yml` authorization uses `backtest.retrospective_replay`
(disabled by default), separately from candidate backtest settings. It requires
`enabled: true`, an `authorization_id`, pinned `provider` and `model`, and a
`key_ref: vault://...` with `backtest:decision_sign` scope. `max_calls` must be
an integer from 1 to 20; `max_tokens` must be an integer from 1 to 500000.
Zero, negative, non-finite and non-integer budgets are rejected. `deadline` is
an absolute Unix timestamp in the future, at most 1800 seconds from authorization
validation. Only the bounded, pinned OpenAI messages backend is admitted;
fallbacks are disabled.
Optional operator-owned `max_output_tokens` sets each decision's output cap
(integer 1–16384, default 4096). It is included in the request/tape identity
and reserved against the existing total token grant before sending; increasing
it does not reset consumed budget or extend the deadline. Some models, including
GLM-5.3 Flash, require thinking, so a low reasoning setting cannot disable it.
After a truncated response, preserve its charged receipt and let the operator
choose an appropriate cap within the remaining grant before resuming.

Size the explicit token grant for conservative request reservations (UTF-8 input
bytes, tool wire, output cap and safety allowance), not only reported usage.
For example, twenty approximately 18700-token reservations need about 374000;
`max_tokens: 400000` fits this case within the 500000 ceiling. The independent
32000 per-request reservation cap still applies, so this does not guarantee
twenty maximum-size requests or completion of any particular window. An existing
`max_tokens: 100000` grant remains valid and still stops before an unaffordable
request. Reservations remain durable before sending and failures stop closed.
The ceiling does not raise an existing grant: the ledger locks each authorization
ID's policy. A changed grant needs an explicit new operator authorization ID;
preserve the old ledger and reconcile any uncertain calls before another run.

The report labels this `current_model_retrospective_replay`: the model is run
now and may know later events. It is not contemporaneous or live evidence;
`performance_evidence` remains false. Read `decision_replay` and the producer
receipt before describing model calls or results. Ordinary candle replay
retains its existing behavior when `agent_replay` is false (default).

The existing gated surfaces are `news`, `llm`, `subagents`, and `messages`.
`mode: stub` exercises code branches using the supplied payload; it does not
execute a model or authenticate historical news. Match the strategy's response
contract: `subagents.run` expects a mapping, while `run_many` and `news.fetch`
expect lists. An empty payload is not a valid ENTRY verdict when the strategy
requires one; normal strategy checks still apply. Do not bypass those gates to
obtain a passing replay.

Actual attempts are recorded per decision and aggregated into
`replay.mock_surface_calls` as `surface`, `mode`, and `count`, without arguments
or response payloads. The audit log retains the same call facts, including
attempts before a failure. Merely configuring a surface records no calls.
Calls to mocked `llm`, `subagents`, or `news` make `performance_evidence: false`;
candle provenance alone cannot establish the provenance of those inputs.
Even caught unsupported calls cannot become performance evidence. Ordinary
trade-only script replays keep their existing performance flag, and unused
mock configuration does not claim AI execution. Message stubs are recorded
but do not by themselves invalidate trading-input evidence.

Reports disclose the non-performance scope and omit mock payloads from their
public config snapshot. The producer receipt and chart inherit the flag; raw
simulation metrics remain available for inspecting code behavior. Native
`mock_surfaces.<name>.mode: replay` is not an available historical model/news
replay engine and must not be recommended as a working recovery path.

# Local data and isolated execution

Native replay accepts `start_utc` / `end_utc` (UTC, end exclusive),
`data_mode: download|local`, `coverage_policy: strict|allow_partial`,
`allow_timeframe_fallback: false`, `download_timeout_seconds: 300`,
`max_run_seconds: 600`, and `max_bars: 2000000`. `local` is network-free.
Strict coverage and known SDK/configuration checks run before strategy execution.
Unknown settings are rejected. See `references/history-data.md` for download,
warmup, resumption, immutable inputs and failure receipts. These native-engine
settings do not configure a package's custom freeform research script.
