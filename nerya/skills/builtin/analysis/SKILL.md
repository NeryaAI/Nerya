<!-- nerya-skill-frontmatter-start -->
---
name: analysis
description: "Use for local data, logs, tables, databases, charts, profiling, disk inspection, or one-off evidence extraction."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Analysis

Use this skill when the user asks what the data, logs, files, metrics,
or local state actually say.

## Flow

CLASSIFY input: table, log, database, folder, process, or metric.
READ schema/head/sample before computing.
RUN the narrowest script or shell command that proves the answer.
SUMMARISE numbers, anomalies, and uncertainty.
SAVE generated artifacts only when they help reproduce the result.

## Scripts

- `scripts/explore_table.py` for quick CSV/parquet/table profiling.

## Lazy References

- `references/full-playbook.md` for deeper analysis patterns.
- `references/libraries.md` for local data-analysis library choices.
