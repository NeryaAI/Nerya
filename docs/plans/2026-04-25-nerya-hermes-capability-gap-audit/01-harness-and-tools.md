# 01 - Harness and Tools Gap

## Current Nerya Capability

Nerya has a real harness, but it is a **skill-call harness**, not a general operator harness.

Evidence:

- `nerya/harness/tool_runner.py` owns skill action execution, retries, budgets, and policy boundaries.
- `nerya/agent/kernel.py` maps canonical agent actions to skill/action pairs and dispatches through the harness.
- `nerya/skills/runtime.py` validates skill input schemas, attaches caller/session metadata, journals skill calls, and wraps errors.
- `nerya/security/`, `nerya/trading/risk.py`, and `nerya/trading/approval.py` provide domain safety for trading paths.

## Hermes Capability

Hermes has a mature general tool harness.

Evidence:

- `model_tools.py` imports and discovers self-registering tools from `tools/registry.py`.
- `tools/` contains about 60 tool modules: terminal, file tools, patch, browser, web, MCP, delegate, code execution, memory, todo, process registry, voice, image, and session search.
- `run_agent.py` has tool-loop logic, parallel-safe tool batching, destructive command heuristics, callbacks, tool-result storage, interruption, checkpoints, and environment cleanup.
- `tools/approval.py`, `tools/terminal_tool.py`, `tools/file_tools.py`, `tools/browser_tool.py`, `tools/mcp_tool.py`, `tools/delegate_tool.py`, and `tools/code_execution_tool.py` are concrete operator capabilities.

## Gap

Nerya cannot currently feel like Hermes/Codex because it lacks first-class general tools:

- no native terminal command tool for agent turns,
- no general file read/write/search/patch tools exposed to the agent loop,
- no browser automation/web extraction stack comparable to Hermes,
- no process/background job registry,
- no result persistence for oversized tool outputs,
- no interrupt/redirect semantics at the harness level,
- no multi-environment execution backends such as local/docker/ssh/modal/daytona/singularity,
- no general approval model for filesystem/shell/browser operations; current safety is mostly trading/skill oriented.

## Why This Hurts Experience

If the user asks the agent to fix code or inspect a runtime, Nerya must route through limited skills or API surfaces. Hermes can inspect files, patch code, run commands, open browsers, delegate, persist outputs, and continue a long task. That is the core experiential difference.

## P0 Alignment Items

1. Add `operator_tool` skill or native harness layer with `read_file`, `search_files`, `write_file`, `patch`, `terminal`, `process`, `web_search`, `web_extract`, `browser_`*, and `execute_code`. **Status: PARTIALLY COMPLETED 2026-04-25.** Built-in `operator` skill (`Nerya/nerya/skills/builtin/operator_skill/skill.yml` lines 1-186, `Nerya/nerya/skills/builtin/operator_skill/actions.py` lines 1-340) ships `read_file`, `list_dir`, `search_files`, `write_file`, and `terminal` actions with full path-safety + destructive-command guard. Coverage: `Nerya/tests/test_operator_skill.py` (12 cases) — read/range/escape, list/glob, search, write/append/escape, safe terminal echo, destructive-pattern refusal, timeout. Skill is auto-enabled via `Nerya/nerya/workspace/manager.py:108-113`. Remaining: `patch_file`, `web_search`, `web_extract`, `browser_*`, `process` registry, `execute_code` (script_skill already covers code exec — consider routing through there).
2. Add a tool registry analogous to Hermes `tools/registry.py`, even if Nerya wraps each tool as a skill for policy consistency. **Status: ALREADY COMPLETE.** The dynamic action catalog at `Nerya/nerya/agent/kernel.py::build_action_catalog` walks every loaded skill manifest's `agent_action`/`agent_payload_builder`/`agent_hint` keys and feeds them to the planner — every skill action is the registry entry. The `operator` skill plugs in via the same path. Discoverable through `GET /runtime/capability_matrix` (Plan 23 §12 partial).
3. Add approval classes for non-trading actions: destructive filesystem, shell mutation, network/browser automation, secrets access, and long-running background process. **Status: PARTIALLY COMPLETED 2026-04-25.** `operator.write_file` and `operator.terminal` declare `risk_gate: required` + `approval_gate: always` in their manifest entries (`Nerya/nerya/skills/builtin/operator_skill/skill.yml`). `operator.terminal` also enforces in-line refusal of destructive Hermes-style patterns (rm -rf, dd, mkfs, sudo, fork bombs, …) at `Nerya/nerya/skills/builtin/operator_skill/actions.py:38-50`. Remaining: extend `nerya/trading/approval.py` to a generic `OperatorApproval` so the existing risk-gate plumbing surfaces these calls in the dashboard approval queue (now they short-circuit at the action layer).
4. Add tool-result storage for large outputs and journal references instead of stuffing everything into context. **Status: COMPLETED 2026-04-25.** `Nerya/nerya/harness/result_store.py` exposes `ResultStore.store/load/list_refs/prune` and a `maybe_persist(payload, threshold_bytes=...)` helper. Files land under `<workspace>/state/tool_results/`. Coverage: `Nerya/tests/test_result_store.py` (5 cases — roundtrip, recency ordering, inline-vs-ref threshold, prune-keeps-newest). Wiring callers (replace inline observations with refs) is a follow-up but the storage primitive is in place.
5. Add interruption/cancellation handles into `AgentKernel.run_turn` and `ToolRunner`. **Status: COMPLETED 2026-04-25.** New `nerya/harness/cancellation.py` ships a `CancelToken` (cooperative; supports explicit `cancel(reason)` + `deadline_s` timeout) and a `CancelledError`. `AgentKernel.run_turn` now accepts `cancel_token=` (`Nerya/nerya/agent/kernel.py:516-540, 567-571, 612-619`) and the iteration loop checks the token at the top of each replan, surfacing `stopped_reason="cancelled:<reason>"` on the turn result (`Nerya/nerya/agent/kernel.py:822-832`). Coverage: `Nerya/tests/test_cancellation.py` (5 cases — fresh/cancelled/deadline/reset/passthrough).

## P1 Alignment Items

1. Add safe parallel execution for independent read-only/file-scoped tools. **Status: COMPLETED 2026-04-25.** `Nerya/nerya/harness/tool_runner.py:97-133` ships `ParallelTask` (the input schema with `skill_id`/`action`/`payload`/`scope`/`timeout_s`) and `Nerya/nerya/harness/tool_runner.py:182-322` implements `ToolRunner.call_parallel(...)` with: (a) per-task `agent_query_only` validation via `is_query_only` (rejects mutating actions with `error_kind="not_query_only"`), (b) atomic budget reserve+commit through the new `TurnBudget.reserve_batch_commit` (`Nerya/nerya/harness/budget.py:117-179`), (c) `ThreadPoolExecutor` dispatch with configurable `max_parallel` (default 4, falls back to serial when `max_parallel=1`), (d) failure rollback via `TurnBudget.release_one` (`Nerya/nerya/harness/budget.py:181-191`) so failed parallel calls don't burn budget — matching the serial `record_call`-on-success contract. `TurnBudget` is now thread-safe (RLock at `Nerya/nerya/harness/budget.py:46-58`). `TurnHarness.step_parallel` (`Nerya/nerya/harness/executor.py:106-141`) plumbs the same primitives through the higher-level harness so callers can keep using `PlannedCall`. Coverage: `Nerya/tests/test_parallel_tools.py` (18 cases) — read-only refusal, missing skill/action validation, jitter-free input ordering, real concurrency assertion (3 threads + barrier under 1.5s), serial fallback, shared-budget pre-allocation, mid-batch failure isolation, per-call timeout, dict-form acceptance, type validation, contended `TurnBudget` (200 ops × 4 threads), atomic batch reservation, `release_one` rollback, end-to-end `operator.read_file`/`list_dir`/`search_files` parallel batch, `operator.write_file` refusal, and serial-call regression. Cross-suite regression: `tests/test_parallel_tools.py + test_harness.py + test_operator_skill.py + test_operator_presets.py + test_model_registry.py + test_llm_gateway.py` = 105 passed; full repo sweep = 1588 passed / 2 skipped.
2. Add environment backends: local first, SSH/VibeShell second, docker third. **Status: PENDING.** `operator.terminal` runs locally only.
3. Add operator-mode presets: `read_only`, `dev`, `deploy`, `live_trading`. **Status: COMPLETED 2026-04-25.** `Nerya/nerya/agent/operator_presets.py` ships the four built-in presets (`read_only`, `dev`, `deploy`, `live_trading`) with `OperatorPreset` (lines 80-126) carrying `query_only` / `block_mutating` / `requires_live_trading_flag` / `deny_actions` / `deny_skills` / `allow_actions` / `allow_skills` policies. `evaluate()` (lines 263-329) decides per-action allow/deny with a stable reason code; `filter_actions()` (lines 332-361) applies the policy and stamps each surviving row with the decision payload. Workspace config exposes `agent.operator.{preset, extra_allow_actions, extra_deny_actions}` (`Nerya/nerya/core/config.py:agent.operator` block) and the planner pipes it into `build_action_catalog` so the LLM-visible catalog is filtered post-availability (`Nerya/nerya/agent/kernel.py:426-540`, preset filter at lines 503-540). Capability matrix exposes both the active preset and the catalog of presets via `_operator_presets_section` + a dedicated `GET/POST /runtime/operator_presets` route (`Nerya/nerya/api/routes_capability.py:193-219, 255-272`). Coverage: `Nerya/tests/test_operator_presets.py` (23 cases — built-in metadata, fallback on unknown id, every preset's filter behaviour, extra allow/deny overrides, live-trading flag gating, `build_action_catalog` end-to-end with synthetic skills, capability matrix endpoint payload, and route registration).
4. Add tests mirroring Hermes hazards: destructive command detection, path-scoped parallel safety, terminal timeout, large output persistence. **Status: PARTIALLY COMPLETED 2026-04-25.** `Nerya/tests/test_operator_skill.py::test_terminal_refuses_destructive` covers destructive detection; `test_terminal_handles_timeout` covers timeout; `Nerya/tests/test_result_store.py` covers oversized-output persistence. Path-scoped parallel safety still TBD with the parallel-batch work above.

## Acceptance Gate

A P0-ready Nerya harness should pass an end-to-end scenario: user asks Nerya to inspect a repo, find a bug, patch it, run a narrow test, summarize changed files, and preserve evidence in the session journal without custom trading-specific skills.