<!-- nerya-skill-frontmatter-start -->
---
name: news_social
description: "Use first for latest/current economy/finance/market news or crypto headlines; RSS first pass before broad web search."
version: 0.1.0
license: MIT
author: Nerya
tags:
  - news
  - rss
  - research
triggers:
  - 热门经济新闻
  - 当前财经新闻
  - market news
  - latest finance headlines
permissions:
  - network
---
<!-- nerya-skill-frontmatter-end -->

# News Social

Use when the task needs current headlines, RSS-backed news context, or
lightweight news/social evidence. Prefer this before broad web search
when built-in feeds cover the topic.

## Flow

CALL native `script_run` for the fast RSS pass:
`skill_id="news_social"`, `name="recent_news.py"`, `args=["--json", "..."]`.
Do not invoke this script through `run_shell`.

IF the user gives a freshness window, pass it as `lookback_hours` when
possible. If the script returns `time_filter`, only summarize returned
`items`; do not fill the window with older or timestamp-missing headlines.

USE Yahoo Finance RSS for broad equity, market, economy topics, and
explicit tickers.

USE CoinDesk, Cointelegraph, and BitcoinMagazine RSS for crypto topics.

FOR broad hot/popular economy or finance roundups:
RUN `web_search_fetch` after the RSS pass with a current-date query for
source diversity and accessible article content.

THEN use `research` / `web_search_fetch` only when RSS coverage is too
narrow, the user needs full article content, or source diversity is
required.

## Custom Feed Registration

When the operator asks to add or persist a custom RSS/news feed URL:
USE `evolve_core_config_patch` with target `news_feeds.yml`.
Do not inspect runtime source files, and do not mutate `news_feeds.yml`
with `write_file`, `edit_file`, or `run_shell`.

## Script

`scripts/recent_news.py` accepts JSON through `--json`, `--payload-file`,
or stdin. Prefer `script_run` with `--json`. Example payloads:

```json
{"topic":"热门财经新闻","limit":20}
```

```json
{"topic":"加密新闻","lookback_hours":3,"limit":20}
```

```json
{"sources":["yahoo_finance_rss"],"tickers":["AAPL","NVDA"],"limit":10}
```

## Lazy References

- `references/full-playbook.md` for custom feed proposal details and the
  full output contract.
