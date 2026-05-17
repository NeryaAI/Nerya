# Dynamic Chart Recipe

Use this when a coding task needs a chart that no built-in skill already
produces: custom simulator equity curves, regression bands, derived
fundamental series, or other ad-hoc data shapes.

## Flow

IF the chart is already produced by `markets.get_candles`,
`equity_research.fetch_market_data`, or `backtest.render_chart`:
USE that static skill path.

IF the data is custom:
COMPUTE points in a `run_shell` Python step.
PUBLISH a chart artifact through the SDK.
EMIT the chart marker so the kernel splices the chart into chat.

```python
from nerya_sdk import connect
from nerya.charting import line_chart_from_rows

client = connect()
points = compute_my_series(...)
block = line_chart_from_rows(
    [{"time": p["time"], "value": p["value"]} for p in points],
    title="rolling sharpe (60d)",
    skill="agent",
    action="dynamic_code",
    series_name="sharpe",
)
res = client.charts.publish(block)
client.charts.emit_marker(res["chart_id"])
```

`client.charts.publish_and_announce(block)` performs publish plus marker
print in one call.

The marker format is:

```text
@@nerya:chart@@ <id>
```

The chart hook scans `run_shell` stdout for the marker and splices the
chart envelope after the call. Series payloads stay under
`artifacts/charts/<id>.json`; dashboard clients fetch them lazily through
`/charts/get?id=...`.

## Do Not Use

- Do not chart a single scalar or fewer than 5 points.
- Do not re-render an existing skill chart just to change style.
- Do not echo large raw point arrays into the LLM context.
