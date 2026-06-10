# Notes: Prompt Playwright All Cases Parallel 3 Rerun

## Initial Facts
- User requested rerun only, no code changes.
- User requested all cases, concurrency 3.
- Updated mimo API key should be used in isolated test config.
- Current working tree is dirty with many existing changes; no reset/revert will be done.
- Available CSV files:
  - `dashboard/tests/e2e/cases.timeout20m.csv`
  - `dashboard/tests/e2e/cases.csv`
- Runner file: `dashboard/tests/e2e/csv-runner.spec.ts`.

## Records
- Plan created before test run.
