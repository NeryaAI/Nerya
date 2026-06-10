# Nerya Agent Harness Best-Practices Implementation Plan

## Objective
Make the Nerya agent loop more like a production harness: observable, verifier-driven, and evidence-based, without hardcoded regex routing or mock-based E2E passing.

## Requirements From User Request
- Delete hardcoded regex/route patches used to force E2E cases through.
- Study `AgentArchitecturePatterns` and apply the best-practice harness patterns.
- Analyze where Nerya's agent is weak relative to those patterns.
- Deeply supplement agent and related capabilities.
- Re-run failed Playwright prompt CSV cases using real API/provider.
- Analyze logs for any remaining failures and fix root causes.
- Finish by running all cases, not just a narrow subset.

## Architecture Findings
1. Nerya already has useful primitives: turn IDs, journals, tool blocks, batch summaries, todo state, session restore, and API-visible runtime state.
2. The weakest layer is completion judgment. The loop can stop on model prose even when no real tool/action evidence exists.
3. Current focus rules in `kernel.py` have been moved toward generic evidence policy; stale tests now assert schema/tool-description contracts rather than prompt keyword routing.
4. The previous forced-tool patch was brittle because it used prose text as a pseudo-protocol; current retry nudges are based on tool evidence, schema validation failures, required-next-tool success, and structured `next_required_action`.
5. Best-practice direction is to strengthen generic harness signals:
   - transition reason at every exit path,
   - tool-loop soft verifier,
   - explicit evidence summaries from real tool results,
   - stronger E2E API/journal checks where available,
   - no raw prompt-case routing.

## Current Completion Audit - 2026-06-02
| AgentArchitecturePatterns law / surface | Current Nerya implementation | Code logic to reference | Fresh evidence |
| --- | --- | --- | --- |
| R1 Turn is source of truth | Runtime journals `agent.turn.start/end/summary`; diagnostic `Turn` / `RolloutWriter` writes append-only JSONL using the same JSONL primitive. | `nerya/agent/kernel.py`, `nerya/rollout/writer.py`, `nerya/core/jsonl.py` | Full lint R1 passed; `tests/test_harness_audit_surfaces.py` covers rollout JSONL redaction. |
| R2 Context cache boundary | Stable cached prompt prefix is separated from volatile temporal/current-turn sections. | `nerya/agent/prompt_sections.py`, `nerya/agent/kernel.py::_build_system_prompt` | Full lint R2 passed; temporal/cache-boundary tests covered in prior plan evidence. |
| R3 External content as data | External tool content is wrapped with nonce-bearing tags before prompt reinjection. | `nerya/agent/loop.py::_wrap_external_content` | Full lint R3 passed; route/external wrapper regressions documented in notes. |
| R4 Three verifier tiers | Hard/soft/lazy verifier statuses and trust flag are computed and exposed. | `nerya/agent/verifier.py`, `nerya/agent/kernel.py`, `dashboard/tests/e2e/csv-runner.spec.ts` | Full lint R4 passed; turn stability gate remains active for prompt CSV. |
| R5 Sandbox first | Shell-class process launch uses `sandbox_exec`; tool-level path guards still block workspace escapes before launch. | `nerya/core/sandbox.py`, `nerya/tools/native/shell.py`, `nerya/tools/native/search.py`, `nerya/tools/native/skill.py`, `nerya/skills/installer.py` | Full lint R5 passed; focused sandbox/tool tests passed in the 65-test aggregation. |
| R6 Import-time redaction | Redaction env toggle is snapshotted at import; disabled path withholds text instead of returning plaintext. | `nerya/core/redaction.py` | Full lint R6 passed; `tests/test_redaction_import_boundary.py` passed. |
| R7 Fail-open scanners | Prompt guard and memory content scanner fail open on scanner exceptions with explicit audit metadata. | `nerya/security/prompt_injection.py`, `nerya/memory/content_scanner.py` | Full lint R7 passed; prompt/memory scanner fail-open tests passed. |
| R8 Frozen memory | Runtime freezes memory prompt block at turn start. | `nerya/agent/kernel.py::_freeze_memory_prompt_block` | Full lint R8 passed; frozen-memory evidence documented in task plan. |
| R9 Skill allowlist / supply chain | Built-in shipped skill allowlist is exposed via `list_bundled_skill_names()` over `skills/builtin`; `skills/bundled` exists as compatibility namespace; external installer rejects legacy executable/YAML surfaces and scans helpers. | `nerya/skills/registry.py`, `nerya/skills/bundled/__init__.py`, `nerya/skills/installer.py` | Full lint R9 passed; harness audit and builtin skill tests passed. |
| R10 Audit trail | Rollout JSONL compatibility writer, security `audit_event`, normal journals, DB tool events, and context-full logs provide audit trails. | `nerya/rollout/writer.py`, `nerya/security/audit.py`, `nerya/llm/gateway.py`, `nerya/workspace/journal.py` | Full lint R10 passed; context-full tests passed. |
| P1 Task progress | Native `todo_write` enforces single `in_progress`; `progress/todo.py` and `format_for_injection()` expose unfinished work for prompt rehydration. | `nerya/tools/native/task.py`, `nerya/progress/todo.py`, `nerya/agent/execution_state.py` | Full lint P1 passed; harness audit tests passed. |
| No prompt/tool route hardcoding | Built-in route manifests are capability-only; old web native route redirect and marker tables are absent. Business trigger `match` fields remain allowed operator data. | `nerya/agent/route_manifest_presets/*.yml`, `nerya/agent/route_manifests.py`, `tests/test_no_runtime_route_hardcoding.py`, `nerya/triggers/routes.py` | `tests/test_no_runtime_route_hardcoding.py` passed; targeted runtime route marker scan had no forbidden runtime hits. |

## Refreshed Gap Matrix - 2026-06-02

| Best-practice requirement | Current evidence | Status / remaining gap | Code logic to reference |
| --- | --- | --- | --- |
| Turn is source of truth | `AgentTurnResult`, `turn_id`, `agent.turn.start/end/summary`, full-context `call_id` correlation, and loop request metadata exist | Mostly implemented. Need final full-suite evidence, not just focused segments, before marking done. | `nerya/agent/kernel.py::AgentTurnResult`, `nerya/agent/kernel.py::_record_session_db_turn`, `nerya/agent/loop.py::WorkspaceNativeAgentLoop.run`, `nerya/llm/gateway.py::_record_context_full` |
| Context has cache/log boundary | Full context logging is opt-in and canonical request context is captured before provider formatting; system prompt now has `CACHE_BOUNDARY_MARKER` and a cache-stability test | Implemented for observability and prompt assembly. Provider-side cache token telemetry is still a future measurement task during full real-provider runs. | `nerya/llm/gateway.py::call_messages`, `nerya/agent/kernel.py::_build_system_prompt`, `nerya/agent/prompt_sections.py`, `tests/test_agent_temporal_context.py` |
| Prompt is data | External tool results can be nonce-wrapped and web route redirects were removed | Implemented for major external content path; keep verifying all new external tools route through the wrapper. | `nerya/agent/loop.py::_wrap_external_content`, `nerya/tools/native/web.py`, `nerya/tools/native/bootstrap.py` |
| Three verifier tiers | `verifier_outcome` carries hard/soft/lazy status; CSV runner checks turn stability before prose/API assertions | Implemented at turn/result surface. Need full CSV run to prove verifier distribution on all prompt cases. | `nerya/agent/verifier.py::compute_verifier_outcome`, `nerya/agent/kernel.py`, `dashboard/tests/e2e/csv-runner.spec.ts` |
| Sandbox first | Shell path-token guards and permission errors exist; no prompt-case matching is used for shell denial | Implemented for known L10 absolute-path class. Continue adding tests when new shell classes appear. | `nerya/tools/native/shell.py`, `tests/test_tool_approval_policy.py`, `tests/test_no_runtime_route_hardcoding.py` |
| Redact at import/log boundary | LLM context-full logging uses display redaction and safe tier config; secrets stay as vault refs | Implemented for new full-context logs. Import-time redaction audit across all log paths remains a broader security task. | `nerya/llm/gateway.py::_record_context_full`, `nerya/core/redaction.py`, `nerya/security/secret_scanner.py` |
| Fail-open scanners | Prompt guard now fails open if a scanner regex/search path raises; normal block/review/allow verdicts remain covered by tests | Implemented for prompt-injection guard. Broader scanners should use the same safe-helper pattern when expanded. | `nerya/security/prompt_injection.py`, `tests/test_openhuman_reference_plan.py` |
| Memory writes need frozen/reset policy | E2E reset can clear memory/profile recall for isolated cases; `AgentKernel._freeze_memory_prompt_block()` freezes the prompt memory fragment at turn start | Implemented for prompt E2E isolation and explicit turn-start prompt snapshots. Broader memory provider expansion should preserve the same snapshot boundary. | `tools/reset_workspace.py`, `dashboard/tests/e2e/global-setup.ts`, `nerya/agent/kernel.py::_freeze_memory_prompt_block`, `nerya/agent/memory.py` |
| Skills are content / supply chain | Builtin skills are `SKILL.md` first and compact; prompt playbooks are lazy-loaded under `references/`; bundled planner manifests are capability-only; new skill scaffolds and external installs reject legacy executable/YAML definition surfaces | Implemented for builtin, agent-created proposals, and external install intake. Executable helpers must live under reviewed `scripts/` and are invoked through normal tool/permission layers. | `nerya/skills/builtin/*/SKILL.md`, `nerya/skills/proposal.py`, `nerya/skills/installer.py`, `nerya/skills/manifest.py`, `nerya/agent/route_manifest_presets/*.yml` |
| Audit trail last mile | Per-case logs, `llm_context_full.jsonl`, verifier outcome, execution state, and artifact API checks exist | Implemented. Need final all-case MiniMax/no-mock run as completion evidence. | `dashboard/test-results/logs/*.jsonl`, `<workspace>/dev_logs/llm_context_full.jsonl`, `dashboard/tests/e2e/csv-runner.spec.ts` |
| Execution-state surfaces | `execution_state` separates approval plan, execution todo, tool progress, task progress, status, resume | Implemented at API/result surface. Need UI/CSV contracts to continue consuming machine state, not prose only. | `nerya/agent/execution_state.py`, `nerya/api/routes_agent.py`, `dashboard/tests/e2e/csv-runner.spec.ts` |
| No route hardcoding | Prompt/web regex redirect removed; Python route tables and packaged YAML route-match tables removed; default config only pins capability manifest `trading-v1` | Implemented for current scan. Builtin manifests may list capabilities and fallback only; explicit workspace-owned manifests remain the operator extension point for routes. Need full CSV evidence after this stricter removal. | `nerya/agent/route_manifests.py`, `nerya/agent/route_manifest_presets/*.yml`, `nerya/core/config.py`, `tests/test_no_runtime_route_hardcoding.py` |

## Static Lint Audit - 2026-06-02 Current State

`AgentArchitecturePatterns/scripts/lint-agent-design.py` was run against `Nerya/nerya`. Treat this as a diagnostic because the script expects scaffold names like `rollout/writer.py`, `security/redact.py`, and `skills/bundled/`, while Nerya uses different module names.

| Rule | Lint result | Nerya evidence / classification | Follow-up |
| --- | --- | --- | --- |
| R1 Turn source of truth | Pass | Added a thin `nerya/rollout/writer.py` compatibility surface with `Turn` / `RolloutWriter` backed by Nerya JSONL journals; existing `agent.turn.start/end/summary` remains authoritative. | Keep final proof on all-case real-provider run evidence. |
| R2 Cache boundary | Pass | `CACHE_BOUNDARY_MARKER` and cache-stability tests exist. | None before full CSV. |
| R3 External content wrap | Was warn, now pass | Real gap closed this pass: wrapper now uses nonce tag names (`external_content_<nonce>`) instead of fixed tag names/attributes. | Keep adding tests for new external tools. |
| R4 Verifier tiers | Pass | `verifier_outcome` has hard/soft/lazy labels and trust flag. | Prove distribution on full CSV. |
| R5 Sandbox first | Pass | Real gap closed: `sandbox_exec` now exists in `nerya/core/sandbox.py`, and shell/search/skill-run/external skill clone paths route process execution through it. | Keep OS-specific hardening centralized in `core/sandbox.py`; full CSV still remains the runtime proof. |
| R6 Import-time redaction | Pass | `nerya/core/redaction.py` now snapshots `_REDACT_ENABLED = os.getenv(...)` at import and withholds text instead of exposing plaintext when disabled. | Keep full-context logs redacted; do not introduce runtime redaction toggles that can change mid-turn. |
| R7 Fail-open scanners | Pass | Memory content scanner now exposes an audited fail-open envelope; prompt guard already fails open on scanner exceptions. | Broaden the same audited scanner result pattern when new scanner modules are added. |
| R8 Frozen memory | Pass | `AgentKernel._freeze_memory_prompt_block()` freezes prompt memory at turn start. | None before full CSV. |
| R9 Skill allowlist/supply chain | Pass | Added `list_bundled_skill_names()` compatibility allowlist and `skills/bundled` namespace marker over the existing `skills/builtin` shipped skill set; external install scanner remains active. | Broaden scanner rules when new executable helper types are allowed. |
| R10 Audit trail | Pass | Added rollout JSONL writer compatibility surface and explicit `audit_event` envelope in `nerya/security/audit.py`; context-full logs remain the LLM request audit trail. | Full CSV remains the runtime audit proof. |
| P1 Task progress | Pass | Added `nerya/progress/todo.py` compatibility surface over `TaskState` and `format_for_injection()` for unfinished work, while preserving the existing single `in_progress` guard. | Continue ensuring UI/CSV consume machine state, not prose only. |

## Implementation Phases

### Phase A - Remove hardcoded routing
- Remove newly added category regex/tool forcing from `loop.py`.
- Replace `kernel.py` regex focus blocks with general non-routing guidance:
  - current facts require live evidence,
  - strategy/backtest work should use strategy tools and artifact evidence,
  - trading orders require safety parameters,
  - browser work should stay scoped to the user's latest request.
- Keep safety behavior as general policy, not prompt classification.

### Phase B - Add generic transition reasons
- Introduce a small `transition_reason` value in `LoopOutcome` or an adjacent metadata field.
- Set it for paths like `no_tool_use`, `tool_use_continue`, `max_iterations`, `max_tool_calls`, `timeout`, `cancelled`, `repeated_tool_call`, `interrupted_max_tokens`, and provider retry exhaustion.
- Journal transition reason with `agent.turn.end` and expose it in the API result.

### Phase C - Strengthen soft verifier
- Keep generic tool-loop detection based on stable tool-name + args fingerprint.
- Surface suppression as a real tool_result with an error kind and recovery hint.
- Abort with `transition_reason=repeated_tool_call` after repeated no-progress signals.
- Do not infer desired tools from user prompt or final prose.

### Phase D - Evidence contract for E2E
- Extend per-case logs to include:
  - turn ID,
  - stop reason,
  - transition reason,
  - tool names used,
  - artifact index summary,
  - API check evidence.
- Where case intent is artifact-based, assert API/journal/artifact evidence instead of only final prose.
- Do not loosen assertions to accept hallucinated summaries.

### Phase E - Targeted remediation
- Re-run failed cluster: B4, B7, B8, B12, C1, C2, C5, C7, C8, C9, C11, C13, C15, C16, C18 if present.
- For each failure, classify:
  - missing real tool evidence,
  - stale test expectation,
  - product/runtime missing capability,
  - external source unavailable,
  - provider protocol failure.
- Fix the reusable layer only.

### Phase F - Verification
- Python syntax: `python -m py_compile nerya\agent\loop.py nerya\agent\kernel.py nerya\api\routes_llm.py`.
- Unit tests: start with the existing loop/config/concurrency tests previously used.
- E2E typecheck: `npx tsc --noEmit --project tests/e2e/tsconfig.json`.
- Targeted Playwright run on failed cases.
- Full `npx playwright test csv-runner --reporter=list` against runtime `:18318` and dashboard `:3001`.

## Non-Goals
- No mock provider fallback unless explicitly allowed by `NERYA_E2E_ALLOW_MOCK_LLM=1`.
- No case-id-specific code paths.
- No prompt regex expansion to chase failing cases.
- No unrelated UI redesign or broad cleanup while the E2E harness is unstable.

## Completion Evidence Needed
- Diff contains no new hardcoded prompt/category regex route patches.
- New/updated tests cover transition/tool-loop behavior.
- Targeted failed cases are rerun with logs read.
- Full CSV run completes with all cases passing or with remaining failures classified by concrete external blocker/product gap.

## 2026-06-02 Route Manifest Resourceization
- Remaining source-level route hardcoding found: `nerya/agent/route_manifests.py` and `nerya/core/config.py` still embedded planner route tables with `match` arrays and `text_contains` escalation strings.
- Fix: moved bundled route presets into `nerya/agent/route_manifest_presets/*.yml`, rewrote `route_manifests.py` as a declarative loader, and changed `DEFAULT_CONFIG.agent.planner` to pin `manifest: trading-v1` with `routes: {}`.
- Guardrail: `tests/test_no_runtime_route_hardcoding.py` now fails if default config regains inline routes or if `route_manifests.py` regains Python route preset literals.
- Verification: `python -m pytest tests/test_no_runtime_route_hardcoding.py -q` passed (`3 passed`); route/focus/context pytest subset passed (`5 passed`); py_compile passed; forbidden marker scan returned no matches.

## 2026-06-02 Source Evidence Auditability
- B4 showed a stable real-provider turn can gather URL/year source evidence and still produce final prose without audit markers. The harness should preserve already gathered source evidence instead of loosening CSV assertions or adding prompt/case routing.
- Fix direction: source-evidence finalization appends a redacted evidence footer only when source tools succeeded and final prose lacks source markers. This keeps the final answer tied to tool evidence while avoiding case IDs, prompt regex routing, or ticker-specific logic.
- Verification: focused loop/source-evidence tests passed (`6 passed`), context-full tests passed (`4 passed`), no-runtime-route-hardcoding passed (`3 passed`), forbidden marker scan returned no matches, and focused real MiniMax B4 passed with yolo/no-mock/context-full logging.

## 2026-06-02 Builtin Route Manifest Hardcoding Deletion
- Strict audit corrected the earlier "YAML resourceization is enough" assumption. Packaged YAML route presets were still hardcoded route match tables, so the builtin manifests were reduced to capability-only records with `fallback: generic`.
- `route_manifests.py` now accepts empty route maps, and `routes_capability._planner_section` no longer falls back to legacy inline `agent.planner.routes` when a manifest is explicitly selected.
- Verification: no-hardcoding regression now covers packaged YAML `routes/match` absence and active-manifest no-fallback behavior (`5 passed`); strategy-context/no-hardcoding passed (`24 passed`); execution-state/context-full subset passed (`5 passed`); py_compile passed; forbidden marker scan had no runtime route-hardcoding hits.

## 2026-06-02 Research Versus Strategy Boundaries
- D8 showed that evidence-based routing can still be over-broad: `team_run` and shell+market exploration are valid research tools, but they are not sufficient proof that a strategy proposal must be generated.
- Fix direction: require explicit strategy tools or account/connector/data-source authoring prep before adding `strategy_generate_proposal` as required next action. Research Team outputs can be synthesized directly, and high-volume source evidence gets an earlier compact-synthesis reserve.
- Verification: focused research/source loop tests passed (`8 passed`), strategy-context guidance passed (`21 passed` with no-runtime-route-hardcoding), forbidden marker scan returned no matches, and focused real MiniMax D8 passed with stable turn evidence.

## 2026-06-02 Portfolio Alerts Are Not Strategy Authoring
- D10 showed another over-broad boundary: `journal_search`, portfolio evidence, and `strategy_list` can support a risk/InBox response without implying a strategy package must be generated.
- Fix direction: keep proposal-required nudges behind explicit strategy workflow tools or role/team proposal scoping; plain portfolio/risk evidence should synthesize or report gaps directly.
- Verification: focused portfolio-alert regression passed, strategy-context guidance stayed green, forbidden marker scan returned no matches, and focused real MiniMax D10 passed with stable turn evidence.

## 2026-06-02 Readiness Contracts Beat Keyword Defaults
- E6 showed that group-level defaults can misrepresent product intent. A data-source readiness case should verify the actual readiness endpoint and require configuration/gap wording, not inherit `team` from the E group.
- Fix direction: encode provider readiness expectations in `api_check` with explicit true/false values and regenerate CSV from `tools/extract_cases.py`.
- Verification: extract-case tests passed, E2E TypeScript compile passed, and focused real MiniMax E6 passed with `financial datasets ready=false`.

## 2026-06-02 Team Evidence Must Be API-Backed And Synchronous
- E7 showed that a concurrency test cannot rely on final prose containing `team`: direct parallel `market_data` can answer the vague prompt without exercising AgentTeam. The test plan now makes the AgentTeam/max_parallel intent explicit, and CSV validates `team_run_exists=true` against `/teams/runs` so a fake prose mention cannot pass.
- E7 also showed a synchronization boundary gap after a successful `team_run`: the model may still call async task tools as if the team run were a task id. `subagent_run_async_handler` now mirrors existing `task_get`/`task_list` behavior by returning the cached synchronous team summary and `next_action` instead of creating a new background task in the same turn.
- Verification: focused unit regressions passed, no-runtime-route-hardcoding stayed green, and focused real MiniMax E7 passed with only `team_run`/`todo_write` evidence and no async-task pollution.

## 2026-06-02 Context-Full Request Diagnostics
- Context-full logging now captures the complete canonical LLM request and adds compact Agent loop correlation fields: `session_id`, `turn_id`, `iteration`, tool progress, required next tools, `llm_attempt`, sent message/tool counts, safety-retry state, and remaining wall time.
- This keeps normal logs compact by default while making `llm_context_full.jsonl` sufficient for post-failure analysis of every provider request in an Agent turn. Secrets still pass through display redaction and tier config records expose only safe key-ref presence flags.
- Verification: TDD correlation test failed before `llm_attempt`/count fields were present, then passed. Focused context-full gateway tests also passed (`4 passed`).
- Follow-up: request/response/error records now expose the safe correlation fields at top level, so failures can be grouped by `session_id` / `turn_id` / `iteration` without first joining to the request record. Redaction also covers synthetic vendor-prefixed tokens and hex-dot provider key shapes before writing the full context log.

## 2026-06-02 Prompt Cache Boundary
- AgentArchitecturePatterns requires a named cache boundary and a stability test proving volatile turn data does not leak into the cached prefix. Nerya previously had `PromptComposer` scaffolding, but `_build_system_prompt()` still rendered a flat prompt with `Temporal context` before any boundary.
- Fix: added `CACHE_BOUNDARY_MARKER` in `prompt_sections.py` and split `_build_system_prompt()` into cached and rolling sections. The cached prefix carries stable identity/workspace/memory/skill/workflow material; the rolling section carries `Temporal context`, latest-turn execution policy, output-language instruction, permission mode, and attached-skill hints.
- Verification: the new cache-boundary test failed before implementation, then passed. `tests/test_agent_temporal_context.py` passed (`13 passed`), and `py_compile` for `kernel.py` / `prompt_sections.py` passed.

## 2026-06-02 Frozen Memory Prompt Snapshot
- AgentArchitecturePatterns Law 8 requires memory writes to land in durable storage without changing the in-flight turn's prompt. Nerya's normal turn path built the system prompt once, but the boundary was implicit and `_build_system_prompt()` could still read live memory if called again in the same turn.
- Fix: added `AgentKernel._freeze_memory_prompt_block()` and made `run_turn` pass the frozen memory string into `_build_system_prompt()`. The prompt builder can still freeze at call time for tests/tools that call it directly, but the runtime turn path now has an explicit snapshot boundary.
- Verification: the new frozen-memory test failed first because the snapshot API did not exist, then passed after implementation. Full `tests/test_agent_temporal_context.py` passed (`14 passed`), `py_compile` for `kernel.py` passed, `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`), and the forbidden-marker scan only found eval `expected_final_text_contains` fields.

## 2026-06-02 Prompt Guard Fail-Open
- AgentArchitecturePatterns Law 7 requires scanners/verifiers to fail open by default so a scanner crash does not kill the agent turn and push users to disable safety wholesale.
- Root gap: `prompt_injection.flag_suspicious()` and `classify()` directly called `Pattern.search()`. If a scanner object or regex engine path raised, the caller would crash rather than returning a safe default verdict.
- Fix: added `_safe_pattern_hits()` to return `(hits, failed)` and log a warning on scanner exceptions. `flag_suspicious()` now returns an empty hit list on scanner failure; `classify()` returns `{verdict: allow, hits: [], policy: prompt_guard.fail_open}`. Normal `block`, `review`, and `allow` behavior remains unchanged.
- Verification: the new fail-open test failed before implementation, then passed. Prompt guard smoke subset passed (`6 passed`), and `py_compile` for `nerya/security/prompt_injection.py` passed.

## 2026-06-02 Context-Full Observability Recheck
- Best-practice status: context-full logging is now sufficient for post-failure prompt analysis without enabling broad HTTP body dumps. The log captures the canonical request before provider formatting, so the same artifact works across MiniMax, MiMo, GLM, and mock-disabled E2E providers.
- Safety boundary: full context is still passed through display redaction, and tier config records expose only safe key-ref/env-presence fields rather than plaintext credentials.
- Operator workflow: for real Playwright prompt runs, keep `NERYA_CONTEXT_FULL_LOG=1`, yolo/non-gating permissions, and no-mock provider checks enabled; inspect `<workspace>/dev_logs/llm_context_full.jsonl` by `session_id`, `turn_id`, `iteration`, `llm_attempt`, and `call_id`.
- Verification: focused context-full tests, Agent loop metadata test, no-runtime-route-hardcoding, py_compile, and runtime route-marker scan all passed in the current recheck.

## 2026-06-02 Context-Full Subagent Correlation Closure
- Fresh context audit found that the main Agent loop messages path had complete turn metadata, but legacy prompt-style LLM calls had no metadata channel. This mattered for subagent/team analysis because `SubAgentRuntime` uses `LLMGateway.call()` rather than `call_messages()`.
- Fix: `LLMGateway.call()` now accepts optional safe metadata, writes it into prompt API context-full request records, and promotes allow-listed correlation fields onto request/response/error records. The allow-list now includes `subagent`, `strategy_id`, `trigger_event_id`, and `parent_call_id` in addition to existing Agent turn fields.
- `SubAgentRuntime.run()` now passes `session_id`, `turn_id`, `iteration`, `subagent`, `strategy_id`, `trigger_event_id`, and `parent_call_id` to its LLM call. This makes team/subagent context-full records joinable without dumping arbitrary metadata at top level.
- Verification: the new prompt API context-full test failed first with `LLMGateway.call() got an unexpected keyword argument 'metadata'`, then passed after implementation. Focused context-full tests passed (`5 passed`); Agent loop + subagent metadata regressions passed (`2 passed`); no-runtime-route-hardcoding passed (`5 passed`); `py_compile` for gateway/runtime passed.

## 2026-06-02 Skill Supply-Chain Lazy-Load Closure
- Best-practice target: Law 9 says skills are content and loadable code is supply chain. New Nerya-authored skill proposals must not create legacy `actions.py`/YAML definition surfaces, and builtin `SKILL.md` files should stay compact enough to act as progressive-disclosure entrypoints.
- Root gaps found in current audit: `nerya/skills/proposal.py` still scaffolded `actions.py`, and builtin `browser`, `news_social`, `strategy_author`, and `backtest` entrypoints exceeded the compact skill budget.
- Fix: the legacy scaffolder now keeps its compatibility signature but ignores the deprecated executable body and creates only `SKILL.md`, `references/`, `scripts/`, and `templates/`. The long builtin entrypoints were compacted, and detailed browser/news/backtest rules were kept under `references/full-playbook.md`.
- Verification: new regression failed before the scaffolder fix and passed after. `tests/test_evolve_skill_proposal.py` passed (`3 passed`), `tests/test_builtin_skill_catalog.py tests/test_no_runtime_route_hardcoding.py` passed (`10 passed`), and a line-count scan returned `over_limit=[]`.
- Follow-up hardening: `install_skill` now rejects root-level `actions.py`, `skill.yml`, `skill.yaml`, `manifest.yml`, and `manifest.yaml` before staging external skills. `tests/test_routes_skills_dashboard.py` gained a failing-first regression proving the pending tree is not written for that shape.
- Verification after installer hardening: `tests/test_evolve_skill_proposal.py tests/test_routes_skills_dashboard.py tests/test_builtin_skill_catalog.py tests/test_no_runtime_route_hardcoding.py` passed (`21 passed`), `py_compile` for skill installer/proposal modules passed, route-marker scan had no runtime matches, and builtin skill line-count scan stayed `over_limit=[]`.

## 2026-06-02 External Content Nonce Boundary Closure
- Static lint flagged R3 because Nerya used `secrets.token_hex(8)` but rendered `<external_data nonce="...">` instead of nonce-bearing tag names. The old form included the nonce, but the fixed tag name was weaker than the AgentArchitecturePatterns/OpenClaw pattern and harder for static lint to prove.
- Fix: `_wrap_external_content()` now emits `<external_content_<nonce>> ... </external_content_<nonce>>`, with the same unpredictable tag name on both boundaries and the existing "data, NOT instructions" warning.
- Verification: the new failing-first regression `test_external_tool_content_uses_nonce_tag_name_boundary` failed on the old `<external_data ...>` form, then passed after the fix. AgentArchitecturePatterns R3 lint now passes. Related route/external wrapper subset passed with `21 passed`.

## 2026-06-02 External Skill Scanner Closure
- Static lint R9 is still a naming false positive for bundled skills (`nerya/skills/builtin` rather than `skills/bundled`), but the audit found a real supply-chain gap: `_static_analyze()` returned an empty list, so user-installable skills had no script/binary scanner beyond legacy surface rejection.
- Fix: external install now scans before staging. Critical findings block staging for blocked native/binary extensions and oversized files; high findings are recorded for dangerous script markers such as shell execution or dynamic eval so the operator can review them in `install_report.json` before proposal approval.
- Verification: new installer scanner tests failed first because binary helpers were staged and script findings were empty, then passed after implementation. Related supply-chain/no-hardcoding subset passed (`21 passed`) and `py_compile` for `installer.py` passed.

## 2026-06-02 Context-Full Edge Correlation Closure
- Best-practice target: the audit trail should make each LLM request attributable to an Agent turn, parent tool call, or explicit internal scope.
- Fix: context-full correlation now promotes `context_scope`; team final synthesis, session-title generation, subagent prompt calls, and native `llm_*` tool subcalls all pass safe metadata into `LLMGateway`. Full request bodies remain redacted and canonical; arbitrary metadata is still stored under `request.metadata` but only allow-listed scalar/list fields are promoted to the record top level.
- Verification: failing-first regressions covered top-level `context_scope`, team final synthesis metadata, session-title metadata, and native LLM tool parent-call metadata. Focused context-full tests passed (`5 passed`), metadata regressions passed (`6 passed`), native LLM metadata tests passed (`2 passed`), no-runtime-route-hardcoding passed, `py_compile` passed, and the forbidden marker scan had no runtime marker hits.

## 2026-06-02 R5-R7 Harness Closure
- R5 sandbox-first is now closed against the AgentArchitecturePatterns lint: `sandbox_exec` centralizes foreground/background process execution in `nerya/core/sandbox.py`, and shell-class tool paths no longer call `subprocess.run` / `subprocess.Popen` directly.
- R6 import-time redaction is now closed: `nerya/core/redaction.py` snapshots `_REDACT_ENABLED` from the environment at import time, and the disabled path withholds text rather than returning plaintext.
- R7 fail-open scanner is now closed for the audited memory-content scanner path: `scan_memory_content_with_audit()` returns `MemoryScanResult(True, audit_event={policy: memory_content_scanner.fail_open, ...})` on scanner exceptions, while the legacy `scan_memory_content()` API remains compatible.
- Additional cleanup: `news_social/SKILL.md` regained its compact `triggers` frontmatter after the lazy-load test showed the compacted skill entrypoint had dropped trigger metadata.
- Verification: AgentArchitecturePatterns R5/R6/R7 lint passed (`3 passes, 0 fails`); focused pytest aggregation passed (`65 passed, 3 deselected`); context-full/no-hardcoding passed (`5 passed, 24 deselected`); py_compile for touched Python files passed; forbidden marker scan found only eval `expected_final_text_contains` fields and no runtime route markers.

## 2026-06-02 Full Static Best-Practice Lint Closure
- Closed the remaining naming-alignment gaps without replacing Nerya's runtime architecture: `nerya/rollout/writer.py` exposes a standard `Turn` / `RolloutWriter` JSONL surface over existing journals; `nerya/security/audit.py` writes an explicit `audit_event`; `nerya/skills/registry.py::list_bundled_skill_names()` exposes the shipped skill allowlist; `nerya/progress/todo.py` exposes the task-progress surface over native `TaskState`.
- These are compatibility/audit surfaces, not prompt routers. They do not add case IDs, regex intent markers, or mock-provider fallback paths.
- Verification: full `lint-agent-design.py` over `Nerya/nerya` passed all rules (`10 passes, 0 fails, 0 advisories`); focused pytest aggregation passed (`65 passed, 7 deselected`); context-full/no-hardcoding passed (`5 passed, 24 deselected`); touched-file py_compile passed; forbidden marker scan found only eval/test `expected_final_text_contains` fields.

## 2026-06-02 Final Static Harness Verification
- Re-ran the current objective gates after updating the completion matrices. Full AgentArchitecturePatterns lint passed (`10 passes, 0 fails, 0 advisories`).
- Re-ran the focused implementation gates separately so `-k context_full` did not narrow unrelated tests: no-hardcoding/harness/sandbox/redaction/memory scanner tests passed (`25 passed, 5 deselected`), and context-full tests passed (`5 passed, 19 deselected`).
- Re-ran safety scans: exact search for the supplied API key fragments returned no matches, and forbidden route/intent marker search returned no matches.
- Result: the static harness-best-practice objective is implemented and verified. The next runtime phase is a full real MiniMax/yolo/no-mock Playwright CSV run with context-full logging enabled.

## 2026-06-03 Historical Failure Runtime Gate
- User narrowed runtime verification from full CSV to the previously failed cases. The active provider/runtime gate stayed real MiniMax only: `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `permission_mode=yolo`, `NERYA_CONTEXT_FULL_LOG=1`, and no mock LLM allowance.
- The final historical set command used `NERYA_CASES_ONLY=C7,E8,E10,GX6,GX14,H7,H9,I6,J1` and passed all 9 cases in one Playwright run (`9 passed`, 17.3m).
- Best-practice closure from this runtime pass:
  - Tool boundary: complete provider `_raw` JSON-object tool arguments are normalized before schema validation in `NativeToolExecutor`; schema validation and permission checks still run afterward.
  - Backtest data boundary: standard OHLCV backtests now fail fast for explicit markets whose venue is not configured/discovered, instead of silently probing unrelated venues or burning the turn budget on unsupported provider fallbacks.
  - Auditability: the final run kept per-case compact logs under `dashboard/test-results/logs/`, screenshots under `dashboard/test-results/screenshots/`, summary at `dashboard/test-results/summary.csv`, and full context records at `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl`.
- Verification evidence:
  - Unit/focused checks passed: `tests/test_tool_errors.py` (`26 passed`), combined tool/backtest subset (`9 passed`), loop/provider subset (`7 passed`), market data discovery subset (`2 passed / 1 skipped`), no-runtime-route-hardcoding (`5 passed`), and `py_compile` for touched files.
  - Focused real `GX14` passed after runtime restart (`1 passed`, 5.0m). Combined historical rerun then passed with `GX14` at 223166 ms and transition `strategy_backtest_data_gap_finalized`.
- Remaining risk: setup output still reports `approvals/pending.jsonl` as skipped during reset. The historical set is clean, but approval-isolation cases should not be considered closed until that reset behavior is rechecked or intentionally documented.

## 2026-06-04 Catalog Compaction and Same-Tool Follow-Up Closure
- Best-practice target: tool result summaries must preserve actionability. A compacted catalog must not hide connector IDs, wallet actions, aliases, or `next_required_action`; otherwise real-provider prompt tests can pass assertions while the model reasons from false evidence.
- Root gap: generic `json.large` compaction treated connector/data catalogs like arbitrary JSON. For `connector_list(query="binance")`, it exposed nested credential `status=missing` as if the connector itself was missing and dropped the actual `count=5` connector evidence. For `data_api(op=list, provider=wallet)`, it dropped the `wallet.readiness` action and next-step contract.
- Fix:
  - `nerya.llm.tool_compaction` now has dedicated reducers for connector catalogs and data_api catalogs.
  - `nerya.data_api.registry` now returns an explicit wallet readiness follow-up for wallet/provider surface discovery.
  - `nerya.agent.loop` now distinguishes "same tool completed a catalog call" from "same tool has completed the required follow-up action"; same-tool `next_required_action` remains pending until the follow-up result arrives.
- Why this is not hardcoding: no case IDs, prompt markers, keyword route tables, or regex intent routers were added. The behavior is driven by structured tool results (`next_required_action`, provider/action catalog shape, and actual tool names).
- Runtime evidence:
  - Real MiniMax/yolo/context-full I5: iteration 2 exposed only `data_api`, with `required_next_tool_names=["data_api"]`; the model then called `wallet.readiness` and finalized with `wallet_provider_readiness_blocked_finalized`.
  - Real MiniMax/yolo/context-full H10: proposal enforcement narrowed iteration 2 to `evolve_skill_proposal`; the case finalized with a `skill_proposal`.
  - Combined `H10|I5` passed (`2 passed`, 2.0m), and E1 was also confirmed passing before stopping an accidentally overbroad regex run.
- Verification gates passed:
  - `python -m py_compile nerya\agent\loop.py nerya\llm\tool_compaction.py nerya\data_api\registry.py tests\test_agent_loop_final_summary.py tests\test_tool_compaction_data_api.py tests\test_data_api_tool.py`
  - Focused compaction/data_api tests: `5 passed`.
  - Focused loop/wallet required-action tests: `4 passed`; wider loop subset: `13 passed`.
  - `tests/test_no_runtime_route_hardcoding.py`: `5 passed`.
- Operational note: context-full logging now provides enough evidence to diagnose these failures, but dev logs need rotation before broad reruns because `http.jsonl` reached ~148 MB.

## 2026-06-04 Provider-Native Required Tool Choice
- Follow-up hardening: required-action narrowing now uses provider-native `tool_choice` when exactly one pending required action tool remains. This preserves the evidence-driven policy while reducing prompt-only compliance risk for MiniMax and other OpenAI-compatible providers.
- The loop clears `tool_choice` for text-only final synthesis, so forced tool selection does not leak into the final answer path.
- The latest MiniMax combined evidence is `H10,I5` passing with context-full enabled: `H10` finalized via `evolve_skill_proposal`; `I5` finalized after the required `data_api` wallet readiness follow-up.
- Verification stayed aligned with the no-hardcoding rule: local compaction and loop subsets passed, `tests/test_no_runtime_route_hardcoding.py` passed, and no case IDs / prompt regex / intent marker routing were added.

## 2026-06-04 Runtime Evidence Boundary Follow-up
- C-AT9 exposed two harness-level protocol gaps rather than a prompt-routing problem:
  - Approval/waiver `next_required_action` values must remain operator gates and must not be converted into forced `strategy_promote` calls.
  - When the harness narrows `tools` to a required action, provider-returned tool calls outside that exposed set are raw provider noise and must not be executed.
- The implemented boundary is generic: approval-gated next actions are excluded from required-tool forcing, and unexposed provider tool calls are ignored at execution time. No case IDs, keyword routers, or intent marker tables were added.
- Real MiniMax C-AT9 rerun passed with execution evidence in `turn.evidence.tool_names`; context-full raw response tools remain useful diagnostics but are not execution truth.
- Revalidated stale archive failures before changing behavior:
  - `C-AT2` now passes with strategy proposal + backtest evidence; the old provider-proposal finalizer path is closed by existing strategy-prep guards.
  - `E1` now passes with durable `market_analysis_team` API evidence.
  - `L9` now passes with a stable `refuse (tool_abuse)` answer and zero tool calls.
- Reset risk reclassified: `approvals/pending.jsonl` safe reset is covered by `tests/test_reset_workspace.py`; setup `skip` means missing file, not preserved state.
- Remaining runtime gate: rotate/clear large isolated dev logs, run the broader high-risk/historical case set, then start a full 160-case MiniMax/yolo/no-mock CSV run if the broader set stays clean.

## 2026-06-05 Turn Correlation and Backtest Verdict Auditability Follow-up
- Context-full observability gap found during E10 audit: the provider request records used an internal loop turn id instead of the API/journal/case `turn_id`. This weakened the audit trail because failed CSV logs could not be joined directly to full request context by `turn_id`.
- Fix: `LoopConfig.turn_id` carries the kernel/API turn id into the workspace-native loop; all loop block envelopes, tool calls, and `LLMGateway.call_messages(...metadata.turn_id...)` now share the same id. Standalone loop tests keep an internal fallback id.
- Backtest auditability gap found during the same E10 audit: `strategy_backtest` `verdict=FAIL` was flattened to metric key names in deterministic final text. The finalizer now preserves verdict, selected display metrics, operator summary, and review-gate metadata so a completed-but-failing standard replay is not presented as promote-ready.
- Wall-clock safety gap closed: when late action tools are skipped to preserve UI responsiveness, completed strategy proposal evidence is kept in the final text instead of returning only a generic harness timeout message.
- This remains evidence-driven and non-routing: no case IDs, prompt marker arrays, regex intent routing, mock fallback, or provider-specific prompt hacks were added.

## 2026-06-05 E12 Team Split-Language Contract Closure
- Best-practice gap: E12 used a prose-only inherited team assertion and an underspecified prompt, so the harness had no durable evidence contract for "team" or for the cross-language requirement.
- Fix: the CSV source now encodes E12 as an explicit AgentTeam research task and requires `team_run_exists`, `team_output_language=English`, and `team_analysis_language=Chinese`. The runner checks those values through TeamStore metrics rather than final-reply regex.
- Tool contract: `team_run` now separates `analysis_language` from `output_language`, propagates both to member payloads/events, and persists both in `final_context`/TeamStore metrics for auditability.
- Prompt contract: explicit final/report/deliverable language takes precedence over surrounding prompt language, and team calls must map split-language requests into structured tool parameters.
- Verification: extract-case, team streaming, temporal prompt, py_compile, TypeScript e2e compile, no-runtime-route-hardcoding, and forbidden marker scans passed. This is a generic contract/evidence repair, not a case-id route or intent-marker patch.
- Runtime closure: real MiniMax/yolo/context-full/no-mock E12 passed with a single `team_run` and API evidence for `team_run_exists`, `output_language=English`, and `analysis_language=Chinese`. Fresh post-fix gates passed: full team streaming (`28 passed`), extract/temporal/no-hardcoding (`31 passed`), E2E TypeScript compile, and touched-file `py_compile`.

## 2026-06-05 E13 Team Contract / Runtime Budget Closure
- Best-practice gap: E13 tested an AgentTeam concurrency requirement with a final-text `team` assertion and did not verify the actual role count. Passing prose could hide wrong team shape.
- Contract fix: E13 now validates durable TeamStore evidence with `team_run_exists=true:team_roles_total=3`; the runner checks `metrics.roles_total` through `/teams/runs`.
- Runtime fix: explicit `roles` are authoritative unless the task is a full market rating/target-price workflow that requires template completion. `team_run` also preserves a viable 30s minimum execution budget, preventing real MiniMax child LLM calls from being killed by literal `5s` quick-deadline wording.
- Observability fix: TeamStore metrics include role totals, success/failure counts, parallelism, and timeout budget so future CSV cases can assert team shape without reply regex.
- Evidence: real MiniMax/yolo/context-full/no-mock E13 passed after restart with `roles_total=3`, `roles_succeeded=3`, `roles_failed=0`, `max_parallel=3`, and `timeout_s=60.0`. Local gates passed and forbidden intent-marker scan had no matches.

## 2026-06-05 Reflection Evidence Debt Closure
- Best-practice gap: a diagnostic/reflection turn could gather enough evidence for a learning proposal and still finish with prose because the harness trusted final text more than the evidence/proposal contract. The first repair was too narrow because it required a `virtual_ledger` call that the real model did not need to make.
- Runtime fix: reflection proposal debt is now derived from structured tool-result evidence. A non-trading `portfolio_pnl` realized delta plus either no-trade ledger evidence or empty strategy inventory + empty journal evidence requires `evolve_reflect`. Empty journal lookups alone and inventory-only evidence still do not trigger learning proposals.
- Required-action lifecycle fix: repeated pending-tool nudges are de-duplicated through `next_action_nudges`; a model that already received the same required-action nudge is not trapped in an unbounded third prompt loop.
- Strategy boundary fix: assistant choice/confirmation text can become strategy proposal debt only when completed tool evidence already supports a strategy package path. This avoids prompt/case routing while preventing "please choose" prose from hiding an unattempted action tool.
- Evidence: local loop suite passed (`154 passed`), extract/no-hardcoding passed (`16 passed`), py_compile passed, and real MiniMax/yolo/context-full F1 passed with `evolve_reflect` and `proposal kind=learning_update ok`. The full F2-F12 segment also passed (`11 passed`, 12.7m).

## 2026-06-05 Required-Action Tool Schema Narrowing Closure
- Best-practice gap: MiniMax can ignore a provider-native `tool_choice` or spend the whole wall-clock budget on complex tool schemas even after the harness has already narrowed the action surface to a single required native tool. H10 exposed this as a real-provider timeout, not a mock path or route-hardcoding issue.
- REF-aligned runtime fix: required native action calls are now treated as deterministic tool-emission steps. The loop uses `temperature=0`, `reasoning_effort=none`, no reasoning summary, and reduced token budgets when the exposed tools are only pending required action tools. Compact required-tool recovery uses a 1024 token cap.
- Schema-boundary fix: required-action tool exposure uses compact, top-level required-only schemas. This mirrors the REF pattern of making recovery calls small and actionable while preserving normal full schemas for ordinary tool discovery and non-required calls.
- Why this is not hardcoding: the behavior is driven by structured loop state (`pending_required_action_tools`), the exposed tool set, and JSON Schema `required` fields. No case IDs, prompt regex routers, `_STRATEGY_INTENT_MARKERS`, or provider/mock fallbacks were added.
- Evidence: local loop suite passed (`160 passed`), no-hardcoding/extract/context metadata passed (`18 passed`), e2e TypeScript compile passed, and real MiniMax/yolo/context-full H10 passed with `tool_names=["evolve_skill_proposal"]`, proposal `prp_7ceba6ef8648`, and `proposal kind=skill_proposal ok`.
- Remaining gate: continue the broader MiniMax/yolo/no-mock CSV run after rotating isolated dev logs; stop on the next real failure and repair from per-case plus context-full logs.

## 2026-06-06 Runtime Verification Discipline Update
- Provider/workspace setup is now part of the verification contract. A green or red CSV result is not trusted unless setup logs show runtime/proxy workspace match, non-mock live LLM probe, live tool probe, `provider=minimax-cn`, `model=MiniMax-M3`, and `permission_mode=yolo`.
- `PLAYWRIGHT_AUTOSTART=1` is safe only when the E2E provider override path is passed deliberately. Otherwise it reruns `prepare_isolated_test_workspace.py` from the source workspace and can revert the isolated workspace to stale MIMO config. For focused current-code audits, manually started runtime/dashboard processes with absolute `NERYA_WORKSPACE` are preferred.
- The 2026-06-06 `K4,K7` rerun closed the previous required-artifact uncertainty under this discipline: `K4` created a `task_create` agent schedule, and `K7` completed `team_run` + `evolve_skill_proposal` + `task_create` before finalizing. This reinforces the best-practice rule that durable artifact contracts must be verified through successful tool results, not only final prose.

## 2026-06-07 Tool Evidence Fidelity Follow-up
- H4, B10, and H7 exposed the same best-practice class from different directions: tool observations must preserve the fields needed for user-facing synthesis, and deterministic finalizers must not preempt the semantic task unless the evidence is actually terminal.
- Evidence-fidelity fixes are generic: short JSON/API `web_fetch` responses retain bounded `response_json`, source/data markers carry response metadata, API markers expose scalar path/quote facts to preserve unit direction, and `data_source_status` has a dedicated compact reducer for summary/source/event rows.
- Finalizer timing is now stricter: `data_source_status` finalizes only near iteration/wall-clock terminal conditions; otherwise it is returned as an observation so news/search/RSS or other relevant tools can continue.
- Verification stayed within the no-hardcoding guardrail: local compaction and loop subsets passed, `tests/test_no_runtime_route_hardcoding.py` passed, and real MiniMax/yolo/context-full `H7` plus `B10,H7` reruns passed without mock LLM allowance.

## 2026-06-07 Messaging Config Proposal Boundary Follow-up
- Best-practice target: agent-authored self-config changes must remain proposal-only, but the proposal after file must still be a real runtime schema. A green proposal-exists check is not enough if approval would apply fields no gateway code consumes.
- Gap found from `J3/J4`: message-channel proposals could contain model-draft keys or omit `kind`, making the approved config non-executable or routed through fallback delivery.
- Fix: `evolve_core_config_patch` now treats `messages/channels.yml/.yaml` as a typed proposal boundary. It normalizes kind/url/topic/route aliases, converts secrets to vault refs, infers generic webhook kind from URL refs, and drops non-consumed env/default/delivery/readiness/global-policy draft fields from the after file.
- Verification: proposal/gateway/message tests passed (`20 passed, 4 deselected`), harness/no-hardcoding proposal set passed (`39 passed`), E2E TypeScript compile passed, and real MiniMax/yolo/context-full/no-mock `J3,J4` passed with canonical after files and no live workspace mutation.

## 2026-06-07 Provider Peak-Busy Retry Follow-up
- Real MiniMax `J6` exposed a provider reliability boundary, not a prompt-routing or product-behavior failure: HTTP `529` peak-busy responses were not classified as transient by either the adapter retry status set or the loop retry hint set.
- Fix: `529` is now a retryable provider overload status, and loop-level transient detection recognizes peak-busy wording such as `temporarily busy`, `server busy`, `服务器短暂繁忙`, and `稍后重试`.
- Why this is not hardcoding: the change is keyed to provider error semantics, not case IDs, user prompt wording, tool route selection, or `_STRATEGY_INTENT_MARKERS`.
- Verification: failing-first loop regression reproduced the old immediate `LLMError`; after the fix, transport retry, loop retry, transient/safety subset, py_compile, and no-runtime-route-hardcoding all passed. Real `J1,J5,J6` rerun also passed on MiniMax/yolo/context-full/no-mock.

## 2026-06-07 Gateway Diagnose Tool Evidence Follow-up
- Best-practice gap: a gateway diagnostic request could pass a weak text assertion without using the actual gateway diagnostic surface; this hid poor UX where the model scanned proposals/skills and timed out into compact evidence snippets.
- Fix: Telegram gateway diagnosis now has a shared helper under `nerya.messaging`, the API route reuses it, and the agent has a read-only native `gateway_diagnose` tool with no send/mutation side effects. The CSV contract requires that tool evidence for `J6`.
- Why this is not hardcoding: the repair adds a reusable operator capability and machine-verifiable evidence contract; it does not route by case id, prompt language, regex intent marker, or forced mock behavior.
- Verification: local gateway/extract/final-synthesis/no-hardcoding gates passed; real MiniMax/yolo/context-full/no-mock `J6` passed with `tool_names=["gateway_diagnose"]`; final `J1,J5,J6` cluster passed with setup proving MiniMax `MiniMax-M3`, yolo permission, vault key ref, and no mock allowance.

## 2026-06-07 Source Fallback Confirmation Boundary Follow-up
- Best-practice gap: when public web search/fetch discovery produced no documents because search engines lacked API keys or hit anti-bot blockers, the model could end with "confirm browser/web_fetch fallback" even though `web_fetch` is a safe read-only tool and yolo mode should not preemptively ask for routine confirmation.
- Fix: the agent loop now records structured `web_search_fetch` failures with zero source documents and, before accepting final text, adds a one-shot source fallback retry requiring `web_fetch` when the tool is available and has not yet been attempted. The retry asks for one or two concrete public URLs already present in the transcript or directly implied by the named source, with Jina/browser fallback enabled.
- Why this is not hardcoding: the trigger is based on tool-result shape (`ok=false` / search failed / zero documents), completed tool state, and provider tool availability. It does not inspect case ids, prompt language, ticker/source keywords, or `_STRATEGY_INTENT_MARKERS`.
- Verification: failing-path regression `test_failed_search_fetch_gets_web_fetch_fallback_before_confirmation` passed, the source/final-synthesis subset passed (`14 passed, 176 deselected`), `py_compile` passed, `tests/test_no_runtime_route_hardcoding.py` passed, and forbidden scan found no route/case-marker additions.
- Runtime evidence: real MiniMax/yolo/context-full/no-mock `B3,B5,B7,B11` passed together (`4 passed`, 6.0m). After runtime restart, focused `B11` passed (`1 passed`, 2.0m); its latest reply uses RSS fallback timestamps/links and no longer asks the operator to confirm browser/source fallback.

## 2026-06-07 Parent-Budget-Aware Team Execution Follow-up
- Best-practice gap: a synchronous multi-agent tool can be correct internally and still fail the user experience if it consumes the full parent turn/UI budget. E5 exposed this: the TeamStore run completed with all roles succeeded, but Playwright timed out because parent synthesis had no remaining wall-clock reserve.
- Fix: outer turn deadline metadata is now propagated into native `ToolCall.metadata`, and `team_run` enforces that parent budget as a hard cap over model-supplied or auto-scaled team timeouts. The summary records both uncapped and capped timeout evidence.
- Fix: successful `team_run` results near the wall-clock boundary now use compact team-only final synthesis with no tool schemas and no provider thinking. If that compact synthesis fails, the loop can still fall back to deterministic structured team report text.
- Why this is not hardcoding: the trigger is based on parent deadline metadata, synchronous tool completion state, pending-required-action state, and TeamStore/tool result shape. It does not inspect case ids, ticker names, prompt language, or route markers.
- Verification: new regressions for parent timeout cap, ToolCall deadline metadata, and low-budget team final synthesis passed. Existing related team and wall-time subsets stayed green, no-runtime-route-hardcoding passed, and touched-file `py_compile` passed.

## 2026-06-07 Deep Team Timeout Floor Follow-up
- Best-practice gap: the first focused E5 rerun stopped the UI timeout but still returned a degraded AgentTeam report because a 7-role `market_analysis_team` carried model-authored `timeout_s=240`. A model tool argument is not the same as an operator/planner time budget and should not shrink a deep multi-wave team below the structural timeout floor.
- Fix: `team_run` now distinguishes model-authored execution controls (`timeout_s`, `max_wall_seconds`) from separate operator/planner budget fields (`deadline`, `timeout`, `time_budget`, `time_budget_s`). Without a separate budget field, short tool timeouts are raised to the auto team floor; with a real budget field, the explicit timeout still wins. Parent wall-budget caps still apply after this floor calculation.
- Why this is not hardcoding: the decision is based on team structure, tool argument semantics, shared payload budget fields, and parent metadata. It does not inspect case ids, prompt language, ticker names, route manifests, or `_STRATEGY_INTENT_MARKERS`.
- Verification: the failing-first deep-team floor regression now passes alongside the explicit-budget regression (`2 passed`); full `tests/test_team_streaming_events.py` passed (`39 passed`), the loop team/wall-time/final-synthesis subset passed (`39 passed, 162 deselected`), `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`), and touched-file `py_compile` passed.
- Remaining gate: restart the isolated MiniMax/yolo/context-full/no-mock runtime/dashboard and rerun focused E5; inspect TeamStore to confirm the team no longer degrades because of the model-authored 240s timeout.

## 2026-06-07 Team Parallelism Cap Follow-up
- Best-practice gap: bounded delegation was still partially model-controlled. A model could pass `max_parallel` equal to the full role count and overload one provider with simultaneous subagent requests, even when the selected `TeamTemplate` declared a safer lower parallelism.
- Fix: native `team_run` now computes effective workers from requested parallelism, runtime config, and `TeamTemplate.max_parallel`, then uses that effective worker count for both execution and timeout-floor wave calculation. This keeps delegation bounded by runtime/template policy rather than prompt-emitted numbers.
- Fix: stock-research subagents default to a 360s wall-time budget so tool-heavy analyst roles can synthesize after evidence collection instead of returning `tool_observation_fallback` at the 240s boundary.
- Why this is not hardcoding: the behavior is driven by team template metadata, runtime config, role count, and subagent class, not by case ids, ticker names, prompt language, route regex, or `_STRATEGY_INTENT_MARKERS`.
- Verification: focused red-green tests for parallel cap and stock-research wall-time passed, full team/compaction tests passed (`43 passed`), no-runtime-route-hardcoding passed (`5 passed`), and touched-file `py_compile` passed.
- Remaining gate: focused real MiniMax E5 rerun, followed by TeamStore/reply quality audit.

## 2026-06-07 Team Output Quality Boundary Follow-up
- Best-practice gap: a synchronous team can look "successful" at the tool boundary while individual members only produced partial tool-observation fallbacks. That violates the verifier boundary because the parent synthesis then treats missing role conclusions as completed evidence.
- Fix: native `team_run` now classifies `partial=true` and `quality=tool_observation_fallback` as member failures, same as explicit `degraded=true`. The aggregate status, TeamStore metrics, and final `next_action` now reflect the missing analysis instead of hiding it in `roles_succeeded`.
- Fix: curated deep teams keep a 600s structural floor for real research/template role sets even when they fit in one worker wave, and equity-research roles such as valuation, SEC, and investor-perspective analysts inherit the longer stock-research wall-time class.
- Fix: duplicate successful subagent tool calls are now a loop-recovery event, not an immediate close. The child runtime suppresses the repeated call, appends a bounded observation containing the duplicate reason, and gives the model one final pass to synthesize from existing observations.
- Why this is not hardcoding: these decisions are based on team template semantics, registered role classes, output quality fields, and repeated tool-call signatures. They do not inspect case ids, ticker names, prompt language, or natural-language intent markers.
- Verification: failing-first tests for deep single-wave floor, partial fallback aggregation, and duplicate-tool recovery passed; full related team/compaction suite passed (`45 passed`), no-runtime-route-hardcoding passed (`5 passed`), and touched-file `py_compile` passed.
- Remaining gate: focused real MiniMax E5 rerun with TeamStore/reply quality inspection before continuing broader old weak rows.

## 2026-06-08 Provider-Wire Observability Boundary
- Best-practice gap: canonical LLM context records were useful but still one layer too high for provider-protocol debugging. For OpenAI-compatible MiniMax, the adapter mutates the final HTTP payload after the canonical Agent request is logged, so failures could not be fully diagnosed from `llm_context_full.jsonl` alone.
- Reference mapping: Codex keeps replayable raw payload/event facts separate from UI/model summaries; Claude Code keeps the exact post-compaction API message set for bug reports. Nerya's equivalent needs both the canonical Agent request and the final provider-wire payload under one join key.
- Fix: provider transports now emit scoped wire events and `LLMGateway` writes `wire_request`, `wire_response`, and `wire_error` rows to the existing context-full journal. They share `call_id` with canonical rows and carry safe correlation fields for main Agent loop, subagents, native LLM tools, session title, and team final synthesis.
- Safety boundary: context-full remains opt-in. Records are redacted before disk, query-string secrets are scrubbed, and tracing failures are non-fatal. The log is for operator/debug analysis and must not be injected raw into prompts or user-facing fallback text.
- How to use it: during real MiniMax/yolo/no-mock E2E, inspect `<workspace>/dev_logs/llm_context_full.jsonl` grouped by `call_id`. Compare `phase=request` against `phase=wire_request` to identify adapter transformations such as MiniMax `thinking`, `max_completion_tokens`, tool-schema conversion, and final transcript shape.
- Why this is not hardcoding: the trigger is the context-full logging mode and the provider HTTP boundary, not prompt language, CSV case id, ticker symbol, route regex, or model-specific success fallback.
- Verification gate before runtime rerun: context-full wire tests, context metadata/native LLM tests, transport tests, no-runtime-route-hardcoding, touched-file `py_compile`, E2E TypeScript compile, and forbidden marker scans.

## 2026-06-08 Role Profile and Evidence-Contract Boundary
- Best-practice gap: a team member can have a plausible model-authored role name and final JSON while lacking the canonical role prompt, callable data surface, or required machine evidence. That lets `done=true` override the verifier boundary and turns incomplete research into a clean TeamStore success.
- Reference mapping: Codex/Claude-style systems keep identity, execution state, raw request logs, and reduced user-facing evidence as separate typed surfaces. Nerya now mirrors that split for teams: requested role identity is persisted, while canonical role profile drives default prompt/skills/budget/evidence policy.
- Generic fix: `SubAgentSpec` carries `canonical_name`; `StrategySubAgentRegistry` resolves missing/default roles through profile metadata; `SubAgentRuntime` uses the profile for stock-research budgets/prefetch/evidence contracts while keeping `spec.name` for caller, events, and TeamStore.
- Generic fix: `team_run` role argument normalization consumes structured provider containers (`roles`, `item/items`, `_raw/raw`, `role_payloads`) without reading prompt text, case IDs, tickers, or output language.
- Generic fix: `evidence_contract` converts missing required public-company research evidence into `partial/degraded` output with `missing_evidence` and `insufficient_research_evidence`; team aggregation persists this as `completed_with_failures` instead of a clean success.
- Observability fix: context-full and provider-wire rows now include `team_run_id` as a first-class correlation key where available, and normal agent-loop calls expose `context_scope=agent_loop`.
- Why this is not hardcoding: profile aliases are role taxonomy, not prompt routing. No CSV case, ticker, natural-language intent regex, output-language branch, or mock provider fallback was added.
- Verification: role/profile/provider-wrapper/evidence/context tests passed; team streaming + tool compaction + no-hardcoding passed; LLM context/native metadata/transport/model override passed; touched-file `py_compile` passed; forbidden marker scan had no runtime hits.
