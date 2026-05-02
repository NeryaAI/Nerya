# chart.json Schema

```json
{
  "schema_version": "1.0",
  "meta": {},
  "panels": [],
  "summary_cards": [],
  "tables": []
}
```

## Panels

Each panel has `id`, `type`, `title`, and `series`. Series entries use `kind`:

- `candles`: OHLC records with `time/open/high/low/close`.
- `line`: `time/value` records.
- `area`: `time/value` records.
- `markers`: trade markers.

## Tables

Tables are pre-trimmed and shaped as:

```json
{"id": "trades_top10", "columns": ["ts"], "rows": [[123]]}
```

