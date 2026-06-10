# News Social Full Playbook

Use this reference after loading the compact `news_social` skill when the
task needs custom feed configuration, source-specific behavior, or the
script output contract.

## RSS Source Selection

Use Yahoo Finance RSS for broad equity, market, economy topics, and
explicit tickers. Use CoinDesk, Cointelegraph, and BitcoinMagazine RSS
for crypto topics. For broad hot/popular economy or finance roundups, run
`web_search_fetch` after the RSS pass with a current-date query so the
answer has source diversity and verified accessible article content.

If the operator gives a freshness window such as "last 3 hours" or
"最近 3 小时", pass `lookback_hours` to `recent_news.py`. The script can
also infer common hour windows from `topic`, but explicit structured
input is preferred. When `time_filter` is present, treat it as the
source-of-truth boundary: summarize only returned `items`, report if the
window has few or zero results, and do not use older or timestamp-missing
RSS items to make the answer look fuller.

## Custom RSS Source Registration

When the operator asks to add, register, include, or persist a custom
RSS/news feed URL for future news turns, do not inspect runtime source
files and do not mutate `news_feeds.yml` with `write_file`, `edit_file`,
or `run_shell`.

Use `evolve_core_config_patch` with target `news_feeds.yml` so the change
is staged as a reviewable proposal under `evolution/proposals/`. If
`news_feeds.yml` already exists, read it first and preserve existing
entries in `config_after`. If it is missing, propose a full document with
a `feeds` list:

```json
{
  "target": "news_feeds.yml",
  "summary": "Add custom RSS feed https://example.com/feed.xml",
  "config_after": {
    "feeds": [
      {
        "id": "example",
        "url": "https://example.com/feed.xml",
        "type": "rss",
        "enabled": true
      }
    ]
  },
  "rationale": "Operator requested that future news retrieval include this RSS source."
}
```

After the proposal is created, report the proposal id/path and state that
the live feed list changes only after operator review/approval.

## Script Input

Call it through native `script_run` with `skill_id="news_social"` and
`name="recent_news.py"`. Do not invoke it through `run_shell`.
`scripts/recent_news.py` accepts JSON through `--json`,
`--payload-file`, or stdin.

Examples:

```json
{"topic":"热门财经新闻","limit":20}
```

```json
{"sources":["yahoo_finance_rss"],"tickers":["AAPL","NVDA"],"limit":10}
```

```json
{"sources":["crypto_rss"],"topic":"Bitcoin ETF flows","limit":12}
```

```json
{"sources":["crypto_rss"],"topic":"Bitcoin ETF flows","lookback_hours":3,"limit":12}
```

## Output Contract

The script returns JSON:

```json
{
  "ok": true,
  "source": "rss",
  "sources": ["yahoo_finance_rss"],
  "count": 3,
  "items": [
    {
      "source": "yahoo_finance_rss",
      "title": "headline",
      "summary": "short summary",
      "url": "https://...",
      "published_at": "date string",
      "tickers": ["AAPL"]
    }
  ],
  "errors": [],
  "notes": [],
  "time_filter": {
    "lookback_hours": 3.0,
    "since": "2026-06-06T08:30:00+00:00",
    "kept_count": 2,
    "dropped_count": 5
  }
}
```
