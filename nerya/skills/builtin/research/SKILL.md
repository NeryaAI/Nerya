<!-- nerya-skill-frontmatter-start -->
---
name: research
description: "Use whenever the agent needs information from outside the workspace \u2014 web search, news, social posts, blog content. Triggers on \"what's happening with\", \"find recent news on\", \"look up\", \"search the web\", \"what are people saying about\", \"fetch this article\", \"summarise this URL\", or any prompt that requires reading current information the codebase does not already contain. Use this before forming a market view or writing a brief; do not invent facts."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Research playbook

This skill captures one rule above all: *cite or do not say*. If a
claim depends on something outside the agent's training data,
research it, name the source, and quote enough of it that the user
can verify.

## When to research vs. answer directly

- **Answer directly** when the question is about general knowledge
  the model is likely to be reliable on (math, well-established
  history, language).
- **Research** when the question depends on:
  - anything that happened in the last ~12 months,
  - a specific company, product, or person,
  - a price, ticker, or asset state,
  - a claim where being wrong is costly.

When in doubt, research. Citing is cheap; hallucinating is not.

## Search strategy

1. **Start narrow.** Specific phrases beat broad keywords. Use the
   user's exact wording first; broaden only if it fails.
2. **Multiple sources.** A single result is a hint, not a fact.
   Confirm at least once before quoting as established.
3. **Read the page, do not summarise the snippet.** Snippets lie.
   Fetch the full content via `scripts/fetch_url.py` before quoting.
   For fast source gathering, use `scripts/search_fetch.py` to search
   and fetch the top results in one bounded pass.
4. **Date-filter.** For anything time-sensitive, filter to the last
   N days/weeks at the search stage; do not rely on yourself to
   notice a stale result.

## Citing

Every claim that depends on a fetched source must include:

- the publication / author,
- the date,
- a direct link.

If a source claims something extraordinary, quote a sentence verbatim
so the user can audit the inference chain.

## Bundled scripts

| Script | Purpose |
|---|---|
| `scripts/web_search.py` | Plain web search → ranked URLs. |
| `scripts/news_search.py` | News-filtered search. |
| `scripts/fetch_url.py` | Pull a URL → readable markdown/text with web-safety checks, HTML extraction, and Jina Reader fallback. |
| `scripts/search_fetch.py` | Search → fetch top N results as markdown documents. |
| `scripts/social_search.py` | Search posts on X / Reddit / Discord. |

Each accepts JSON payload via `--json` / `--payload-file` / stdin.

`fetch_url.py` returns both `markdown` and the legacy-compatible `text`
field. It tries local extraction first (`trafilatura`, then
`markdownify`, then stdlib text stripping). If the direct fetch is
blocked, too thin, or low quality, it can fall back to Jina Reader via
`https://r.jina.ai/<url>`. The result records `fetch_method`,
`fallback_errors`, and the `safety` decision so downstream reports can
explain where content came from.
