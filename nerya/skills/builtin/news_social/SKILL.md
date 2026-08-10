<!-- nerya-skill-frontmatter-start -->
---
name: news_social
description: "Use for RSS-specific current news or social evidence, source-by-source headline review, and custom feed registration; general market research should use its own bounded research flow."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# News Social

Use when the task specifically needs RSS-backed headlines, source-by-source
news context, or custom feed handling.

## Flow

CALL `research_run` once with the full topic, requested sources, and freshness
window. Consume its capture and finish instead of repeating raw searches.

IF the user gives a freshness window, pass it as `lookback_hours` when
possible. If the script returns `time_filter`, only summarize returned
`items`; do not fill the window with older or timestamp-missing headlines.

USE Yahoo Finance RSS for broad equity, market, economy topics, and
explicit tickers.

USE CoinDesk, Cointelegraph, and BitcoinMagazine RSS for crypto topics.

Use `web_fetch` only for an exact article URL missing from the returned capture.

## Custom Feed Registration

When the operator asks to add or persist a custom RSS/news feed URL:
USE `evolve_core_config_patch` with target `news_feeds.yml`.
Do not inspect runtime source files, and do not mutate `news_feeds.yml`
with `write_file`, `edit_file`, or `run_shell`.

## Lazy References

- `references/full-playbook.md` for explicit RSS script use, custom feed
  proposal details, and the full output contract.
