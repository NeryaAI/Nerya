# Factor Schema

## Top-level fields

```json
{
  "factor_id": "trend.ema_slope.20",
  "name": "EMA 20 slope",
  "family": "moving_average_trend",
  "category": "trend",
  "scope": "universal",
  "version": 1,
  "status": "candidate",
  "formula_path": "strategies/research/ema_slope_20.py",
  "description": "Normalized slope of the 20-period exponential moving average.",
  "data_requirements": ["open", "high", "low", "close", "volume"],
  "required_data": ["ohlcv"],
  "lookback": 20,
  "available_at": "bar_close",
  "direction": "higher_is_bullish",
  "normalization": "rolling_zscore",
  "markets": [],
  "timeframes": [],
  "applicability": {
    "asset_classes": ["crypto"],
    "instrument_types": ["spot", "perpetual"],
    "venues": [],
    "timeframes": ["15m", "1h", "4h"]
  },
  "not_applicable": [],
  "validation": {
    "in_sample": null,
    "out_of_sample": null,
    "walk_forward": null,
    "cost_stress": null,
    "correlation": null
  },
  "validation_by_market": {}
}
```

## Field definitions

| Field | Type | Description |
|-------|------|-------------|
| `factor_id` | string | Unique identifier, namespaced by family. |
| `name` | string | Human-readable name. |
| `family` | string | Factor family (e.g., `moving_average_trend`). |
| `category` | string | Broad category (e.g., `trend`, `mean_reversion`, `volatility`). |
| `scope` | string | `universal` or `market_specific`. |
| `version` | int | Monotonically increasing version. |
| `status` | string | One of: `candidate`, `validated`, `production`, `degraded`, `retired`, `rejected`. |
| `formula_path` | string | Path to the factor implementation (project-relative). |
| `description` | string | What the factor measures and its economic logic. |
| `data_requirements` | list | Required data fields. |
| `required_data` | list | Required data bundles (e.g., `ohlcv`). |
| `lookback` | int | Lookback window in bars. |
| `available_at` | string | When the factor value is available (e.g., `bar_close`). |
| `direction` | string | `higher_is_bullish` or `higher_is_bearish`. |
| `normalization` | string | Normalization method (e.g., `rolling_zscore`, `minmax`). |
| `applicability` | object | Asset classes, instrument types, venues, timeframes. |
| `not_applicable` | list | Market profiles where the factor is not applicable, with reasons. |
| `validation` | object | Validation results (in-sample, OOS, walk-forward, cost-stress, correlation). |
| `validation_by_market` | object | Per-market-profile validation state. |

## Status transitions

```
candidate -> validated -> production -> degraded -> retired
    |                        |
    v                        v
rejected                rejected
```

- `candidate -> validated`: requires passed viability gate + multi-window OOS + cost stress.
- `validated -> production`: requires human approval.
- `production -> degraded`: automatic on decay detection.
- `degraded -> production`: requires re-validation + human approval.
- `degraded -> retired`: requires human approval.
- Any state -> `rejected`: requires failure evidence + human approval.
