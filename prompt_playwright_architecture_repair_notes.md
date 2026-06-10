# Notes: Prompt Playwright Architecture Repair

## Reference Sources
- `AgentArchitecturePatterns/REF/codex`
- `AgentArchitecturePatterns/docs-site/src/content/docs`

## Extracted Principles
- Agent loop completion should be state-machine/verifier driven, not based on model self-report alone.
- Required outputs are contract obligations. If the required artifact maps to an available tool, the loop should narrow/force the tool route before accepting prose finalization.
- Tools should be schema/contract driven. Runtime code should use generic artifact/tool contracts, not case IDs or prompt string matching.
- Permission/yolo only changes approval routing. It must not weaken completion criteria or verifier obligations.
- Execution surfaces must stay separate:
  - tool progress, raw JSON, debug trace, approval plans, and status cards are observable execution state,
  - final user answer is a separate clean surface,
  - UI projection may show both, but backend must not let trace text become `final_text`.
- Honest failure is valid only after the relevant action/tool was attempted or the tool is unavailable. Missing required artifacts before attempting the required tool is an unfinished loop state, not a final answer.

## Nerya Root-Cause Mapping
- Required artifact escape: `C5`, `E3`, `E10`, `GX6`. The loop exposes `required_artifact_missing_finalized`, but this is too late if the required tool is available and the model emitted a prose-only finish.
- Final reply pollution: `G9`, `GX4`, `GX11`, `I4`. Backend sometimes has no clean `final_text` and the dashboard falls back to decision/log/debug text.
- Artifact/finalizer semantics: `C-AT6`, `C-AT9`. Need distinguish incomplete artifact content from honest data-gap finalization after a tool attempt.
- Harness/precondition wording: `E2`, `E6`, `H9`. These may need contract/status wording fixes after root loop fixes are in place.

## Changes Made
- `nerya/agent/loop.py`
  - Added a generic tool-evidence finalizer for turns where native tools completed but provider returned no clean final text.
  - Preserved explicit required artifact gaps so tool-evidence summaries do not mask missing mandatory outputs.
  - Restricted required artifact missing finalization to generic terminal states, so stable blocker/exhaustion outcomes keep their more precise reason and text.
  - Deduped weak `next_required_action` nudges while leaving explicit `required_artifacts` as hard loop obligations.
  - Sanitized team `tool_observation_fallback` payloads so final answers do not expose raw observations, credential/status JSON, or fallback diagnostics as user-facing prose.
  - Added contract-derived recovery for required `evolve_provider_proposal` artifacts. If an explicit machine-readable contract requires a provider proposal and the model emits prose instead of the tool call, the loop can call the provider proposal tool with structured contract hints.
- `dashboard/components/chat/ChatMessage.tsx`
  - Empty assistant reply placeholders now remain marked as `data-turn-section="reply"` so tests and users do not fall back to tool/debug trace as the answer body.
- `nerya/agent/kernel.py`
  - Preserved provider-proposal contract hints such as `metadata_contains`, `venue`, `base_url`, `docs_url`, `auth`, `label`, and `runtime` through request normalization.
- `dashboard/tests/e2e/csv-runner.spec.ts`
  - Carried `metadata_contains` from `proposal_kind=...` checks into the required artifact contract, and mapped it to `subject` when no subject is provided.
- `nerya/skills/builtin/backtest/scripts/mock_ctx.py`
  - Added SDK-compatible market-data aliases (`get_ohlcv`, `get_candles`, `klines`) to backtest `MockCtx`, plus `get_ohlcv`/`klines` on `MockMarket`.
- `nerya/strategies/context.py`
  - Added the same top-level `get_ohlcv`/`get_candles`/`klines` compatibility aliases to the live `StrategyContext`, and `get_ohlcv` on `StrategyMarket`.
- `tests/test_agent_loop_final_summary.py`
  - Added coverage for clean tool-evidence finalization and for not masking required artifact gaps.
- `tests/test_backtest_skill.py`
  - Added coverage that backtest `MockCtx` and `MockMarket` expose the same common market-data aliases used by generated strategies.

## Plain-Language Root Causes
- `E2`: the team path sometimes finished with a child-agent fallback object instead of a clean report. In plain terms, the system accidentally handed the user the tool room's raw notes instead of the finished memo. Fix: keep fallback/debug JSON inside logs and synthesize a clean user-facing answer.
- `GX6`: the test contract explicitly required an Aster provider proposal, but the runtime treated that requirement too much like a suggestion. The model could say "I will create it" without actually calling `evolve_provider_proposal`. Fix: explicit required artifacts are loop obligations; if the required provider tool exists, the loop must produce the proposal artifact or honestly report the tool-observed blocker.
- `E3`: after the proposal and team work succeeded, `strategy_backtest` failed inside the backtest harness because generated strategy code used `ctx.get_ohlcv(...)`, while `MockCtx` only had `ctx.ohlcv(...)`. In plain terms, the live strategy SDK and replay SDK did not speak exactly the same dialect. Fix: make the public market-data aliases available in both live and backtest contexts.

## Non-Hardcoding Notes
- No case-id branches, exact prompt routers, mock LLM shortcuts, or committee-role marker hacks were added.
- The provider-proposal recovery is driven by structured `required_artifacts` and tool availability, not by the word "Aster" or the `GX6` id.
- The E3 repair is a public SDK compatibility fix for all strategy packages, not a SOL-specific or prompt-specific rewrite.
- The E2 repair separates execution/debug state from final user text for all team fallback payloads.

## Verification
- `python -m pytest tests/test_agent_loop_final_summary.py -q` -> 233 passed.
- `python -m pytest tests/test_backtest_skill.py -q` -> 36 passed.
- `python -m pytest tests/test_agent_loop_final_summary.py -k "strategy_backtest_runtime_error or distinct_backtest_runtime_errors or strategy_backtest_success_finalizes or strategy_backtest_data_gap or required_team_strategy_backtest" -q` -> 5 passed.
- `python -m pytest tests/test_agent_loop_final_summary.py tests/test_strategy_code_generator.py tests/test_team_streaming_events.py tests/test_backtest_skill.py -q` -> 374 passed.
- Focused yolo Playwright after E2 fix: `NERYA_CASES_ONLY=E2`, `--timeout=1200000` -> 1 passed.
- 13-case yolo rerun before E3 alias fix: 12 passed, 1 failed (`E3`).
- Focused yolo Playwright after E3 alias fix: `NERYA_CASES_ONLY=E3`, `--timeout=1200000` -> 1 passed.
- Final full yolo Playwright rerun: `NERYA_CASES_ONLY=C5,C-AT6,C-AT9,E2,E3,E6,E10,G9,GX4,GX6,GX11,H9,I4`, `--timeout=1200000` -> 13 passed (14.7m).
- `dashboard/test-results/summary.csv`: every row is `pass`, `api_check_pass=yes`.
- `dashboard/test-results/logs/*.reply.txt` internal-leak scan: no matches for fallback/status/schema markers.
