# Research — search providers and fetching tools

Use this when picking how to gather information from outside the
workspace.

## Search providers

The agent has access to a web search tool natively. Reach for a
custom script only when you need:

- a *specific* provider (e.g. constrained to news, or constrained
  to a domain), or
- a structured result format (machine-readable JSON), or
- batched search across many queries.

Useful libraries:

- **httpx** — outbound HTTP for any of the providers below.
- **trafilatura** — extract clean main-text from arbitrary HTML.
  Better than home-rolled `<p>` extraction.
- **readability-lxml** — older alternative to trafilatura; slightly
  more aggressive about boilerplate removal.

## Common search APIs

- **Google Programmable Search** — needs a CSE id + key. Best for
  general-purpose web.
- **Bing Web Search** — drop-in alternative; sometimes better
  recall on technical terms.
- **DuckDuckGo Instant Answer / HTML** — no key but heavy rate
  limits; avoid for production loops.
- **Brave Search API** — privacy-leaning; useful when you want to
  avoid Google's ranking biases.
- **Tavily / Exa / Serper** — search-as-a-service wrappers around
  the above; pay for them when you want one key for everything.

## Social

- **X (Twitter) API** — requires a developer key; rate-limited
  aggressively on free tier.
- **Reddit API (PRAW)** — fine for read paths; you'll need an OAuth
  app even for read-only.
- **Hacker News (Algolia)** — public, no key, fast.

## Fetching

For URL → markdown:

- The native `fetch_url` tool when a single page is enough.
- A bundled `fetch_url.py` (this skill) when the agent needs the
  raw markdown for a downstream script.

For JS-heavy pages:

- **playwright** — when the page only renders after JS executes.
  Heavyweight; only use when a static fetch fails.

## Patterns

1. **Search → fetch → quote.** Don't summarise from the snippet;
   pull the page, then quote.
2. **Always cite.** Every fact tied to a fetched URL must include
   the URL and date in the reply.
3. **Cache during a session.** If you're going to fetch the same
   page twice in a single task, write it to a temp file the second
   time around.
