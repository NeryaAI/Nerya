# Notes: Prompt Playwright Hardcoding Cleanup

## Audit Rules
- Bad: case-id branches, exact prompt matching, ticker/venue-specific runtime routing, committee-specific marker hacks, mock success fallbacks, oversized always-on prompt instructions that encode workflows better handled by tool contracts.
- Acceptable: schema validation, URL parsing, JSON parsing, tool-result sanitization, artifact contract enforcement, public SDK compatibility aliases, test-only case IDs.

## Findings
- Runtime case-id search found no product branches for `C5`, `E2`, `GX6`, etc. Matches were limited to test harness files or comments saying the logic is not case-id based.
- The main brittle item was `nerya/agent/kernel.py`: the always-on system prompt contained a long workflow runbook with specific AgentTeam, strategy, connector, wallet, and recurring-task routing instructions.
- `nerya/agent/loop.py` still contains dynamic `[harness]` retry prompts. These are state-machine nudges tied to observed tool results, required artifacts, schema validation, provider failures, or wall-clock reserve. They are not case-id routers, but they remain prompt-based recovery text and should keep moving toward structured tool-choice/contract state over time.
- Regexes found in runtime are mostly parsing or sanitization: URL/year extraction, legacy tool-call cleanup, sensitive-key filtering, JSON/schema dump rejection, and SDK/strategy validation. No Playwright case regex route was found.

## Changes Made
- Shrunk `_render_turn_focus_block()` to a short generic policy: latest user request, tool schemas/loaded skills/observed state, tool-result evidence, live evidence for changing facts, required next action contracts, and final/debug separation.
- Shrunk `_render_permission_mode_block()` to a short permission boundary statement; removed route-specific trading confirmation language from the always-on prompt.
- Replaced the long `Workflow:` system prompt block with five generic execution principles.
- Updated prompt tests so they now reject old hard-route text instead of requiring it.
- Removed team template auto-remapping from role-name bundles. `team_run` now preserves the explicit `team_template`, and defaults to `ad_hoc_parallel_team` instead of inferring market/committee/design templates from role names.
- Rewrote `team_run`, `role_list`, `market_data`, `data_api`, `connector_list`, `strategy_generate_proposal`, `strategy_backtest`, and wallet/trade tool descriptions to describe capabilities and structured continuations rather than hardcoded routes or scenario-specific playbooks.
- Removed required-artifact recovery defaults that inferred markets/accounts from `BTC`/`ETH`/`SOL`/`BSC`/chain words. Recovery now requires explicit `market` and `account` in the structured contract.
- Removed error-text inference that stamped wallet readiness blockers as `OKX_ONCHAIN` when an error merely mentioned OKX/onchain.

## Current Audit Evidence
- `rg` in `nerya/agent/kernel.py` found no old hard-route terms such as wallet/provider route runbooks, committee workflows, recurring-routing instructions, or chat-order hard routing.
- `python -m pytest tests/test_no_runtime_route_hardcoding.py tests/test_agent_temporal_context.py tests/test_strategy_context_guidance.py -q` -> `46 passed`.
- `python -m pytest tests/test_no_runtime_route_hardcoding.py tests/test_agent_temporal_context.py tests/test_strategy_context_guidance.py tests/test_team_streaming_events.py tests/test_agent_loop_final_summary.py tests/test_strategy_code_generator.py tests/test_backtest_skill.py tests/test_data_api_tool.py tests/test_market_data_runtime.py tests/test_tool_compaction_data_api.py -q` -> `493 passed, 1 skipped`.
- Runtime audit after cleanup: no target Playwright case IDs in `nerya/agent` or `nerya/tools/native`; no `committee_role_markers`; old hard-route terms now absent from runtime code. Remaining regexes are parser/sanitizer/safety code paths such as URL/path parsing, shell risk classification, search, JSON/tool-call cleanup, and schema validation.
