<!-- nerya-skill-frontmatter-start -->
---
name: analysis
description: "Use whenever the user wants to *look at data* \u2014 explore a CSV / parquet, build a quick chart, profile a function, check disk usage, inspect a database, run a one-off SQL query, diagnose a slow process, or sanity-check pipeline output. Triggers on \"plot this\", \"explore this dataset\", \"why is this slow\", \"compare these two files\", \"what's eating disk\", \"check the logs for\", \"show me a histogram of\". Read this skill before reaching for a heavyweight notebook setup; most data questions resolve in one or two scripts plus a short interpretation."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Analysis playbook

This skill covers the "let me look at this for a second" surface —
exploratory data analysis, quick diagnostics, lightweight ops
checks. It is *not* a replacement for a real notebook session when
the work is genuinely open-ended; it is the right tool when the
question is concrete and the answer fits in a chart or a paragraph.

## Pick the smallest tool that answers the question

- A grep / wc / head pass is often enough. Don't load a dataset into
pandas to count rows.
- A one-shot pandas script (`scripts/explore_table.py`) handles 90%
of "what does this CSV look like" questions.
- Reach for a real notebook only when the analysis branches — when
every answer raises three new questions.

## Chart hygiene

- Always label axes (units!) and date the chart.
- Prefer one chart that answers one question over a multi-panel
dashboard.
- Don't smooth without saying so. If you took a 7-day average, the
chart title says so.
- Save charts to `workspace/charts/<slug>.png` and quote the path
back to the user — never embed huge base64 blobs in the chat.

## Profiling

When asked "why is this slow", default to the cheap tools first:

1. `time` / `hyperfine` for end-to-end wall-clock.
2. `python -X importtime` for cold-start surprises.
3. `cProfile` only when you have a hypothesis about which call.
4. A real profiler (`py-spy`, `perf`) only after the cheap tools
  point at a specific function.

## Bundled scripts


| Script                     | Purpose                                                          |
| -------------------------- | ---------------------------------------------------------------- |
| `scripts/explore_table.py` | Load a CSV / parquet and emit head, describe, dtypes, row count. |
| `scripts/quick_chart.py`   | Make a single-pane chart from a column or two.                   |
| `scripts/disk_usage.py`    | Top-N largest paths under a root.                                |
| `scripts/log_grep.py`      | Filter + summarise a log file.                                   |
| `scripts/profile_run.py`   | Time a script with cProfile and emit the top calls.              |


Each script reads JSON payload via `--json` / `--payload-file` /
stdin.

## Failure modes

- **Loading 10GB into a pandas DataFrame on a laptop.** Sample first
(`head`, `awk NR % 1000`) before reaching for full loads.
- **Pretty charts that obscure the question.** A clean line chart
beats a glossy multi-pane dashboard for diagnosis.
- **Profiling without a hypothesis.** Profile data is overwhelming
without a guiding question; form the question first.