# News, RSS and Social Evidence

Use for headline reviews, source-specific feeds, social signals or explicit
freshness windows. Send one `research_run` covering topics, tickers, requested
sources and the time window. Do not repeat its searches after usable evidence
has returned. Fetch an exact article URL only for a material missing detail.

Honor `lookback_hours` and returned `time_filter`: summarize only returned
items in the window, never fill gaps with older or timestamp-missing stories.
Separate publication time from the event date. Deduplicate syndicated stories;
identify original reporting, company releases and unattributed claims.
Social volume is not independently verified fundamental evidence.

For explicit RSS collection, legacy `news_social` retains its bundled helper
and `references/full-playbook.md`: inspect that reference through
`Skill(skill="news_social", file="references/full-playbook.md")` to use the
actual script/argument names. Yahoo Finance covers equity/economy feeds;
CoinDesk, Cointelegraph and BitcoinMagazine are existing crypto feed options.
Do not assume an empty feed means no event occurred.

## Persisting a custom feed

A request to register an RSS URL uses `evolve_core_config_patch` targeting
`news_feeds.yml`. This is a proposal; do not mutate the configuration with
`write_file`, `edit_file` or `run_shell`. Do not expose credentials in the URL.
Report proposed versus applied state exactly as returned by the tool.
