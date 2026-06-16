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
| `fee_bps_by_venue` | map | venue defaults | Fee charged on each fill. |
| `slip_bps_by_venue` | map | venue defaults | Simulated adverse slippage. |
| `fill_mode` | string | `entry_current_open__exit_next_open` | V1 locked. |
| `allow_short` | boolean | false | Short support is gated. |
| `max_open_trades` | integer | 1 | Open-position cap. |
| `stake_amount.mode` | string | `unlimited` | `fixed` or `unlimited`. |
| `stake_amount.fixed_usd` | number/null | null | Required when mode is `fixed`. |
| `mock_surfaces.<name>.mode` | string | `error` | `error`, `stub`, or `replay`. |
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
