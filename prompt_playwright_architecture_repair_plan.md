# Task Plan: Prompt Playwright Architecture Repair

## Goal
Use the local AgentArchitecturePatterns references to fix remaining prompt Playwright failures at the architecture boundary, avoiding case-specific hardcoding or brittle prompt-only patches.

## Scope
- Reference:
  - `C:/Users/Ricky/Documents/Project/AgentArchitecturePatterns/REF/codex`
  - `C:/Users/Ricky/Documents/Project/AgentArchitecturePatterns/docs-site/src/content/docs`
- Target remaining failures after yolo rerun:
  - Product/output: `C5`, `C-AT6`, `C-AT9`, `E3`, `E10`, `G9`, `GX4`, `GX6`, `GX11`, `I4`
  - Harness/env/precondition: `E2`, `E6`, `H9`
- Testing must run with `NERYA_PERMISSION_MODE=yolo` and clean isolated workspace data before rerun.

## Phases
- [x] Phase 1: Create plan/notes files
- [x] Phase 2: Extract relevant architecture best practices
- [x] Phase 3: Inspect Nerya root-cause code paths
- [x] Phase 4: Implement architecture-level fixes
- [x] Phase 5: Clean isolated workspace and run focused Playwright failures
- [x] Phase 6: Update Chinese report

## Key Questions
1. How should required tool/artifact contracts be represented as state-machine obligations rather than prompt suggestions?
2. How should final answers be selected so trace/debug blocks never become user-facing reply text?
3. How should partial/tool failure states be classified as honest final outcomes without masking missing required artifacts?

## Decisions Made
- No hardcoded case IDs or prompt strings in runtime logic.
- Prefer explicit state/contract handling over stronger prompt wording.
- Keep UI extraction as a consumer of backend final text; root finalizer fixes belong in backend.
- Clean test workspace with project reset tooling before Playwright verification; do not clean or reset git worktree.
- Treat required artifacts as hard loop obligations when the required tool exists; an assistant prose answer cannot close the turn before verifier obligations are satisfied.
- Keep tool progress, raw JSON, and diagnostic trace out of the final user reply surface; they remain observable through logs/cards/events.
- Do not add committee-specific marker hacks or case-specific routers. Fixes must be based on structured tool contracts, runtime state, and public SDK compatibility.
- Backtest replay must expose the same common market-data aliases as the live StrategyContext; generated strategy code should not pass live validation but fail only in MockCtx due to missing SDK aliases.

## Errors Encountered
- `tests/test_agent_loop_final_summary.py` initially regressed three existing state-machine tests after adding tool-evidence finalization. Resolution: required artifact return finalization now only replaces generic terminal states, preserving specific blockers/exhaustion; weak `next_required_action` nudges are deduped without weakening explicit `required_artifacts`.
- Full 13-case Playwright rerun failed only `E3`: `strategy_backtest` was called but failed with `AttributeError: 'MockCtx' object has no attribute 'get_ohlcv'`. Resolution: added public SDK-compatible `get_ohlcv`/related market-data aliases to both live `StrategyContext` and backtest `MockCtx`, with pytest coverage.

## Status
**Complete** - isolated workspace was cleaned before verification; full 13-case yolo Playwright rerun passed with 20-minute per-case timeout, and backend regression passed.

## Final Verification
- Backend: `python -m pytest tests/test_agent_loop_final_summary.py tests/test_strategy_code_generator.py tests/test_team_streaming_events.py tests/test_backtest_skill.py -q` -> `374 passed`.
- Focused E3 after SDK alias fix: `NERYA_CASES_ONLY=E3 npx playwright test csv-runner --workers=1 --timeout=1200000 --reporter=line` -> `1 passed`.
- Full prompt regression: `NERYA_CASES_ONLY=C5,C-AT6,C-AT9,E2,E3,E6,E10,G9,GX4,GX6,GX11,H9,I4 npx playwright test csv-runner --workers=1 --timeout=1200000 --reporter=line` -> `13 passed (14.7m)`.
- Final summary: all 13 rows in `dashboard/test-results/summary.csv` are `pass` with `api_check_pass=yes`.
- Reply audit: `dashboard/test-results/logs/*.reply.txt` was scanned for internal leak markers (`tool_observation_fallback`, `credential_status`, JSON status/iteration fragments, `skill_calls`, team marker strings); no matches.
