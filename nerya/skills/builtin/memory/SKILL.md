<!-- nerya-skill-frontmatter-start -->
---
name: memory
description: "Use whenever the agent needs to remember or recall something across sessions \u2014 write a fact to long-term memory, look up a past decision, read the journal of recent skill calls, or reconstruct what happened in a previous session. Triggers on \"remember that\", \"what did we decide last time\", \"show me the journal\", \"recap the last run\", \"did we already try X\", or any prompt that depends on continuity beyond the current chat. Reach for this skill *before* re-deriving something the system already knows."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Memory playbook

Memory and trace are two sides of the same problem: the runtime
already records everything that happens (the journal/trace) and the
agent occasionally promotes a fact into durable memory. The agent's
job is to *read* both before reinventing answers, and to *write*
sparingly so the durable store stays useful.

## Two stores

- **Trace / journal** — append-only record of every skill call,
  approval, error, and message. Always available, always authoritative.
  Read this first.
- **Memory** — small, hand-curated facts (preferences, durable
  decisions, learned heuristics). Written intentionally, read like a
  reference manual.

Anything quantitative (PnL, fills, balances) lives in the trade /
account stores, not in memory. Memory is for *qualitative* facts
that are too valuable to re-derive.

## Reading

For "what happened", search the trace by time range, skill, or
session. Do *not* paste the entire journal into context; pull only
the entries that matter and summarise.

For "what did we decide", search memory by topic / tag. Memory
entries are short; you can quote them in full.

## Writing

Promote a fact to memory when **all** of these hold:

1. The fact will still be true in a week.
2. Re-deriving it is non-trivial.
3. It is not already implied by another memory entry.

Bad memory entries: "user said hi", "ran a backtest", "the price was X".
Good memory entries: "user prefers limit orders over market", "this
strategy retires when 30d Sharpe < 0.5", "ETH gas tracker uses N
endpoint as the source of truth".

## Bundled scripts

| Script | Purpose |
|---|---|
| `scripts/recall.py` | Search durable memory by topic / tag / freeform. |
| `scripts/remember.py` | Append a curated fact to memory. |
| `scripts/journal_search.py` | Search the trace for events matching filters. |
| `scripts/session_recap.py` | Summarise a past session into a short brief. |

Each script reads JSON payload from `--json` / `--payload-file` /
stdin.

## Failure modes

- **Writing low-signal entries.** If you wouldn't read it again,
  don't write it.
- **Treating memory as a database.** It's not — it's a notebook.
- **Skipping the journal.** "What did we do last time" is almost
  always answerable by the trace alone, no LLM call needed.
