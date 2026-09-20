# news_interpreter

Classify supplied or freshly collected news into alpha, noise and risk, and
identify affected tickers. Load `research` and its `references/news-and-social.md`
when collection, RSS or freshness rules are needed. Use actual exposed tools;
never invent dotted skill actions or shell-based collection routes.

Return JSON with `items`, `evidence`, `signals`, `uncertainty`, `gaps`, and
`done`. Each item includes headline, tickers, category, one-sentence summary,
source and timestamp. Distinguish publication from event time, deduplicate
syndicated coverage and mark unverified social claims. When no usable story
exists, return an empty items array with the precise gap, not fabricated news.
