# Analysis — libraries you'll typically reach for

Read this before installing or importing something exotic. Most
analysis tasks resolve cleanly with the libraries below.

## Tabular data

- **pandas** — default for ad-hoc tabular work. Easy interop with
  CSV / Parquet / Excel.
- **polars** — faster than pandas on > 1 GB tables; lazy mode is a
  good fit when you need to chain transforms without realising
  intermediates.
- **duckdb** — when SQL is the right shape for the question. Works
  directly on CSV/Parquet without an import step.
- **pyarrow** — Parquet IO + Arrow tables; useful as a bridge layer.

## Charts

- **matplotlib** — default. Plain, reproducible, scriptable.
- **seaborn** — statistical plots on top of matplotlib (distribution,
  regression, categorical comparisons).
- **plotly** — interactive charts, embeddable as HTML. Good for
  reports the user will browse.

Save charts to `workspace/charts/<slug>.png` and quote the path in
your reply rather than embedding base64 in chat.

## Numerics

- **numpy** — vector math. Prefer over Python loops on > 1k elements.
- **scipy** — distributions, optimisation, signal processing,
  statistics.

## Profiling and diagnostics

- **time / hyperfine** — wall-clock; cheapest tool, try first.
- **cProfile** — built-in profiler; one-liner via `python -m
  cProfile -s cumtime`.
- **py-spy** — sampling profiler; works on running processes.
- **memory_profiler** — memory allocation per line; only when memory
  is the concern.

## When to write your own script

Write a small script when:

- the analysis is a one-shot answer, not a session of exploration, or
- the output should be JSON for a downstream consumer, or
- the same task will be re-run later (a script is documentation).

Open a notebook when:

- the analysis branches and re-explores at every step, or
- you need to keep intermediate state (a fitted model, a cleaned
  dataframe) live for follow-up questions.

## Patterns

1. **Sample before loading.** For files > 100MB, read the first N
   rows or sample-by-skip rather than loading the whole table.
2. **JSON in, JSON out.** Mirror the shape of the bundled scripts so
   your custom helpers compose with `run_shell` cleanly.
3. **Cite paths, not contents.** When the result is a chart, write
   the file then quote the path; do not paste base64.
