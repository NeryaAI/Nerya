# Notes: Prompt Playwright Repair

## Source Evidence
- Rerun notes: `prompt_playwright_rerun_notes.md`
- Chinese repair plan: `prompt_playwright_rerun_fix_plan.zh-CN.md`
- Summary CSV: `dashboard/test-results/summary.csv`
- Per-case logs: `dashboard/test-results/logs/<case>.jsonl`
- Screenshots: `dashboard/test-results/screenshots/<case>.png`

## Initial Failure Buckets
- User-visible final answer missing or polluted by internal debug data.
- Required artifact tools not being forced reliably.
- Tool parameter normalization gaps.
- Harness/environment precondition mismatches.

## Findings
- `dashboard/tests/e2e/csv-runner.spec.ts`: `sendAndCancel()` previously returned `void`; cancel cases kept `reply=""`, so A10 failed the reply-quality gate even when UI state changed.
- `dashboard/components/chat/ChatView.tsx`: operator cancel was rendered as `msg.error`, which produces an error card and suppresses `[data-turn-section="reply"]`.
- `nerya/skills/builtin/tasks/scripts/create_task.py`: script task creation only accepted bare ids or `script:<id>`. A model-provided path such as `scripts/approved/eth_btc_ratio_chart/eth_btc_ratio_chart.py` failed approval lookup or persisted an unstable target.
- `nerya/agent/loop.py`: deterministic abort summary included `tool errors:`, which the CSV reply-quality gate treats as an internal dump marker.
- `nerya/agent/loop.py`: required artifact retries existed for final text, but there was no final return guard when a required artifact was still missing after the loop exhausted safe retries.

## Checks Run
- `python -m pytest tests/test_tasks_skill.py tests/test_agent_loop_final_summary.py::test_max_iterations_without_final_text_gets_deterministic_summary tests/test_agent_loop_final_summary.py::test_required_artifact_missing_finalizes_with_explicit_gap -q` -> 18 passed.
- `npx tsc --noEmit` from `dashboard/` -> passed.
- Focused Playwright: `NERYA_CASES_CSV=tests/e2e/cases.timeout20m.csv`, `NERYA_CASES_ONLY=A10,D5`, ports `3060/18460`, `NERYA_E2E_SKIP_LLM_PROBE=1` -> 2 passed.
- A10 log: reply length 51, reply-quality ok, API check ok; screenshot shows a visible Chinese cancellation reply and no error card.
- D5 log: `glob` and `task_create` succeeded, transition `task_schedule_created`, API check `schedule session_kind=script ok`; screenshot shows `task_create ok`; workspace schedule persisted `target: script:eth_btc_ratio_chart` and `payload.script_id: eth_btc_ratio_chart`.
- Yolo rerun of historical failures: `NERYA_PERMISSION_MODE=yolo`, `NEXT_PUBLIC_NERYA_PERMISSION_MODE=yolo`, `NERYA_CASES_ONLY=C3,C5,C7,C-AT6,C-AT9,D9,E2,E3,E6,E10,F4,F6,G1,G9,GX1,GX4,GX6,GX11,H6,H9,I3,I4`, ports `3061/18461`, `NERYA_E2E_SKIP_LLM_PROBE=1` -> 9 passed, 13 failed, duration 26.5 min.
- Yolo pass list: `C3`, `C7`, `D9`, `F4`, `F6`, `G1`, `GX1`, `H6`, `I3`.
- Yolo fail list: `C5`, `C-AT6`, `C-AT9`, `E2`, `E3`, `E6`, `E10`, `G9`, `GX4`, `GX6`, `GX11`, `H9`, `I4`.

## Remaining Failed Buckets
- Product/output gaps still pending after yolo rerun: `C5`, `C-AT6`, `C-AT9`, `E3`, `E10`, `G9`, `GX4`, `GX6`, `GX11`, `I4`.
- Harness/env/brittle assertion or workspace-precondition items still pending after yolo rerun: `E2`, `E6`, `H9`.
- Live LLM readiness probe instability remains: setup can still route probe traffic to `sensenova-6.7-flash-lite`, which returned empty text with `max_tokens`.

## Yolo Rerun Failure Details
- `C5`: no tool evidence; required `strategy_generate_proposal` and `strategy_backtest` both missing. Transition `required_artifact_missing_finalized`.
- `C-AT6`: proposal/backtest succeeded, but generated `main.py` missed required `news_social` integration.
- `C-AT9`: proposal succeeded and `strategy_backtest` tool ran, but API check saw transition `no_more_tools` instead of `strategy_backtest_finalized`; the tool result was `ok=false/no_historical_data`, so finalizer/contract semantics need tightening.
- `E2`: `team_run` succeeded and bounded fallback returned a debate, but final text did not contain literal `team`; likely output contract/harness wording issue.
- `E3`: `team_run` succeeded, but required strategy proposal/backtest artifacts still missing; transition `required_artifact_missing_finalized`.
- `E6`: output quality ok, but API check expected Financial Datasets not ready while workspace reports ready=true; likely workspace/vault precondition mismatch.
- `E10`: no tool evidence; required strategy proposal/backtest artifacts missing.
- `G9`, `GX4`, `GX11`, `I4`: reply quality failed because final reply included rendered thinking/tool/Raw JSON transcript text.
- `GX6`: required `evolve_provider_proposal` missing for Aster provider proposal.
- `H9`: provider proposal was created, but final reply did not match the must_contain contract for Financial Datasets key/vault/status wording.

## Follow-up Fix Attempt
- Added dashboard `topLevelDecisionText()` fallback from `reply_text` to `final_text`; `npx tsc --noEmit` passed.
- Reran `G9,GX4,GX11,I4` with yolo on ports `3062/18462`; all 4 still failed reply quality. This means these turns did not have a clean backend `final_text`; the page fell back to whole trace because the backend ended with `no_more_tools` and no natural-language final answer. Root cause is backend finalizer/no-more-tools behavior, not only dashboard rendering.
