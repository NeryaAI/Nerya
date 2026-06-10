# Task Plan: Prompt Playwright All Cases Parallel 3 Rerun

## Goal
Run the latest code against all CSV-driven prompt Playwright cases with 3 workers, using the updated mimo API key in isolated test config, and record detailed evidence without modifying product code.

## Phases
- [x] Phase 1: Read instructions and inspect current workspace/test files
- [x] Phase 2: Stop stale E2E services and prepare isolated workspace
- [x] Phase 3: Apply updated mimo key to isolated test config only
- [x] Phase 4: Run all cases with Playwright workers=3 and 20-minute timeout
- [x] Phase 5: Inspect summary, logs, replies, screenshots, and stderr
- [x] Phase 6: Write detailed Chinese run report

## Constraints
- Do not modify product code.
- Use latest working tree as-is.
- Run all cases from `dashboard/tests/e2e/cases.timeout20m.csv`.
- Use `--workers=3` and `--timeout=1200000`.
- Use yolo permission mode.
- Do not print raw API keys in reports.

## Status
**Completed** - final Playwright stdout reports `35 failed`, `125 passed`, total duration `1.2h`. Detailed Chinese report is in `prompt_playwright_all_cases_parallel3_report.zh-CN.md`.
