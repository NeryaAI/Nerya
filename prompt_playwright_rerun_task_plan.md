# Task Plan: Prompt Playwright Case Rerun

## Goal
Run the prompt Playwright cases with the user's current local `C:\Users\Ricky\.nerya\nerya.yml` LLM model/provider configuration, judge implemented vs missing cases from responses/logs/screenshots, and produce a Chinese repair plan.

## Phases
- [x] Phase 1: Discover case entrypoints and provider configuration
- [x] Phase 2: Configure an isolated run without persisting plaintext secrets
- [x] Phase 3: Run the full prompt Playwright cases
- [x] Phase 4: Analyze results, logs, traces, and screenshots
- [x] Phase 5: Write Chinese fix plan for missing cases

## Key Questions Answered
1. Prompt case entrypoint: `dashboard/tests/e2e/csv-runner.spec.ts`.
2. Case source used for the final run: `dashboard/tests/e2e/cases.timeout20m.csv`.
3. Timeout verification: 160 rows, all `timeout_ms=1200000`.
4. Final result: 160 cases completed, 136 passed, 24 failed.
5. Final evidence is from local `C:\Users\Ricky\.nerya\nerya.yml` model/provider routing, not from the interrupted Sensenova-only run.

## Decisions Made
- Do not write or report plaintext API keys.
- Use a separate 20-minute CSV copy instead of mutating the original case file.
- Use Playwright autostart with isolated workspace `dashboard/.nerya-local-config-20m-workspace`.
- Use local `~/.nerya/nerya.yml` model/provider tiers after the user changed direction.
- Skip the undersized live LLM probe and judge actual case turns/logs because the probe can fail on reasoning-only short completions.
- Classify failed rows into product implementation gaps, output/finalizer gaps, test harness/precondition issues, and brittle assertions.

## Errors Encountered
- Single-case launch attempt 1 did not start tests: `Resolve-Path` was used before the isolated workspace directory existed, and `--reporter=list,json` was parsed incorrectly by PowerShell. Resolution: create/use a literal workspace path and rely on configured reporters.
- Single-case launch attempt 2 passed workspace/provider checks but global setup stopped at the 128-token live LLM probe. Resolution: skip the undersized readiness probe for the final CSV run.
- Earlier full run used original per-case timeouts and later showed Next.js dev cache corruption. Resolution: stop old runtime/dashboard processes, clean `.next`, and rerun from scratch with 20-minute CSV.
- Sensenova-only 20-minute run was interrupted after the user requested local `~/.nerya/nerya.yml` model routing. Resolution: rerun from scratch using the local config.
- One read-only PowerShell extraction command had an empty-pipe syntax error. Resolution: rerun with an explicit output array; no test artifacts were modified.

## Status
**Complete** - full 160-case suite finished; notes and Chinese repair plan are written in:
- `prompt_playwright_rerun_notes.md`
- `prompt_playwright_rerun_fix_plan.zh-CN.md`
