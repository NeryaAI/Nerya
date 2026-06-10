<!-- nerya-skill-frontmatter-start -->
---
name: research
description: "Use for external information: web search, page fetch, news, social posts, blog content, and current facts outside the workspace."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Research

Use before making claims that depend on current external information.

## Flow

SEARCH only as broadly as needed.
FOR latest economy/finance headline requests, load `news_social` first
and run its RSS script before broad web search.
FETCH full pages before relying on snippets.
USE markdown extraction first; use Jina Reader as fallback when direct
fetch is blocked or thin.
IF a page shows anti-bot, CAPTCHA, "verify you are human", JS-only, or
similar blocker content, keep using `scripts/fetch_url.py`; it falls
through to Jina Reader and then the configured headless browser engine.
TRACK source URL, fetch method, and timestamp.
SUMMARISE without over-quoting.
PASS evidence to `market_research` or `research_report` when needed.

## Scripts

- `scripts/web_search.py`
- `scripts/fetch_url.py`
- `scripts/search_fetch.py`
- `scripts/news_search.py`
- `scripts/social_search.py`

## Lazy References

- `references/full-playbook.md` for detailed search/fetch rules.
- `references/libraries.md` for research libraries.
