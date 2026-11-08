# Interactive research deliverables

Use this contract for investment research, market comparisons, flow analysis
and requested strategy visuals. Source-only briefs, historical-gap findings and
status follow-ups use their requested format without unrelated price charts.
The dashboard already understands `chart_blocks`; do
not change the Agent Loop, prompts, routing or tool schemas to render a chart.

## Required completion sequence

1. Identify the exact market/provider; preserve settlement suffixes such as
   `BYBIT:BTC/USDT:USDT`. Do not guess the venue of a ticker. Fetch only the
   evidence needed for this question using available market/research scripts.
2. Register researched instruments, observed candle snapshots and up to three
   useful studies. Include source/as-of dates, units and calculation notes.
   Obtain relevant news through the research/news scripts; each headline needs
   its original URL, publisher and publication time (empty only when unknown).
3. Write the bounded JSON input below and execute the Research publisher with
   `script_run(skill_id="research", name="publish_visuals.py", args=["--input",
   "<absolute-input-path>", "--workspace", "<current-workspace>"])` when that
   runner is available. Use the actual exposed runner argument schema.
   Alternatively execute the module with the current runtime Python:
   `python -m nerya.skills.builtin.research.scripts.publish_visuals --input <path> --workspace <workspace>`.
4. Preserve the returned JSON on stdout, including `chart_blocks` (never wrap
   it inside an explanatory string or pipe it into a one-line summary). The
   publisher appends existing `@@nerya:chart@@` markers in `announcement` so
   large batches survive the native runner's bounded stdout tail. Keep this
   field unchanged. Bounded publication `notes` preserve the descriptor/news
   and receipt through the existing script-output compactor; heavy points
   remain in chart artifacts. Receipts over 48 KiB fail explicitly: split the
   batch, never drop sources or disable compaction. The existing chart hook
   resolves the persisted series. Verify
   `ok=true`, nonempty `receipt.chart_ids` when charts were requested, and
   `receipt.bulk_verified=true` for workspace-backed charts. Repair invalid
   data once; otherwise report the specific limitation. A saved PNG alone is
   not an interactive deliverable.
5. Finish with the finding, uncertainty and what the charts establish. Each
   market-visual reply publishes its own instruments and relevant charts,
   including follow-ups that request those visuals. The UI attaches a compact name/price/change/
   sparkline card to THAT reply; clicking it opens that reply's snapshot on the
   right. Non-price studies appear under Research charts on the left. Nothing
   goes below the input box. Do not output a fake Markdown image or card HTML.
   The card uses the last observed candle close and labels the displayed-window
   change as period change, not a claimed 24h ticker return. Include the actual
   quote currency, source, data as-of time and observation window. Reused older
   evidence keeps its original timestamp; never relabel it as newly fetched.

## Input shape

Top level: `instruments` (at most 40), `charts` (custom descriptors) and/or
`chart_blocks` (existing market chart results, at most 16 combined).

An instrument is `{market, venue, name?, interval?, news?, news_status?, news_as_of?}`.
Use a qualified market or a bare symbol plus explicit venue. Without a known
provider, publish the instrument without venue and state that candles cannot
be queried. `news_status` is `ok`, `empty`, `unavailable` or `not_requested`.
Never fabricate headlines, URLs, timestamps or market values to fill the UI.

Each custom chart has:

```json
{
  "title": "A meaningful research question · unit",
  "chart_kind": "multi",
  "series": [
    {"name": "Observed series", "type": "line", "data": []}
  ],
  "source": {
    "skill": "research",
    "action": "computed_study",
    "as_of": "ISO-8601 with timezone from the data",
    "artifact_path": "path to the actual input evidence"
  },
  "caption": "Unit, observation window, transformation and missing-data policy",
  "insights": ["Only findings supported by the computed values"],
  "warnings": ["Any material sampling or source limitation"]
}
```

This is a schema illustration, NOT runnable evidence. Replace every placeholder
and supply real nonempty points. A source may use `cite_url` instead of
`artifact_path`; keep original primary-source URLs. Times are strictly
ascending unique Unix **seconds**, numeric and finite, never millisecond values.
Line/area/baseline/histogram points are `{time,value}`. Candlesticks/OHLC bars
are `{time,open,high,low,close,volume?}`. `bar` means OHLC, not a category chart.

For a price chart add `instrument` with its exact market/venue/interval. Only
candlestick charts use this attachment; multi-asset comparisons stay on the
left. Reuse `markets/get_candles.py` `chart_blocks` rather than fetching twice.
The publisher preserves instrument/news context on the emitted blocks, so
streaming, persisted transcript reload and bulk artifact fetching agree.

## Useful advanced studies

- **Flow:** observed inflow, observed outflow and computed net flow in the same
  unit, with net = inflow − outflow. Use a histogram for signed net flow and
  lines for the two components. Explain positive/negative direction. Do not
  confuse exchange inflow with buying pressure, nor exchange flow with ETF
  subscriptions or trade volume. An OHLCV-derived indicator is only a proxy.
- **Comparison:** multiple normalized price or cumulative-return lines with
  a documented common baseline/time window. Never put raw BTC and ETH prices
  on one axis as a return comparison. State missing periods and alignment.
- **Strategy evidence:** observed signal vs outcome, equity/drawdown, factor
  decay or rolling risk from an actual completed test. Preserve chronological
  holdout and existing backtest requirements; charts are not a substitute.

Use separate charts for incompatible units. The renderer supports line, area,
baseline, histogram and candles, not arbitrary plotting-library specifications.
Give series distinct names (optionally colors/line styles), keep legends and
source details usable, and do not fabricate a chart when evidence is missing.

## Failure and authenticity

Provider failures must remain visible. Do not silently use another provider,
replace missing values with zero, interpolate undocumented flow observations,
or mark test fixtures as live data. Product acceptance screenshots and real-Agent
checks must use live-fetched real observations, never mock sessions, fabricated
news or generated prices. Isolated unit tests may still use labelled fixtures;
those do not prove real Agent or real-source execution. No order, schedule,
strategy activation or model configuration change is part of publication.
