# Task Plan: Prompt Playwright Case Repair

## Goal
Fix the prompt Playwright failures found in the 160-case rerun, starting with high-confidence product defects and keeping changes focused, verifiable, and reversible.

## Phases
- [x] Phase 1: Create persistent repair planning files
- [x] Phase 2: Re-read evidence and inspect target code paths
- [x] Phase 3: Implement first repair batch
- [x] Phase 4: Run focused checks
- [x] Phase 5: Update repair report and next-step plan

## First Repair Batch
1. `A10`: make cancel produce a user-visible assistant text instead of an empty reply/error-only card.
2. `D5`: normalize approved script ids so path-like script references resolve to the approved script id.
3. Output/finalizer guard: prevent tool/debug/evidence JSON from being treated as final assistant reply when no clean final text exists.
4. Required artifact guard: make required proposal/backtest/provider/skill tools harder to bypass when the CSV contract requests them.

## Key Questions
1. Where does the CSV runner extract assistant reply text, and how can UI debug blocks stay out of the final answer?
2. Which agent loop branch allows `model_done` / `no_more_tools` while required artifacts are still missing?
3. Can the first batch be verified with unit tests or a subset of CSV cases without rerunning all 160 cases?

## Decisions Made
- Do not change model/provider configuration as part of repair.
- Do not write any plaintext secrets into repo files.
- Fix product code first; adjust brittle tests only after product behavior is correct.
- Prefer small targeted patches and focused checks before considering broader refactors.
- Treat operator cancel as a normal user-visible terminal state, not an error-card failure.
- Normalize approved script paths to stable script ids before schedule persistence.
- Add a final required-artifact guard that reports missing required tools instead of silently accepting prose-only completion.
- Use `NERYA_PERMISSION_MODE=yolo` / `NEXT_PUBLIC_NERYA_PERMISSION_MODE=yolo` for prompt Playwright reruns so routine approvals do not block unattended cases.

## Errors Encountered
- First focused Playwright attempt reused an existing dashboard on `:3001` whose proxy pointed at `C:\Users\Ricky\.nerya`; global setup correctly blocked workspace mismatch.
- Second focused Playwright attempt used a relative workspace path, causing expected/runtime workspace path comparison mismatch.
- Third focused Playwright attempt passed workspace/proxy checks but failed the live LLM readiness probe because the probe hit `sensenova-6.7-flash-lite` and received `stop_reason=max_tokens` with empty text. Final focused run used `NERYA_E2E_SKIP_LLM_PROBE=1` to enter the cases.
- Yolo historical-failure rerun finished with 9 pass / 13 fail; remaining failures are not permission-approval waits.

## Status
**Completed first repair batch and yolo triage** - A10/D5 pass; yolo rerun shows 13 remaining failures after permission waits are removed.
