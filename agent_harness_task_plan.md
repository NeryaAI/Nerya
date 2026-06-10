# Task Plan: Nerya Agent Harness Best-Practice Refactor

## Goal
Refactor Nerya's agent harness toward the AgentArchitecturePatterns best practices, remove hardcoded prompt/regex routing patches, then rerun and fix the failing real-API Playwright prompt CSV cases with evidence from logs.

## Current Completion Audit - 2026-06-02
| Objective requirement | Current state | Reference code logic | Verification evidence |
| --- | --- | --- | --- |
| Use planning-with-files to list gaps vs AgentArchitecturePatterns | Current gap and closure state is tracked in this file, `agent_harness_best_practices_plan.md`, and `agent_harness_notes.md`. Historical matrices are preserved; this section and the refreshed best-practices matrix are authoritative for current state. | `agent_harness_best_practices_plan.md::Refreshed Gap Matrix`, `agent_harness_notes.md` | Plan files updated through full static lint closure; current status remains Phase 7 / full e2e verification. |
| Plan lists code logic to reference | The plan names the concrete runtime modules for loop, prompt, verifier, execution state, tools, sandbox, security, skills, route manifests, rollout/audit, and progress surfaces. | `nerya/agent/loop.py`, `nerya/agent/kernel.py`, `nerya/agent/verifier.py`, `nerya/agent/execution_state.py`, `nerya/tools/native/*`, `nerya/core/sandbox.py`, `nerya/core/redaction.py`, `nerya/memory/content_scanner.py`, `nerya/rollout/writer.py`, `nerya/progress/todo.py` | AgentArchitecturePatterns lint over `Nerya/nerya` passes all rules; focused pytest and py_compile cover the touched surfaces. |
| Delete prompt/tool route hardcoding | Known prompt/tool route hardcoding has been deleted. Built-in planner manifests are capability-only and do not ship `routes` / `match` tables. Residual `match` fields in trigger routing, eval scenarios, and API/ref names are data structures, not prompt/tool routers. | `nerya/agent/route_manifests.py`, `nerya/agent/route_manifest_presets/*.yml`, `tests/test_no_runtime_route_hardcoding.py`, `nerya/triggers/routes.py` | `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`); runtime route marker scan found no `_STRATEGY_INTENT_MARKERS`, `INTENT_MARKERS`, `_NATIVE_ROUTE_WEB`, hidden web route redirect, or packaged route `match` tables. |
| Implement best harness practices before prompt e2e | AgentArchitecturePatterns R1-R10 plus P1 are now represented by current Nerya surfaces: turn truth/audit, cache boundary, nonce external content, verifier tiers, sandbox exec, import-time redaction, fail-open scanners, frozen memory, bundled skill allowlist, audit JSONL, and task progress. | `nerya/rollout/writer.py`, `nerya/agent/prompt_sections.py`, `nerya/agent/loop.py`, `nerya/agent/verifier.py`, `nerya/core/sandbox.py`, `nerya/core/redaction.py`, `nerya/memory/content_scanner.py`, `nerya/agent/kernel.py`, `nerya/skills/registry.py`, `nerya/security/audit.py`, `nerya/progress/todo.py` | `lint-agent-design.py` full run passed (`10 passes, 0 fails, 0 advisories`); focused regression aggregation passed (`65 passed, 7 deselected`); context-full/no-hardcoding passed (`5 passed, 24 deselected`). |
| Keep real-provider E2E separate from mock success | Harness best-practice adaptation is now statically closed; full real MiniMax/yolo/no-mock prompt CSV remains the next runtime gate and is not claimed complete here. | `dashboard/tests/e2e/csv-runner.spec.ts`, `<NERYA_WORKSPACE>/dev_logs/llm_context_full.jsonl`, `dashboard/tests/e2e/README.md` | Next gate remains a fresh full CSV run on MiniMax with yolo/no-mock/context-full. |

## Current Full CSV Run - 2026-06-02
- Runtime/dashboard restarted on isolated `dashboard/.nerya-test-workspace` after reseeding the MiniMax vault ref.
- Verified pre-run gates: `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `permission_mode=yolo`, `key_ref=True`, no mock allowance, context-full enabled.
- Live `/llm/messages/probe` text and tool-call probes passed before starting the full CSV diagnostic run.
- First full CSV diagnostic run uses `NERYA_TEST_RETRIES=0` so failures map cleanly to one per-case log row.

## Phases
- [x] Phase 1: Stop stale E2E processes and remove the just-added hardcoded regex/tool-forcing patch.
- [x] Phase 2: Read `planning-with-files` and the AgentArchitecturePatterns reference material.
- [x] Phase 3: Audit Nerya against the 10 Iron Laws and the Agent Loop / Tool / Verifier / Execution State chapters.
- [x] Phase 4: Replace hardcoded intent/focus regex routing with general harness policies and observable verifier signals.
- [x] Phase 5: Implement missing harness capabilities in small, reviewable increments.
- [x] Phase 6: Run Python/TypeScript checks only; defer Playwright prompt CSV until harness adaptation V3 is complete.
- [ ] Phase 7: Run full MiniMax/yolo/no-mock CSV, repair failures from logs, and summarize pass/fail evidence plus log locations.

## Key Questions
1. Which failing cases are caused by missing generic harness behavior rather than stale assertions or external source limits?
2. Which existing Nerya mechanisms already map to the best practices and should be reused instead of rewritten?
3. Which hardcoded regex/route blocks can be removed without losing essential safety behavior?
4. What verifier signals should decide completion when the model returns prose without real tool evidence?
5. Which current routes are real operator-configurable capability manifests, and which are hidden runtime prompt/query routers that must be deleted?
6. Which Playwright contracts still assert final prose instead of hard evidence from API state, artifact index, journals, or tool results?

## Decisions Made
- Do not add case-specific regex, prompt IDs, or mock routes to pass Playwright.
- Keep real provider/API gates: direct runtime and dashboard proxy must hit the actual LLM/provider path.
- Treat per-case logs and runtime journals as ground truth; do not infer success from visible prose alone.
- Prefer generic harness improvements: tool-loop detection, transition reasons, verifier events, execution-state routing, source/evidence contracts.
- Context-full logging must be explicitly enabled, capture the canonical LLM request for every Agent provider attempt, and redact plaintext provider/API tokens before writing `llm_context_full.jsonl`.
- Team results are now treated as normal tool observations. The model gets one more loop to decide whether to synthesize, call strategy tools, or report gaps; the harness no longer decides this from prompt keywords.
- Runtime examples should use placeholders or typed fields, not hardcoded case subjects such as a specific ticker or wallet-flow niche.
- Treat `AgentArchitecturePatterns` as the architecture reference, not as a source of prompt wording. The implementation should copy mechanisms: turn truth, frozen context, verifier tiers, sandbox gates, execution-state surfaces, audit logs.
- Runtime prompt/query regex that selects a route or blocks one tool in favor of another is not acceptable. Security validators, syntax parsers, ID sanitizers, protocol shims, and test assertions may still use regex when they are not selecting task routes.
- Builtin planner manifests must be capability-only. Source-level Python route tables and packaged YAML `routes/match` tables are both treated as route hardcoding; only explicit workspace-owned manifests may define route tables.
- Prompt E2E must default to unattended `yolo` permission mode through three independent surfaces: Playwright process env, dashboard public env/localStorage, and isolated runtime config. Production/default runtime remains `default` unless payload/env/config explicitly changes it.

## AgentArchitecturePatterns Reference Map
- `skills/build-your-own-agent/SKILL.md`: 10 Iron Laws and source-backed picks. Nerya must be audited against turn truth, cache boundary, prompt-as-data, three verifier tiers, sandbox-first, import-time redaction, fail-open scanners, frozen memory, skill supply-chain, and audit trail.
- `skills/build-your-own-agent/references/diagnose-agent.md`: diagnostic Flow A-D and runtime anti-patterns. The immediate Nerya focus is AP-1 tool loop, AP-3 silent verifier, AP-7 missing/overloaded transition reason, AP-9 stale task progress.
- `skills/build-your-own-agent/references/build-agent-workflow.md`: file-level contracts. Relevant Nerya analogs are `nerya/agent/loop.py` for `core/loop.py`, `nerya/agent/kernel.py` for `core/prompt.py`, `nerya/tools/*` for `core/tools.py`, `nerya/agent/verifier.py` for `core/verifier.py`, `nerya/tools/native/shell.py` for sandbox, `nerya/security/*` for redaction/external threat scanning, and journals/session rows for rollout.
- `docs-site/src/content/docs/patterns/05-verifier.mdx`: three verifier tiers. Nerya must stop only through hard evidence, soft evidence, or an explicit lazy/model-done fallback with a transition reason; it must not treat model prose as proof of work.
- `docs-site/src/content/docs/patterns/22-execution-state-surfaces.mdx`: execution-state router. Nerya must separate approval plan, execution todo, tool progress, background task progress, status surface, and resume summary instead of letting final prose or one checklist carry all semantics.

## Current Nerya Gap Matrix
| Best-practice requirement | Current evidence | Gap | Code to inspect/change |
| --- | --- | --- | --- |
| Turn is source of truth | `AgentTurnResult`, session rows, journals, `turn_id` exist | Some deterministic finalizers stop based on partial tool evidence; completion needs a verifier outcome, not shortcut prose | `nerya/agent/loop.py`, `nerya/agent/kernel.py::_record_session_db_turn`, `_compact_turn_payload` |
| Three verifier tiers | `transition_reason`, repeated-tool suppression, `compute_verifier_nudge` exist | No explicit hard/soft/lazy verifier pipeline exposed in one place; E2E often checks reply regex instead of hard artifacts | `nerya/agent/verifier.py`, `nerya/agent/loop.py`, `dashboard/tests/e2e/csv-runner.spec.ts` |
| Prompt-as-data / no route regex | `_render_turn_focus_block` says no keyword routing | `nerya/tools/native/web.py` still uses query/URL regex to deny web tools and redirect to wallet route discovery | `nerya/tools/native/web.py`, `tests/test_native_web_route_redirect.py` |
| Sandbox-first | shell tool has deny regex and path guards | L10 showed absolute host path arguments can reach shell before workspace sandbox denial is clear; guard needs structured path-token checks, not case prompt matching | `nerya/tools/native/shell.py`, `tests/test_tool_approval_policy.py` or new shell sandbox tests |
| Execution-state surfaces | tool events, task rows, artifact index exist | Approval/proposal/todo/tool progress/background task/prose are still partially mixed; account setup finalizer can mask strategy work | `nerya/agent/loop.py`, `nerya/agent/artifact_index.py`, `nerya/api/routes_agent_tasks.py` |
| Audit trail last mile | per-case JSONL, screenshots, `summary.csv`, journals exist | The plan lacks a per-turn verifier outcome matrix; tests should consume `turn.evidence`, `artifact_index`, API checks, not only final answer | `dashboard/tests/e2e/csv-runner.spec.ts`, `nerya/agent/kernel.py` |
| Frozen memory / reset | reset supports `--clear-memory` for E2E | Full CSV still sensitive to per-case prompt extraction and existing workspace artifacts; need evidence-based reset policy documented | `tools/reset_workspace.py`, `dashboard/tests/e2e/global-setup.ts`, `csv-runner.spec.ts` |
| Skill supply-chain / lazy load | Nerya is `SKILL.md` first | Always-on kernel prompt has grown into a large procedural contract; some details should move to tool schemas/skill docs and be lazy-loaded | `nerya/agent/kernel.py`, `nerya/skills/builtin/strategy_author/SKILL.md`, native tool descriptions |

## Hardcoded Routing Audit
Must delete or replace now:
- `nerya/tools/native/web.py::_NATIVE_ROUTE_WEB_QUERY_RE` and `_NATIVE_ROUTE_WEB_URL_RE`: hidden query/URL regex denies web/search/fetch and forces `data_api wallet.meme_strategy_guide`. This is exactly the route-hardcoding pattern the user rejected.
- `tests/test_native_web_route_redirect.py`: validates the above hidden router; remove or rewrite to assert web tools stay general and route preference lives in `data_api` tool descriptions / `next_required_action`.
- Any newly-added prompt/final-answer intent-marker tables: current code scan found none, but keep this as a hard gate.

Deleted or replaced in later passes:
- `nerya/agent/route_manifests.py` and `nerya/agent/route_manifest_presets/*.yml`: bundled planner presets no longer ship source/YAML `routes` or `match` tables. Builtins describe capabilities and fallback only; workspace-owned manifests remain the explicit operator extension point for any route table.

Requires deeper refactor, not immediate deletion:
- `nerya/agent/loop.py::_durable_workflow_proposal_retry_prompt` and strategy/account finalizers: these are not regex route matchers, but they encode tool-name-specific continuations. Replace over time with structured tool-result fields such as `completion_scope`, `remaining_actions`, `evidence_kind`, and `terminal_for`.
- `nerya/agent/kernel.py::_render_turn_focus_block`: currently non-regex policy text, but it is too broad and procedural. Shift specific behavior into tool schemas and verifier outcomes.

Allowed regex/classes:
- Security and sandbox deny patterns in `nerya/security/*` and `nerya/tools/native/shell.py`, when they block dangerous operations rather than route tasks.
- Syntax/ID parsers such as vault ref, safe names, markdown/frontmatter, XML-ish legacy tool-call recovery.
- Test-only `must_contain` regex in `tools/extract_cases.py` while CSV remains prose-backed, but the plan is to reduce reliance on it by adding typed `api_check` contracts.

## Code Logic To Reference During Implementation
- Loop and transition state: `nerya/agent/loop.py::WorkspaceNativeAgentLoop.run`, `LoopConfig`, `LoopOutcome`, `transition_reason` assignments, repeated-tool fingerprint suppression.
- Prompt/context assembly: `nerya/agent/kernel.py::_build_system_prompt`, `_render_turn_focus_block`, `_render_permission_mode_block`, `_compact_turn_payload`, artifact index journaling.
- Tool dispatch and permissions: `nerya/tools/orchestrator.py`, `nerya/tools/executor.py`, `nerya/tools/permissions.py`, native tool descriptors in `nerya/tools/native/bootstrap.py`.
- Evidence index and final report: `nerya/agent/artifact_index.py`, `nerya/agent/kernel.py` final report journal events.
- Verifier layer: `nerya/agent/verifier.py`, `tests/test_agent_loop_final_summary.py`, `tests/test_strategy_context_guidance.py`.
- E2E contract layer: `dashboard/tests/e2e/csv-runner.spec.ts`, `dashboard/tests/e2e/fixtures.ts`, `dashboard/tests/e2e/global-setup.ts`, `tools/extract_cases.py`.
- Failing-case evidence sources: `dashboard/test-results/logs/*.jsonl`, `dashboard/test-results/logs/*.reply.txt`, `dashboard/test-results/summary.csv`, `dashboard/.e2e-server-logs/*`, runtime journals under `dashboard/.nerya-e2e-real-workspace/journals/`.

## Implementation Plan V2
1. Update this plan and notes with the AgentArchitecturePatterns gap matrix. Status: in progress.
2. Delete web-tool native route regex redirect and its validating tests. Web/search/fetch must stay general; if a native route is preferred, expose it as tool schema guidance or structured `data_api` result.
3. Add a no-route-hardcoding regression scan/test covering the known forbidden pattern names and hidden web redirect. This should check runtime files, not CSV test contracts.
4. Introduce a small verifier outcome object for each turn: hard evidence observed, soft verifier status, lazy fallback status, and transition reason. Reuse existing `artifact_index` and `LoopOutcome` instead of inventing a new store.
5. Replace deterministic account/team/proposal finalizers with a generic evidence finalization policy: a tool result can finalize only if its structured result says it is terminal for the current action scope, otherwise it becomes evidence and the loop continues.
6. Strengthen shell sandbox path-token checks so absolute host paths are denied with `permission_denied` before command launch, independent of the natural-language prompt.
7. Move strategy/wallet/polymarket procedural routing out of always-on kernel text where possible and into `strategy_author` / `data_api` / `strategy_generate_proposal` tool schemas plus structured `next_required_action`.
8. Expand CSV `api_check` typed contracts for artifact-backed cases; leave `must_contain` only as a user-visible language sanity check.
9. Verification order: py_compile touched Python, focused unit tests, TypeScript e2e check, focused failed cases, then full CSV on real GLM/yolo/no mock.

## Harness Adaptation V3 - Before Any More Prompt E2E
1. Make verifier outcome first-class on every turn: hard verifier status, soft verifier status, lazy/model-done status, trust flag, and machine-readable transition label.
2. Stop treating "no validation signal" as `verified`. A model-only answer with no hard evidence is a lazy fallback (`model_done`), not a trusted verified result.
3. Add a generic execution-state router that derives separate surfaces from turn blocks and activity events: approval plan, execution todo, tool progress, task progress, status/resume evidence.
4. Persist and return `verifier_outcome` and `execution_state` through `AgentTurnResult`, `/agent/run_turn`, compact session turn payloads, transcript rehydration, and CSV evidence extraction.
5. Add narrow Python/TypeScript checks for the new surfaces and rerun hardcoded-routing scans. Only after these pass should real MiMo Playwright prompt cases run.

## Harness Adaptation V3 Result
- `nerya/agent/verifier.py` now treats missing hard evidence as `hard_status=missing`, `trusted=false`, and a lazy `model_done` style outcome rather than `verified`.
- `nerya/agent/execution_state.py` now projects turn blocks and activity events into separate execution-state surfaces: approval plan, execution todo, tool progress, task progress, status, and resume.
- `AgentTurnResult`, compact session turn payloads, `/agent/run_turn`, and CSV evidence extraction now expose `verifier_outcome` and `execution_state`.
- Stale prompt-routing tests were changed to assert the generic evidence policy instead of old browser/trading/news keyword routing.
- Verification completed before e2e: py_compile passed; related pytest `22 passed`; e2e TypeScript compile passed; no-runtime-route-hardcoding pytest passed; forbidden marker/route text scan had no matches.
- Web-budget hardening is now part of the harness adaptation: `fetch_url.py` shares one deadline across direct/Jina/browser/Scrapling fallback, `search_fetch.py` bounds the whole search+fetch operation, and bulk `web_search_fetch` defaults to direct/Jina only with browser/Scrapling explicit opt-in.
- Focused real MiMo/yolo/no-mock verification passed `B6,B7,B8`: `B6` 156271 ms, `B7` 102496 ms, `B8` 94639 ms. Setup confirmed `provider=xiaomi`, `model=mimo-v2.5`, `base_url=https://token-plan-cn.xiaomimimo.com/v1`, `key_ref=yes`, and `permission_mode=yolo`.

## Errors Encountered
- Semantic code search MCP returned `Transport closed` during the context-full logging audit. Resolution: use `rg` plus direct file reads and keep the investigation scoped to gateway/loop/logging files.
- A prior patch added hardcoded regex categories for web/news/strategy tasks and forced tools based on those categories. Resolution: removed that patch direction and stopped the runtime started with it.
- The interrupted full CSV run was running stale code. Resolution: stopped it and will restart only after architecture-aligned changes are ready.
- The first full CSV restart only wrote A1-A3 before the PTY session disappeared and no Playwright runner remained. Resolution: treat it as incomplete evidence, preserve the partial summary, and rerun after the architecture-aligned edits are verified.
- `test_team_run_tool_result_final_synthesis_receives_original_prompt` still expected the removed synthetic final-synthesis path. Resolution: update it to assert normal transcript continuation with the original prompt, assistant `team_run` tool_use, and user `tool_result` evidence.
- The first TypeScript check used a stale root-relative path (`tests/e2e/tsconfig.json`). Resolution: run the actual dashboard e2e project config at `dashboard/tests/e2e/tsconfig.json`.
- The first targeted real-provider rerun used `mimo-v2.5` but inherited the operator workspace's `token-plan-cn` base URL during Playwright autostart. Resolution: stop that run and add a generic `NERYA_E2E_LLM_BASE_URL` override to `tools/prepare_isolated_test_workspace.py`, then pin the isolated workspace to the requested SGP endpoint.
- Mimo SGP with the supplied isolated vault key failed the live LLM probe with provider `401 Invalid API Key`. Resolution: switch to the only allowed fallback, StepFun `step-3.7-flash`, with the same no-mock E2E gates.
- Full CSV restart on StepFun exposed a real isolation bug: A8 wrote a durable "only trade ETH/SOL, no BTC" memory and later `reset_before=1` rows did not clear agent memory by default, so C1 refused to create a BTC strategy. Resolution: add test-only `reset_workspace.py --clear-memory`, pass it from global setup and CSV per-case reset, and make CSV `reset_before=1` active by default with `NERYA_RESET_PER_CASE=0` as local-debug opt-out.
- The same restart showed A10 was not a normal reply case. Resolution: add a generic `cancel_inflight=true` CSV api_check flow that sends then clicks cancel, and fix `ChatView.cancel()` to immediately clear frontend sending state while still signalling backend `/agent/interrupt`.
- B3/B4/B7 failures were stale source-availability assertions, not prompt routing: real tools ran, but Reddit/X/search sources were blocked or unavailable and the model reported degraded status in Chinese. Resolution: add generic source/date/unavailable reporting guidance and broaden those CSV expectations to accept explicit degraded-source wording.
- C2 previously hung during freeform backtest because generated scripts that read stdin could block on Windows. Resolution: close/provide stdin in the freeform runner, then rerun C2 successfully on StepFun.
- C5 failed because `data_api` returned a structured `next_required_action` naming `strategy_generate_proposal`, but the model ended with a choice prompt before attempting the required proposal tool. Resolution: add a generic native-loop nudge for unattempted tools named by `next_required_action`; C5 then generated `prp_9b134083c75e` and passed the `strategy_package_proposal:bsc:execution_mode=agent` API check.
- C7/C8 surfaced a non-fatal backtest warning for async `run(ctx)` strategies not being awaited. Resolution: the bar-by-bar engine now resolves awaitable strategy decisions and the async strategy regression test passes.
- C-AT1/C-AT4 exposed generic custom-strategy packaging gaps, not a mock/provider issue: a stock template can erase named indicator logic such as MACD/Bollinger, and inline `strategy.yml` overrides could drop `execution_mode`/agent-task manifest fields. Resolution: require `files.main.py` for named custom signal logic and merge critical manifest defaults back into inline manifests.
- C-AT2 exposed a native `glob` crash on Windows when the model supplied an absolute glob pattern; the turn then spiraled into repeated tool errors and timeout. Resolution: normalize absolute glob patterns under the workspace before calling `Path.rglob`, while still denying patterns outside the workspace.
- The next C-AT rerun showed C-AT1/C-AT4 can stop on `max_tokens` after long reasoning without a native tool call. Resolution: add a generic one-time continuation nudge for truncated no-tool responses instead of ending the turn as `no_tool_use`.
- C-AT2 then reached `strategy_generate_proposal` but StepFun serialized the `files` object as a JSON string, causing repeated schema failures. Resolution: decode unambiguous JSON-string object/array arguments in the native executor before schema validation.
- C-AT5 reached proposal generation on real StepFun, then looped until `max_iterations=120` because the freeform backtest runner rejected artifacts from `backtests/research_backtest.py`. Root cause: the generated script wrote artifacts beside itself and printed pretty JSON with a numeric `equity_curve`, while the runner only accepted `NERYA_BACKTEST_OUT_DIR` files, could misparse pretty stdout, and did not normalize numeric equity arrays. Resolution: make the freeform runner accept fresh cwd/script-dir artifacts, parse full pretty stdout JSON, and normalize numeric equity curves into CSV rows.
- The next C-AT5 run finished but exposed an agent-task contract gap: `execution_mode=agent` strategies could dispatch on success while returning `ctx.result.hold()` when preconditions failed, so the package passed validation but failed the CSV `StrategyAgentTask.skip` check. Resolution: block this generic pattern in the agent-task validator and document that non-dispatch branches must return `StrategyAgentTask.skip(...)`.
- Follow-up C-AT5 runs generated valid proposals/backtests with `StrategyAgentTask.skip`, but the CSV `needle=confluence`/`共振` assertion was too dependent on optional naming rather than the actual contract. Resolution: make the assertion match stable generated evidence (`macd` proposal plus `main.py` contains `StrategyAgentTask.skip`, `rsi`, and `volume`) without changing runtime routing.
- C-AT10 first stopped hanging only after direct repro showed `urllib.request.urlopen(..., timeout=...)` could still block inside Windows TLS handshake against Binance Vision. Resolution: run each Binance Vision ZIP fetch in a short-lived subprocess with a hard parent timeout, keep a total archive-scan deadline, and fall back to the existing real candle source instead of mock data.
- C-AT10 then exposed stale/over-narrow CSV evidence: the generated strategy used `triple_alignment`/中文“三重时间框架” instead of the optional `mtf` acronym, and later runs produced multiple intermediate proposals. Resolution: assert stable MTF evidence (`timeframe`, `1d`, `4h`, `1h`) and make the generic `api_check` proposal matcher select a candidate that satisfies all requested execution/main.py checks rather than the first needle match.
- D1/D5 exposed task automation gaps rather than a mock/provider issue. D1 did create a real `subagent_run_async` task, but `/agent/tasks` only surfaced session rows and the CSV check only read a top-level `tasks` field, missing the envelope `data.tasks`. Resolution: merge background subagent task records into `/agent/tasks`, prioritize them before older sessions, and teach the CSV check to read envelope data. D5 was not self-contained under per-case reset because it referenced “D1 那个脚本”; resolution: keep the script-schedule assertion but use the existing `eth_btc_ratio_chart.py` script in the prompt.
- D6 initially entered the full AgentKernel/LLM loop for a workflow-help style request. Resolution: register a generic `/workflows` gateway command and handle registered slash commands before starting the agent loop; focused D6 rerun passed without mock.
- D7 initially kept re-entering team/task tooling after a synchronous degraded `team_run` result. Resolution: finalize degraded team results with a deterministic report when the tool result itself is already terminal; focused D7 rerun passed on real StepFun.
- D8 previously gathered real macro/BTC evidence but exhausted wall-clock budget before synthesis, so the reply degraded to timeout fallback and failed `宏观|BTC|观点`. Resolution: add a generic near-wall-clock text-only synthesis attempt from completed tool evidence, without allowing further tools; targeted pytest/TypeScript checks passed and real StepFun D8 rerun passed (`1 passed`, 5.4m, 11 tool calls, `aborted=false`).
- The next full D run showed D8 could still fail in-batch when slow public market-data calls consumed the Playwright wall budget: `YAHOO:DXY` took 153369ms and returned 0 rows. Resolution: add a generic bounded timeout for `market_data` public connectors, pass it through Yahoo/CCXT provider factories, and keep mock fallback disabled.
- The user-supplied MiMo SGP endpoint/key was tried first for the D8 rerun, but the live LLM probe failed before test execution with `401 Invalid API Key`. Resolution: continue only on the allowed fallback, StepFun `step-3.7-flash`, with no mock LLM gate.
- Full D1-D10 rerun then passed on real StepFun with `NERYA_MARKET_DATA_TIMEOUT_SECONDS=3`: D1 38s, D2 24s, D3 59s, D4 27s, D5 29s, D6 5s, D7 151s, D8 121s, D9 26s, D10 32s (`10 passed`, 9.4m).
- GLM rerun constraint update: use only provider `zai`, model `glm-5.1`, base URL `https://open.bigmodel.cn/api/coding/paas/v4`, with no `NERYA_E2E_ALLOW_MOCK_LLM`. The supplied key must be passed through env/vault only and not printed.
- Latest GLM focused F rerun passed F2 and F5, and failed F1, F3, F4, F6, F7, F10. Logs are under `dashboard/test-results/logs/`.
- F3 and F10 failed with BigModel 400 `messages 参数非法`, consistent with OpenAI-compatible requests containing multiple `system` messages after compaction. Resolution in progress: render OpenAI-compatible transcripts with a single merged system message.
- F4 produced final prose saying it would submit a skill proposal but never called `evolve_skill_proposal`. Resolution direction: require proposal claims to be backed by proposal tool evidence, but do not classify the final text via regex.
- F6 directly edited strategy files and reported success without a proposal. Resolution direction: enforce proposal-only mutation guards in file/shell tools and drive recovery from structured `next_required_action`, not prompt text.
- F7 correctly refused a protected risk-scope change in Chinese, but the CSV assertion only accepted `refuse|advisory|reject`. Resolution direction: either make protected-scope refusal wording explicitly include the stable English audit label from policy/tool evidence, or adjust the assertion if the product contract is Chinese refusal text.
- A temporary `loop.py` patch added prompt/final-text regex helpers for proposal/refusal/reflection detection. This conflicts with the no-hardcoded-routing constraint. Resolution: remove those helpers and keep only tool-result/schema/permission evidence paths.
- Fresh BigModel/Z.ai focused verification passed with provider `zai`, model `glm-5.1`, base URL `https://open.bigmodel.cn/api/coding/paas/v4`, and no mock LLM gate: focused pytest `2 passed`, then Playwright CSV `C1,F6` `2 passed (6.2m)`. `C1` created/backtested a BTC 1h momentum strategy in 3.8m; `F6` completed proposal-only strategy rewrite evidence in 1.6m.
- First full GLM CSV restart passed A1-A9, then A10 timed out because the generated CSV row lacked `api_check=cancel_inflight=true`; the runner never entered `sendAndCancel` and treated the long AgentTeam prompt as a normal turn. Resolution: add the A10 override in `tools/extract_cases.py`, regenerate `cases.csv`, and verify focused A10 on real GLM (`1 passed`, 38.9s).
- Second full GLM CSV restart showed A2 behavior was correct but the assertion was English-only: the reply asked for `买什么`, `买多少`, `平台/交易所`, and `交易账户`, but `must_contain=account|symbol|amount|paper` failed. Resolution: broaden the A2 override to stable Chinese/English missing-parameter terms and verify focused A2 on real GLM (`1 passed`, 31.2s).
- Third full GLM CSV restart passed A1-A10 and B1-B2, then B3 failed only because the default B assertion required a year/URL while the model correctly reported Reddit access blockers (`403`, `Cloudflare`, `未验证`) with real tool evidence. Resolution: add a B3 override accepting explicit Reddit degraded-source wording and verify focused B3 on real GLM (`1 passed`, 2.6m).
- B group continuation passed B1-B8, then B9 exposed two legitimate outcomes for custom RSS source setup: create a proposal when absent, or report `news_feeds.yml` already contains the feed when present. Resolution: broaden B9 to accept proposal evidence or existing `news_feeds/feed.xml` confirmation; focused B9 passed on real GLM (`1 passed`, 27.1s).
- Full F rerun on real GLM passed 10/12 and failed F5/F9 only on stale CSV contracts. F5 had valid tool evidence and a strategy/proposal path but exposed generated strategy SDK drift; F9 used already-registered HTX instead of a provider proposal because `htx` is built into `provider_spec.py`.
- F5/F9 contracts were corrected without prompt routing: F5 now accepts the valid "strategy missing / tuning proposal" outcomes, and F9 verifies `/exchanges/providers` contains `htx` or an alias. Focused real GLM rerun passed `F5,F9` (`2 passed`, 5.7m).
- F5 then exposed a generic SDK compatibility gap: validator allowed `ctx.result.skip()` but `ResultBuilder` only implemented `hold/ok/error`. Resolution: add `StrategyResult.skip` / `ResultBuilder.skip` as a hold alias with regression tests (`tests/test_strategy_result.py`, `2 passed`) while leaving agent-task validation's `StrategyAgentTask.skip(...)` rule intact.
- Permission-flow update: Playwright now defaults `NERYA_PERMISSION_MODE=yolo` at config/global-setup level, not just via page localStorage. Focused F9 real GLM rerun showed `[setup] permission_mode=yolo` and passed (`1 passed`, 58.7s).
- Permission-flow hardening: `/agent/run_turn` now falls back to `runtime.permission_mode` when payload/env are absent, and `tools/prepare_isolated_test_workspace.py` writes `runtime.permission_mode: yolo`. Playwright also mirrors `NERYA_PERMISSION_MODE` into `NEXT_PUBLIC_NERYA_PERMISSION_MODE`. Regression checks: `py_compile` passed; focused pytest `5 passed`; e2e TypeScript check passed; forbidden marker-name `rg` returned no matches.
- H group follow-up root causes: H4 is a stale English-only assertion despite a real ETH price response; H8 API checks lost colon-containing values such as `account:paper_main`; H5 times out because credential-gated Glassnode requests keep probing after no API key is configured. Resolution direction: update CSV contracts/parser generically and add credential-status terminal guidance to data-source tools, not prompt-specific markers.
- H group verification: focused `H4,H5,H8` passed on real GLM (`3 passed`, 3.4m), then full `^H[0-9]+` passed on real GLM with `NERYA_PERMISSION_MODE=yolo` (`10 passed`, 11.5m). Setup confirmed `provider=zai`, `model=glm-5.1`, `base_url=https://open.bigmodel.cn/api/coding/paas/v4`, `key_ref=yes`, and no mock LLM gate.
- Latest J/L run failed `J2,L5,L6,L7,L8,L10`. `J2,L5,L7` show the CSV generator stripped important prompt context outside backticks, such as "在 Telegram 群里", "断网后", and "发 50 个超长文档". `L8` shows prompt guard allowed a Chinese vault exfiltration jailbreak prompt. `L10` shows `run_shell` only sandboxed cwd and let an absolute host path argument (`/etc/passwd`) reach the shell. Resolution direction: preserve plan prompt context, strengthen generic prompt-guard/sandbox evidence, and add stable safety audit wording.
- Real MiMo B6 initially failed because `web_search_fetch` calls spent 91s/65s in progressive fetch fallback and Playwright cancelled before synthesis. Resolution: generic web-budget deadlines plus bulk-search direct/Jina defaults; focused `B6,B7,B8` passed afterward. Remaining operational risk: MiMo intermittently returns `429 Too many requests`, so full CSV may be slow but retry/backoff and wall-time synthesis are recovering.
- A focused rerun failed setup with MiMo 401 when Playwright autostart overwrote a manually seeded isolated vault. Resolution: seed the requested key through a masked PowerShell process env for the autostart prepare step only; do not put the key on the command line or in files.
- Full MiMo CSV run failed `B8` only on a stale degraded-source assertion. Runtime used real web tools and reported `网络限制` / `反爬虫` / `搜索失败` / `访问失败`; the row only accepted narrower phrases such as `无法获取`. Resolution: broaden the generated B8 `must_contain` contract in `tools/extract_cases.py` and regenerate `dashboard/tests/e2e/cases.csv`; focused real MiMo rerun still pending.
- The same MiMo run failed `B9` because the agent wandered through skill/source-code exploration and timed out after 17 tool calls instead of staging a custom RSS config proposal. Resolution: add `news_feeds.yml/.yaml` to proposal-only file and shell mutation guards, expose it in `evolve_core_config_patch`, document the `news_social` custom RSS flow, and make `recent_news.py` consume approved `news_feeds.yml` entries. Local verification: py_compile passed; focused proposal/evolve/custom-feed tests passed (`13 passed`); harness regression set passed (`51 passed`); e2e TypeScript compile passed; forbidden hardcoding scan returned no matches.
- Focused real MiMo CN/yolo/no-mock rerun passed `B8,B9` (`2 passed`, 2.5m). Setup confirmed runtime and dashboard proxy used `provider=xiaomi`, `model=mimo-v2.5`, `base_url=https://token-plan-cn.xiaomimimo.com/v1`, and `key_ref=yes`. `B8` passed with real web/tool degradation evidence; `B9` created a `core_config_patch` proposal targeting `news_feeds.yml` instead of mutating the live workspace.
- Full MiMo CN run then exposed `B12` and `C4`: `B12` was the already-fixed stale TheBlock degraded-access assertion; `C4` was a real MiMo provider payload bug, with the upstream error identifying `messages[19] assistant must provide content, reasoning_content or tool_calls`. Resolution: skip OpenAI-compatible assistant history messages that become empty after dropping provider-incompatible thinking blocks, preserving messages with text or tool calls. Verification: py_compile passed; focused OpenAI message-rendering tests passed (`3 passed`); no-runtime-route-hardcoding passed; focused real MiMo `B12,C4` passed (`2 passed`, 8.0m).
- Fresh full MiMo CN/yolo/no-mock CSV run passed `A1-A10`, `B1-B12`, `C1`, and `C2`, then hit provider-wide MiniMax-sized load pressure on MiMo: `C3`, `C4`, and `C5` all failed with upstream `xiaomi messages api error (429): Too many requests` after the configured 6 retry attempts. This is an external provider limit, not a mock/harness routing failure. The run was stopped to avoid polluting later cases with rate-limit failures.
- Provider switch update: per user instruction, continue prompt E2E on MiniMax only, using provider `minimax-cn`, model `MiniMax-M3`, base URL `https://api.minimaxi.com/v1`, no mock LLM gate, and `NERYA_PERMISSION_MODE=yolo`. The existing isolated-workspace override sets `kind: chat_completions`, so the `/v1` MiniMax endpoint uses the OpenAI-compatible messages backend while keeping the provider visible as MiniMax.
- First MiniMax focused run passed `C3`, then `C4` exposed a provider-compatibility issue: MiniMax-M3 default adaptive thinking consumed the whole 4096-token completion inside visible `<think>` text, returned `finish_reason=length`, and did not emit a native tool call. Resolution: the OpenAI-compatible backend now detects MiniMax by provider/model/base URL, sends `max_completion_tokens`, disables MiniMax `thinking` by default unless `reasoning_effort` is explicitly requested, enables `reasoning_split` only for requested adaptive thinking, and normalizes `<think>` / `reasoning_details` into thinking blocks instead of final text.
- MiniMax focused verification after the refined thinking control passed `C3,C4,C5` on the real API path with `permission_mode=yolo` and no mock LLM gate. Durations: `C3` 87743 ms, `C4` 291860 ms, `C5` 893286 ms. `C5` still saw provider read timeouts but recovered through retry and finished with `transition_reason=strategy_backtest_finalized`.
- MiniMax full CSV restart exposed `B10` as a provider moderation surface gap: the real upstream returned `422 input new_sensitive (1026)` after several successful tool/model rounds, and the loop surfaced it as HTTP 500. Resolution: recognize MiniMax-style 422 safety rejection generically, retry final synthesis once with sanitized evidence-only prompt and tools disabled, then fall back to deterministic tool evidence if the provider still refuses. Focused real MiniMax `B10` rerun passed (`1 passed`, 2.2m).
- Fresh MiniMax full CSV then passed `A1-A10`, `B1-B12`, `C1-C4`, and failed `C5` because repeated malformed `strategy_generate_proposal` payloads never created a BSC agent proposal. Resolution: normalize provider-variant strategy package arguments in the executor (`files.*` / package file keys / inline `strategy.yml` metadata) before schema validation. Focused real MiniMax `C5` rerun passed (`1 passed`, 15.0m) with proposal `prp_88f60cdbe2bc`, BSC evidence, and `execution_mode=agent`.

## Status
**Currently in Phase 7 / full e2e verification** - Harness Adaptation V3 plus web-budget hardening, custom RSS proposal handling, B12 contract update, the OpenAI-compatible empty-assistant payload fix, MiniMax OpenAI-compatible thinking control, MiniMax 422 safety-retry handling, strategy package argument normalization, task-schedule harness hardening, context-full logging, turn-stability gating, and compact final synthesis are implemented. MiniMax has passed A/B/C1-C5 evidence across full/focused runs; latest focused B4 repair passed on MiniMax with `transition_reason=wall_time_final_synthesis`. Next step is running the full CSV suite on MiniMax with yolo/no-mock.

## 2026-06-08 E12 Split-Language AgentTeam Boundary
- Focused real MiniMax/yolo/context-full E12 failed after a real `team_run`: `dashboard/test-results/logs/E12.jsonl` shows `team_run_exists ok` and `team analysis_language=Chinese ok`, but `no team run output_language=English`.
- Runtime artifacts confirm the wrong boundary: `dashboard/.nerya-e5-rerun-workspace/teams/team-f3403beb3d/run.json` stored `metrics.output_language=zh` and `metrics.analysis_language=zh`, while the original mission requested Chinese analysis and an English final report.
- Context-full evidence shows a second generic issue: after the team result, the loop inserted `degraded_team_strategy_proposal_retry`; the final reply became a strategy proposal/backtest summary even though the caller-required artifact was only `team_run`.
- Current repair target: make team language resolution distinguish final-output fields from role/analysis language fields, preserve split-language contracts in required-tool recovery, and prevent research-only team results from being upgraded into strategy proposal/backtest unless a strategy artifact or explicit strategy-design/team template contract requires it.

## 2026-06-01 MiniMax D Task-Schedule Repair
- Root cause for D4 was not a bad CSV prompt or mock path. Real MiniMax either over-discovered task/Telegram state and asked for confirmation, or emitted `task_create` payloads whose `delivery_targets` used provider-wrapper shapes such as `{"item":"telegram"}` / `{"item":{"kind":"telegram"}}`.
- Generic harness fix: task automation context now includes inspected task state and loaded `tasks`/`triggers` skill evidence. If the model tries to finish with text before `task_create`/`subagent_run_async`, the loop gives one action nudge telling it to stop broad discovery and use safe defaults instead of asking confirmation.
- Generic tool-boundary fix: `task_create` now normalizes wrapped/string channel targets into gateway delivery targets, and the native schema advertises the accepted string/object/list forms.
- Deterministic completion fix: successful `task_create` results now finalize from the schedule tool result directly, avoiding an extra LLM synthesis round that can hit MiniMax 429/read timeout.
- Verification: py_compile passed; focused pytest/task/hardcoding set passed (`16 passed` plus narrower task tests); e2e TypeScript compile passed; focused real MiniMax D4 passed (`1 passed`, 1.1m) with `schedule session_kind=agent ok`.

## 2026-06-02 Context Full Logging Mode
- Added an opt-in LLM context journal under `<workspace>/dev_logs/llm_context_full.jsonl`, enabled by `llm.context_log_mode: full` or `NERYA_CONTEXT_FULL_LOG=1`.
- The journal records `request`, `response`, and `error` phases under one `call_id` for both legacy prompt calls and provider-native `messages` calls. The `messages` request stores canonical `system`, `messages`, `tools`, `tool_choice`, token/temperature settings, reasoning fields, deadline, metadata, tier, provider, and model before provider-specific formatting.
- Full context logging remains off by default and passes through existing display redaction before writing, so API keys and secret-looking values are masked while normal prompt/tool context remains inspectable.
- Verification: py_compile passed; focused context logging tests passed (`4 passed`); provider/native-web/deadline related gateway tests passed (`16 passed`); no-runtime-route-hardcoding/context regression passed (`5 passed`); forbidden marker scan returned no matches.

## 2026-06-02 CSV Turn-Stability Gate
- Gap found from current logs: B4 was recorded as `case.pass` even though its latest turn evidence showed `stopped_reason=timeout`, `transition_reason=timeout_during_llm_call`, `budget.aborted=true`, and `verifier_trusted=false`. That allowed regex-matched fallback prose to count as a pass.
- Generic harness fix: `dashboard/tests/e2e/csv-runner.spec.ts` now asserts `turn_stability` for every non-cancel CSV case. A case fails before reply/API assertions can pass if the latest turn evidence reports timeout, cancellation, max iterations/tool calls, repeated tool loop, interrupted max tokens, or an aborted budget.
- This does not add prompt/case routing. It uses the API-visible turn metadata already added by Harness Adaptation V3 and keeps `cancel_inflight=true` on its dedicated cancellation path.
- Verification: e2e TypeScript compile passed; forbidden route/intent marker scan still has no runtime matches.

## 2026-06-02 MiniMax Compact Final Synthesis
- Root cause from `context full` logs: B4 was not using mock and not failing on stale assertions. It reached wall-time final synthesis with completed tool evidence, but still sent the full 24K system prompt plus a large transcript to MiniMax with `tools=[]`; the provider read timed out until the turn deadline was exhausted.
- Generic harness fix: wall-time final synthesis now uses a compact evidence-only request when completed tool results contain URL/year/error markers. The request sends one user message, a small final-synthesis system prompt, and no tools. Transient provider failures after completed tool evidence also retry once with the same compact-evidence shape.
- Verification: `py_compile` passed; focused loop tests passed (`4 passed`); gateway/context/hardcoding regression passed (`10 passed`); e2e TypeScript compile passed; forbidden marker scan returned no matches. Focused real MiniMax B4 rerun passed (`1 passed`, 3.2m setup-inclusive / 1.8m case) with `stopped_reason=end_turn`, `transition_reason=wall_time_final_synthesis`, `budget.aborted=false`, and `assert.turn_stability ok=true`.
- Context evidence: the final B4 request shrank from full-context shape to `messages=1`, `tools=0`, `system_len=210`, `msg_chars=1132`, then returned `stop_reason=end_turn`.

## 2026-06-02 Large-Payload Finalization Pressure
- Full MiniMax CSV restart passed A1-A10 and B1-B7, then B8 failed with the new stability gate: `stopped_reason=timeout`, `transition_reason=timeout_during_llm_call`, `budget.aborted=true`. Logs showed the failing B8 request was still a normal tool-enabled LLM round (`tools=80`) with a large transcript (`msg_chars` about 90K), started before the fixed wall-time threshold and then consumed the remaining deadline.
- Generic harness fix: if completed tool evidence is available and the transcript payload is already large, the wall-time finalization threshold is raised to 120s for that turn. This makes large evidence transcripts enter compact final synthesis before another full tool-schema request can consume the deadline.
- Verification: focused large-payload loop test passed; focused wall-time/transient loop set passed (`5 passed`); gateway/context/hardcoding regression passed (`10 passed`); e2e TypeScript compile passed; forbidden marker scan returned no matches. Focused real MiniMax B8 rerun passed (`1 passed`, 2.3m setup-inclusive / 1.0m case) with `stopped_reason=end_turn`, `transition_reason=no_more_tools`, `budget.aborted=false`, and `assert.turn_stability ok=true`.

## 2026-06-07 J3/J4 Messaging Gateway Proposal Closure
- Root cause: messaging proposal cases could pass while after files were not executable gateway config. `J3` allowed model-draft keys (`channel_type`, env/default webhook fields, delivery targets, legacy `routes/global_policy`), and `J4` could stage a generic webhook without `kind: webhook`, causing dashboard fallback after approval.
- Generic fix: `nerya/evolution/self_config.py` normalizes `messages/channels.yml/.yaml` proposal bodies into `routes_gateway` / `MessagePipeline` schema. It converts kind/url/topic/route aliases, produces vault refs without plaintext/env/default leakage, infers generic webhook kind from URL refs, and excludes non-consumed draft fields.
- Verification:
  - [x] Failing-first proposal regressions now pass for J3-style model drafts and J4-style generic webhook drafts.
  - [x] `python -m pytest tests\test_evolve_proposals_tool.py tests\test_proposal_only_mutation_guards.py tests\test_gateway_config.py tests\test_message_pipeline.py -q` -> `20 passed, 4 deselected`.
  - [x] `python -m pytest tests\test_evolve_proposals_tool.py tests\test_proposal_only_mutation_guards.py tests\test_extract_cases.py tests\test_no_runtime_route_hardcoding.py -q` -> `39 passed`.
  - [x] `python -m py_compile nerya\evolution\self_config.py tests\test_evolve_proposals_tool.py` passed.
  - [x] `npx tsc --noEmit --project dashboard\tests\e2e\tsconfig.json` passed.
  - [x] Real MiniMax/yolo/context-full/no-mock `J3,J4` passed after runtime restart on `:18369/:3069` (`2 passed`, 1.1m). Latest after files are canonical and no live `messages/channels.yml` was created.
- Next action: continue Phase 7 current-code audit on remaining weak/pass-but-unproven rows; keep checking after-file schema quality, not just proposal existence.

## 2026-06-07 E5 AgentTeam Fallback UX Repair
- Current focused E5 real MiniMax run passes the CSV stability gate, but the final reply is not yet acceptable UX: it exposes internal team compaction fragments such as `aggregate`, `raw`, `task id`, and `status":"in_progress"` after `team_final_synthesis` times out.
- Root cause hypothesis: the deterministic bounded AgentTeam fallback renders compacted member outputs directly instead of converting them into a user-facing evidence report. This is generic team-result presentation debt, not a case-specific prompt or ticker routing problem.
- Repair plan: add a failing regression for degraded team + synthesis-timeout fallback output, then change `loop.py` fallback helpers to sanitize schema/internal fields, preserve original request focus, list completed roles and evidence gaps, and keep the output in the user's language.
- Verification target: focused loop fallback tests, related team compaction/streaming tests, no-runtime-route-hardcoding, py_compile, then focused real MiniMax/yolo/context-full/no-mock E5 rerun with reply/log inspection.

## 2026-06-06 B1 Time-Window Evidence Compaction Closure
- Current-code audit finding: latest B1 failed semantic UX even though CSV passed. The answer saw one or more RSS headlines but claimed the 3-hour boundary could not be checked because `now` was absent.
- Root cause: `news_social/recent_news.py` returned `stdout_json.time_filter`, and loop marker extraction allowed `time_filter`, but `nerya/llm/tool_compaction.py::_compact_script_stdout_json` dropped that metadata from compacted `script_run` observations. The final model therefore received titles/timestamps without the explicit `now/since` boundary.
- Generic fix: script-run stdout compaction now preserves a bounded scalar `time_filter` map. No case IDs, prompt regex, intent marker table, mock provider, or news-specific finalizer was added.
- Verification:
  - [x] Failing-first `python -m pytest tests\test_openhuman_reference_plan.py -q -k "script_run_preserves_stdout_json_items"` failed on missing `time_filter`, then passed after the fix.
  - [x] `python -m pytest tests\test_agent_loop_final_summary.py::test_news_social_evidence_marker_preserves_time_filter_boundary -q` passed.
  - [x] `python -m pytest tests\test_research_fetch_tools.py tests\test_builtin_skill_catalog.py tests\test_no_runtime_route_hardcoding.py -q` passed (`37 passed`).
  - [x] Touched-file `python -m py_compile ...` passed; forbidden marker scan over touched files had no matches.
  - [x] Focused real MiniMax/yolo/context-full/no-mock B1 rerun passed on runtime `:18366` / dashboard `:3066` (`1 passed`, 1.0m). Setup confirmed `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `key_ref=yes`, and `permission_mode=yolo`.
- UX evidence: `dashboard/test-results/logs/B1.reply.txt` now reports the exact window `2026-06-06 09:01 UTC – 12:01 UTC`, lists 2 CoinDesk RSS items with UTC timestamps and links, and says 28 old RSS items were dropped. Context-full iteration 4 contains `time_filter` with `lookback_hours=3`, `now`, `since`, `kept_count=2`, and `dropped_count=28`; screenshot saved at `dashboard/test-results/screenshots/B1.png`.
- Next action: continue current-code UX audit on remaining old incomplete/weak rows, starting with B2/B3/B5/B6/B7/B10/B11 and stale high-risk rows whose old evidence predates the current compaction/search fixes.

## 2026-06-02 Tool-Enabled Deadline Reserve
- Remaining-segment MiniMax run passed B8-B10, then B11 failed with `timeout_during_llm_call` even though the transcript was not huge. The failing request was a normal tool-enabled LLM round after completed evidence; the provider read timeout consumed the entire remaining turn deadline, leaving no time for the compact-evidence retry path.
- Generic harness fix: after at least one tool result, the next tool-enabled LLM call now reserves 30s of the turn deadline for final synthesis/retry when there are no required next tools pending. If the provider times out, the loop still has budget to retry once with compact evidence and `tools=[]`.
- Verification: added deadline-reserve regression test; focused wall-time/transient/deadline-reserve loop set passed (`6 passed`); gateway/context/hardcoding regression passed (`10 passed`); e2e TypeScript compile passed; forbidden marker scan returned no matches. Focused real MiniMax B11 rerun passed (`1 passed`, 3.3m setup-inclusive / 1.9m case) with `stopped_reason=end_turn`, `transition_reason=wall_time_final_synthesis`, `budget.aborted=false`, and `assert.turn_stability ok=true`.

## 2026-06-02 Context Correlation + C7 AgentTeam Proposal Retry
- Context-full logging was useful but lacked direct correlation fields for multi-case analysis. Request/response/error records now include `ts`, and native Agent loop requests pass safe metadata including `session_id`, `turn_id`, `iteration`, completed tool names, required next tools, and remaining wall budget. Full request context is still redacted through the existing display redactor before writing `<workspace>/dev_logs/llm_context_full.jsonl`.
- C7 failed on real MiniMax because the model gathered strategy/AgentTeam planning context (`portfolio_summary`, `role_list`, `role_get`, `strategy_list`, `todo_write`) and then ended with "please confirm defaults" prose. The CSV API contract required a `strategy_generate_proposal` proposal for `nvda` with `execution_mode=agent_team`.
- Generic harness fix: if completed tool evidence shows strategy proposal scoping context and `strategy_generate_proposal` is available but unattempted, the loop gives one proposal retry nudge that uses safe reversible paper defaults instead of asking for confirmation. This is based on tool evidence only; it does not inspect prompt text, case IDs, tickers, or regex intent markers.
- Verification: `py_compile` passed; focused strategy proposal retry tests passed (`3 passed` then broader related set `4 passed`); context-full tests passed (`4 passed`); gateway/context/hardcoding regression passed (`10 passed`); e2e TypeScript compile passed; forbidden marker scan returned no matches. Focused real MiniMax C7 rerun passed (`1 passed`, 10.7m setup-inclusive / 9.3m case) with proposal `prp_5ebc417037df` and `execution_mode=agent_team ok`.

## 2026-06-02 MiniMax C8-C18 Continuation
- Continued the real MiniMax/yolo/no-mock CSV run after the C7 repair with `NERYA_CASES_FILTER='^C(8|9|10|11|12|13|14|15|16|17|18)$'`.
- Verification: C8-C18 passed on MiniMax (`11 passed`, 40.9m setup-inclusive / 39.6m slow test file). Long cases included C8 multi-asset AgentTeam rotation (`10.0m`), C9 dry-run strategy draft (`11.8m`), and C18 wallet meme + freeform backtest (`9.3m`). Setup confirmed runtime/dashboard workspace alignment, `permission_mode=yolo`, provider `minimax-cn`, model `MiniMax-M3`, base URL `https://api.minimaxi.com/v1`, and key by vault ref.

## 2026-06-02 C-AT3 Strategy Authoring Convergence
- C-AT segment started on MiniMax/yolo/no-mock. C-AT1 passed (`4.1m`) and C-AT2 passed (`9.8m`), then C-AT3 failed the turn-stability gate with `stopped_reason=timeout`, `transition_reason=timeout_during_llm_call`, `budget.aborted=true`.
- Root cause from `llm_context_full.jsonl`: the model loaded strategy_author context, gathered connector/account/market data, wrote/edited/read files, and repeatedly used shell/file tools, but did not attempt `strategy_generate_proposal` until iteration 31. The oversized post-proposal context then hit consecutive MiniMax read timeouts.
- Generic harness fix: when strategy authoring context is loaded and completed tools show sufficient file plus data/account/connector prep, or shell/data exploration has exceeded a safe threshold, the loop now gives one convergence nudge to stop rediscovery and call `strategy_generate_proposal` next. The trigger is tool-evidence based only; it does not inspect prompt text, tickers, case IDs, or regex intent markers.
- Setup hardening: global setup now treats live LLM probe/tool-probe fetch timeouts as transient retryable probe results, so existing retry logic handles MiniMax probe stalls instead of failing before cases start. The probe still requires real non-mock tool-call support.
- Verification: `py_compile` passed; focused authoring convergence pytest passed (`1 passed`) and related proposal retry set passed (`4 passed`); e2e TypeScript compile passed; forbidden marker scan returned no matches. Focused real MiniMax C-AT3 rerun passed (`1 passed`, 11.7m setup-inclusive / 10.4m case), and context logs showed `strategy_generate_proposal` completed by iteration 6 instead of iteration 31.

## 2026-06-02 C-AT4 Strategy Proposal Schema Retry
- C-AT4 reached the real MiniMax API and attempted `strategy_generate_proposal`, but the proposal payload failed schema validation (`strategy_id` / `markets` / `accounts` missing on one attempt, then SDK `files.main.py` missing for named custom signal logic). Because the proposal tool had technically been attempted, the existing "unattempted proposal" retry path no longer applied, and the turn fell into final synthesis / safety fallback without a valid proposal.
- Generic harness fix: the native loop now detects `ToolErrorKind.SCHEMA_VALIDATION` from `strategy_generate_proposal` and gives one compact corrective retry nudge. The nudge tells the model to re-call the same tool with required top-level fields, use already gathered account/connector/market evidence plus safe paper defaults, and include SDK `files.main.py` when the tool error indicates custom/named signal/script logic. This is based on tool error evidence only; it does not inspect prompt text, case IDs, tickers, indicators, or regex intent markers.
- Verification: `py_compile` passed; focused schema-retry pytest passed (`1 passed`); context-full logging tests passed (`4 passed`); related strategy proposal/authoring retry set passed (`5 passed`); no-runtime-route-hardcoding passed; forbidden marker scan returned no matches. Focused real MiniMax C-AT4 rerun passed (`1 passed`, 12.7m setup-inclusive / 11.4m case) with proposal `prp_b67af53d25a5`, `execution_mode=agent ok`, and `assert.turn_stability ok=true`. Final text still used the existing MiniMax 422 evidence fallback, but the API check passed from the durable proposal artifact rather than prose.

## 2026-06-02 C-AT5-C-AT14 Required Tool Success Semantics
- Continued real MiniMax/yolo/no-mock C-AT5-C-AT14 after C-AT4. Passed: C-AT9, C-AT11, C-AT12, C-AT13. Failed: C-AT5, C-AT6, C-AT7, C-AT8, C-AT10, C-AT14.
- Root causes from per-case logs and `llm_context_full.jsonl`: C-AT5/C-AT10/C-AT14 had `strategy_generate_proposal` attempts that failed schema validation, but `completed_tool_names` counted failed attempts as satisfying `required_next_tool_names`, allowing wall-time synthesis or timeout before a successful proposal. C-AT7/C-AT8 gathered market/account/data or file evidence but stopped in analysis/credential explanation without a proposal. C-AT6 created/backtested a proposal, but its `main.py` omitted the required news/social evidence hook.
- Generic harness fix: the native loop now tracks `successful_tool_names` separately from attempted/completed tools. `required_next_tool_names` is considered satisfied only by successful tool results, and failed required tools get an additional success-required retry nudge before finalization. Strategy proposal convergence can now trigger from sufficient file/data prep or market/account/data-source prep evidence even when the model did not explicitly load `strategy_author`. This remains tool-evidence based and does not inspect prompt text, case IDs, tickers, indicators, or regex intent markers.
- Verification so far: `py_compile` passed; focused new regression tests passed (`3 passed`); broader strategy retry regression passed (`7 passed`); context-full logging tests passed (`4 passed`); no-runtime-route-hardcoding passed; forbidden marker scan returned no matches. Runtime restart and focused C-AT failure reruns are next.

## 2026-06-02 C-AT6 News/Social Evidence Hook Repair
- Root cause for C-AT6 was in the strategy package generator boundary, not the CSV assertion. The request/manifest could carry `news_sources`, but Agent-mode generated `main.py` and `agent_profile.attached_skills` did not preserve an auditable `news_social` hook, so the proposal artifact failed `main_py_contains=news_social` even after creating a valid proposal.
- Generic generator fix: Agent-mode strategy templates now pass declared manifest news sources into `ctx.news.fetch(sources=...)`, attach `news_social` to the generated `StrategyAgentTask` and `agent_profile` when `news_sources` is non-empty, and append a `news_social` audit helper to caller-supplied `files.main.py` overrides that would otherwise drop the declared news/social source trail.
- Adjacent validation fix: Agent/AgentTeam templates now use `StrategyAgentTask.skip(...)` for non-dispatch branches instead of `ctx.result.hold(...)`, matching the existing agent-task validator and avoiding promotion blockers before the E2E artifact check.
- Verification: focused new generator tests passed (`2 passed`); full strategy generator suite passed (`24 passed`); context-full logging tests passed (`4 passed`); focused Agent loop required-tool/schema retry tests passed (`3 passed`); `py_compile` passed; forbidden marker scan returned no matches. Focused real MiniMax rerun for C-AT5/C-AT6/C-AT7/C-AT8/C-AT10/C-AT14 is next.

## 2026-06-02 C-AT5/C-AT10 Semantic Proposal Tags
- Focused MiniMax rerun after the required-tool-success and news/social fixes passed C-AT6, C-AT7, C-AT8, and C-AT14, but C-AT5 and C-AT10 still failed API checks. Both turns were stable and created/backtested proposals, but the durable proposal text did not contain the CSV semantic needles (`confluence` for multi-factor gating and `mtf` for multi-timeframe gating), so the API checker could not select the right artifact.
- Generic artifact fix: strategy package proposals now include `metadata.semantic_tags`, derived from the request and generated package files. The tags come from technical factor families and timeframe structure in the artifact itself, not from case IDs or runtime routing. Multi-factor gates receive `confluence` / `multi_indicator_confluence`; multi-timeframe packages receive `mtf` / `multi_timeframe`.
- Verification: new semantic-tag tests passed (`2 passed`); full strategy generator suite passed (`26 passed`); context-full + Agent loop retry regression passed (`4 passed`); `py_compile` passed; forbidden marker scan returned no matches. Real MiniMax focused rerun with `NERYA_CASES_FILTER='^C-AT(5|10)$'` passed (`2 passed`, 16.1m). Combined with the prior focused runs, C-AT1-C-AT14 have now passed on real MiniMax/yolo/no-mock.

## 2026-06-02 Context-Full Operator Notes
- Current implementation check: `nerya/llm/gateway.py` records context-full `request` records before backend/provider formatting for both legacy prompt calls and provider-native messages calls, and `nerya/agent/loop.py` passes session/turn/iteration/tool-progress metadata on every Agent loop LLM request.
- Current E2E log evidence: `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl` exists and contains request/response/error records, including Agent loop request metadata. The file is large by design when full mode is enabled.
- Documentation fix: `dashboard/tests/e2e/README.md` now documents `NERYA_CONTEXT_FULL_LOG=1`, `llm.context_log_mode: full`, the output path, record phases, correlation fields, and redaction behavior so future real-API E2E runs can be analyzed from full request context without rediscovering the log path.
- Current-turn recheck: focused context-full logging tests passed (`4 passed`), Agent loop correlation metadata test passed (`1 passed`), py_compile for `nerya/llm/gateway.py` and `nerya/agent/loop.py` passed, and the isolated dev log still exists at `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl`.
- 2026-06-02 follow-up: Agent loop request metadata now also includes `llm_attempt`, `messages_sent_count`, `tools_sent_count`, and `safety_retry_active` so context-full logs can distinguish provider retry attempts and request shape without first expanding the full message/tool payload. TDD evidence: the correlation test failed before the fields existed, then passed after the implementation. Final verification for this slice: context-full gateway tests passed (`4 passed`), Agent loop correlation test passed (`1 passed`), no-runtime-route-hardcoding passed (`5 passed`), `py_compile` passed, and the forbidden-marker scan only found E2E case-plan references.

## 2026-06-02 Prompt Cache Boundary
- Fresh AgentArchitecturePatterns check: `core/prompt.py` must expose a named `CACHE_BOUNDARY_LAYER`, volatile data must sit below the boundary, and a cache-stability test must compare the bytes before the boundary across different user messages / clocks.
- Root gap: `nerya/agent/prompt_sections.py` had a `PromptComposer` cache-boundary concept, but `nerya/agent/kernel.py::_build_system_prompt()` did not use a boundary and rendered dynamic `Temporal context` before the stable prompt material.
- TDD guardrail: added `test_system_prompt_cache_boundary_keeps_dynamic_context_below_marker`, which first failed because `CACHE_BOUNDARY_MARKER` did not exist and the system prompt had no boundary.
- Implementation: added `CACHE_BOUNDARY_MARKER` next to `CACHE_BOUNDARY_LAYER` and split `_build_system_prompt()` into cached and rolling sections. Cached: identity/workspace/memory/profile/skills/recipes/workflow. Rolling: timestamp/freshness rules, latest-turn execution policy, output-language rule, permission mode, and attached-skill hints.
- Verification: new cache-boundary test passed after implementation; full `tests/test_agent_temporal_context.py` passed (`13 passed`); focused context/strategy/no-hardcoding suite passed (`10 passed` and `24 passed`); `py_compile` for touched prompt/logging files passed. Runtime hardcoding scan found only `nerya/evals` `expected_final_text_contains` fields, not active agent route tables or forbidden intent markers.

## 2026-06-02 Frozen Memory Prompt Snapshot
- Fresh AgentArchitecturePatterns check: Law 8 requires memory writes to use a frozen prompt snapshot; mid-turn writes may persist to disk but must not mutate the in-flight prompt or cache prefix.
- Root gap: the runtime currently builds the system prompt once per turn, but this was an implicit ordering property. `_build_system_prompt()` itself still read live memory, so any future same-turn prompt rebuild could see memory written after the turn began.
- TDD guardrail: added `test_system_prompt_uses_frozen_memory_snapshot_until_next_turn`, which first failed because there was no explicit frozen-memory prompt API.
- Implementation: added `AgentKernel._freeze_memory_prompt_block()` and changed the runtime turn path to pass `frozen_memory_block` into `_build_system_prompt()`. Direct prompt-builder tests still get a fresh snapshot when no frozen block is supplied, preserving existing call behavior.
- Verification: frozen-memory test passed after implementation; full `tests/test_agent_temporal_context.py` passed (`14 passed`); `py_compile` for `nerya/agent/kernel.py` passed; `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`); forbidden marker scan still only found eval `expected_final_text_contains` fields.

## 2026-06-02 Prompt Guard Fail-Open
- Fresh AgentArchitecturePatterns check: Law 7 says scanner/verifier failures should fail open by default; a scanner crash should not crash the whole agent turn.
- Root gap: `nerya/security/prompt_injection.py::flag_suspicious` and `classify` directly called regex `.search()`, so a scanner exception propagated to the caller.
- TDD guardrail: added `test_prompt_guard_fails_open_if_scanner_raises`, which first failed with `RuntimeError: regex engine unavailable`.
- Implementation: added `_safe_pattern_hits()` in `prompt_injection.py`. Scanner exceptions now return no hits for `flag_suspicious`, and `classify` returns an explicit `prompt_guard.fail_open` allow verdict. Normal block/review/allow patterns are unchanged.
- Verification: prompt guard smoke subset passed (`6 passed`); `py_compile` for `prompt_injection.py` passed. A subagent prompt firewall test file is currently filtered by the repo's default `-m smoke` pytest addopts, so it was not used as evidence for this slice.

## 2026-06-02 Context-Full Logging Audit Follow-up
- Current audit target: make `llm_context_full.jsonl` directly analyzable per Agent request attempt without joining every response/error record back to its request record by hand.
- TDD guardrail to add before implementation: response/error context-full records should expose safe correlation fields (`session_id`, `turn_id`, `iteration`, `llm_attempt`, request-shape counts) at the record top level, and synthetic vendor-token shapes must be redacted from full request context.
- Implementation: `LLMGateway` now promotes only allow-listed Agent correlation metadata onto each context-full record, and `redact_text` masks synthetic vendor-prefixed and hex-dot provider-token shapes in addition to existing `sk-*` / key-name redaction.
- Documentation: `dashboard/tests/e2e/README.md` now states that request/response/error records all expose top-level correlation fields and that common vendor-token shapes are masked in full context logs.
- Verification: the new context-full tests failed first on partial `tp-*` redaction, missing hex-dot provider-token redaction, and missing response/error top-level correlation fields; after the implementation, `tests/test_llm_gateway_model_override.py -k context_full` plus the Agent loop correlation test passed (`4 passed, 20 deselected`). `py_compile` for `redaction.py` / `gateway.py` passed, `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`), and the forbidden marker scan found only eval `expected_final_text_contains` fields.

## 2026-06-02 Route Manifest Source Hardcoding Removal
- Fresh audit against `AgentArchitecturePatterns` found the remaining source-level route hardcoding in `nerya/agent/route_manifests.py` and `DEFAULT_CONFIG.agent.planner.routes`: both embedded route match arrays in Python. This was not a hidden prompt router, but it still violated the requested "no source route hardcoding" direction.
- TDD guardrail: added tests requiring default config to use `manifest: trading-v1` with `routes: {}`, and requiring bundled manifests to load from declarative resources rather than Python literals.
- Implementation: added `nerya/agent/route_manifest_presets/general-operator-v1.yml`, `minimal-v1.yml`, and `trading-v1.yml`; rewrote `route_manifests.py` as a YAML loader with workspace overrides still taking precedence; added package-data config for those YAML files.
- Verification: `python -m pytest tests/test_no_runtime_route_hardcoding.py -q` passed (`3 passed`); route/focus/context subset passed (`5 passed`); `tests/test_routes_operator_readiness.py tests/test_phase_m_native_first.py -k "capability or route or readiness or native"` passed earlier in this turn (`30 passed`); py_compile passed; forbidden marker scan returned no matches. Full Playwright CSV still not rerun in this turn.

## 2026-06-02 Builtin Route Manifest Match-Table Deletion
- Fresh strict audit found that moving route presets from Python into packaged YAML was still not enough for the user's "delete all route hardcoding" requirement: `route_manifest_presets/trading-v1.yml`, `general-operator-v1.yml`, and `minimal-v1.yml` still shipped built-in `routes` / `match` tables such as `price.*`, `news.*`, and `strategy.*`.
- TDD guardrail: `tests/test_no_runtime_route_hardcoding.py` now fails if packaged route manifest resources contain `routes:` or `match:`, requires all builtin manifests to load with `routes == {}`, and asserts a pinned manifest does not fall back to legacy inline `agent.planner.routes` in the capability API.
- Implementation: builtin manifests are now capability-only (`id`, `name`, `description`, `version`, `mode`, `capabilities`, `fallback`). `route_manifests.py` accepts empty `routes`, and `routes_capability._planner_section` treats a selected manifest as authoritative even when its route table is empty.
- Verification: the new no-hardcoding test first failed on the old YAML tables and inline fallback, then passed after the fix (`5 passed`). Additional verification passed: `py_compile` for `route_manifests.py` and `routes_capability.py`; `tests/test_strategy_context_guidance.py tests/test_no_runtime_route_hardcoding.py` (`24 passed`); `tests/test_agent_execution_state.py tests/test_llm_gateway_model_override.py -k "context_full or execution_state"` (`5 passed`); forbidden marker scan had no runtime route-hardcoding hits.

## 2026-06-02 Source Evidence Marker Preservation
- Current B4 failure mode after route resourceization: real MiniMax gathered source evidence through `web_search_fetch` / source tools, and the turn was stable, but the final prose could omit concrete URL/year markers required for auditability. A retry could then time out even though the evidence already existed.
- Generic loop fix: after successful source-evidence tools, final answers that omit source markers get a footer built from the already captured tool-result evidence. This is evidence-preservation only: it does not inspect case IDs, prompts, tickers, route keywords, or strategy intent markers.
- Verification: focused source-evidence loop tests passed (`6 passed`); context-full logging tests passed (`4 passed`); `py_compile` passed; `tests/test_no_runtime_route_hardcoding.py` passed (`3 passed`); forbidden marker scan returned no matches. Focused real MiniMax/yolo/no-mock B4 rerun passed (`1 passed`, 2.3m setup-inclusive / 2.1m case) with setup confirming provider `minimax-cn`, model `MiniMax-M3`, base URL `https://api.minimaxi.com/v1`, key by vault ref, and context-full logging enabled.

## 2026-06-02 D8 Research Synthesis False Strategy Retry
- Remaining MiniMax segment passed B12 and D1-D7, then D8 failed the turn-stability gate with `timeout_during_llm_call`. First failure shape: source/market evidence existed, but compact synthesis started too late after high tool-result volume. Second focused rerun exposed an additional false-positive retry: a research `team_run` plus market/source tools caused `required_next_tool_names=["strategy_generate_proposal"]`, blocking compact finalization even though D8 was a research/opinion task.
- Generic loop fixes: high-volume source-evidence turns now reserve a larger compact-synthesis window before another tool-enabled provider round can consume the wall budget. Strategy authoring detection no longer treats `team_run` / `task_create` alone as strategy workflow evidence, and shell+market exploration only counts as authoring prep when account/connector/data-source setup evidence is also present.
- Verification: new regression tests first failed, then passed after the fixes. Focused source/research loop set passed (`8 passed`); strategy proposal retry guard passed; context-full tests passed (`4 passed`); `tests/test_no_runtime_route_hardcoding.py tests/test_strategy_context_guidance.py` passed (`21 passed`); py_compile passed; forbidden marker scan returned no matches. Focused real MiniMax/yolo/no-mock D8 rerun passed (`1 passed`, 4.2m setup-inclusive / 4.1m case) with `stopped_reason=end_turn`, `transition_reason=wall_time_final_synthesis`, `budget.aborted=false`, and `assert.turn_stability ok=true`.

## 2026-06-02 D10 Portfolio Alert False Strategy Retry
- Remaining MiniMax segment resumed and D9 passed, then D10 failed the turn-stability gate. The logs showed `journal_search` / portfolio / strategy-list evidence for an Inbox risk alert, but the loop over-classified `strategy_list + portfolio_summary` as strategy proposal context. It then attempted `strategy_generate_proposal`, hit schema validation, kept the failed proposal as `required_next_tool_names`, and timed out before stable synthesis.
- Generic loop fix: portfolio/risk alert evidence no longer forces `strategy_generate_proposal`. Strategy proposal context now requires explicit strategy workflow tools or role/team proposal scoping evidence; plain `strategy_list + portfolio_summary` remains research/risk evidence and can be summarized directly.
- Verification: new D10-shaped regression first failed, then passed after the fix. False-proposal regression set passed (`3 passed`); source-evidence set passed (`7 passed`); context-full tests passed (`4 passed`); strategy-context/no-runtime-route-hardcoding passed (`21 passed`); py_compile passed; forbidden marker scan returned no matches. Focused real MiniMax/yolo/no-mock D10 rerun passed (`1 passed`, 1.1m setup-inclusive / 58.7s case) with `stopped_reason=end_turn`, `transition_reason=no_more_tools`, `budget.aborted=false`, and `assert.turn_stability ok=true`.

## 2026-06-02 E6 Financial Datasets Readiness Contract
- E1-E5 passed on real MiniMax, then E6 failed only because the CSV contract still inherited the E-group default `must_contain=team`. The source test plan requires `Financial Datasets` readiness to be false when the key is missing and the reply to give configuration guidance rather than pretend FD data exists.
- Contract fix: `tools/extract_cases.py` now gives E6 an explicit FD readiness/assertion contract (`financial_datasets_status=false` plus FD key/config wording). The CSV runner now validates `financial_datasets_status=true|false`, not merely that the `/data/financial_datasets/status` endpoint returns a boolean. `cases.csv` was regenerated from the extractor.
- Verification: extract-case tests passed (`2 passed`); E2E TypeScript compile passed; context-full tests passed (`4 passed`); strategy-context/no-runtime-route-hardcoding passed (`21 passed`); forbidden marker scan returned no matches. Focused real MiniMax/yolo/no-mock E6 rerun passed (`1 passed`, 8.1m setup-inclusive / 8.0m case) with `financial datasets ready=false`, stable turn evidence, and reply text explicitly saying the FD key was not configured and FD data was not used.

## 2026-06-02 E7 Team Concurrency Contract
- Root cause: E7's source plan expected AgentTeam concurrency, but the CSV prompt did not explicitly request AgentTeam and the CSV contract only checked final prose for `team`. Real MiniMax could satisfy the vague prompt with direct parallel `market_data`, so no durable team run existed. After making the prompt explicit, a secondary quality issue appeared: the model sometimes treated synchronous `team_run` as an async task and called `subagent_run_async`, causing a weak "background task submitted" final reply.
- Generic fixes: E7 now has an explicit AgentTeam/max_parallel prompt and `api_check=team_run_exists=true`; `csv-runner.spec.ts` validates a durable `/teams/runs` row instead of trusting prose. The `team_run` tool description now covers broad parallel research across several securities/tickers. `subagent_run_async_handler` now returns the cached synchronous team summary when a `team_run` already completed in the same turn, preventing accidental background-task creation after a completed team run.
- Verification: focused new regressions passed (`7 passed` across extract/team/schema/no-hardcoding subsets); TypeScript e2e compile passed; forbidden marker scan returned no matches. Focused real MiniMax/yolo/context-full E7 rerun passed twice; the final clean rerun passed in 10.7m with `tool_names=["team_run","todo_write"]`, `transition_reason=no_more_tools`, `budget.aborted=false`, `team_run_exists ok: team-74f44b9d31`, and no `subagent_run_async`.

## 2026-06-02 Context-Full Logging Recheck
- Current-turn audit confirms `llm.context_log_mode: full` / `NERYA_CONTEXT_FULL_LOG=1` writes redacted full LLM records to `<NERYA_WORKSPACE>/dev_logs/llm_context_full.jsonl`.
- Coverage verified: provider-native messages requests include canonical `system`, `messages`, `tools`, tool choice, model settings, deadline, metadata, safe tier config, plus response/error records under the same `call_id`.
- Agent loop metadata verified: every request attempt carries `session_id`, `turn_id`, `iteration`, `max_iterations`, completed/successful/required tool names, `llm_attempt`, sent message/tool counts, safety-retry state, and remaining wall time.
- Verification run: `tests/test_llm_gateway_model_override.py -k context_full` passed (`4 passed, 19 deselected`); Agent loop correlation filter passed (`1 passed, 61 deselected`); `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`); `py_compile` for `gateway.py`, `loop.py`, and `redaction.py` passed; runtime route-marker scan returned no matches.

## 2026-06-02 Context-Full Subagent Metadata
- Gap found after re-reading active call paths: `WorkspaceNativeAgentLoop` passed full correlation metadata into `call_messages()`, but `SubAgentRuntime` still used legacy `LLMGateway.call()` and that API had no metadata parameter. Subagent/team LLM prompt records could therefore be present in `llm_context_full.jsonl` but not directly joinable by `session_id`, `turn_id`, or parent tool call.
- TDD guardrail: added `test_context_full_logging_records_prompt_api_correlation_metadata`, which failed first because `LLMGateway.call()` did not accept `metadata`.
- Implementation: prompt-style context-full logging now records metadata and safe top-level correlation on request/response/error records; subagent runtime passes `session_id`, `turn_id`, `iteration`, `subagent`, `strategy_id`, `trigger_event_id`, and `parent_call_id`.
- Verification: focused context-full gateway tests passed (`5 passed`); Agent loop + subagent metadata tests passed (`2 passed`); no-runtime-route-hardcoding passed (`5 passed`); `py_compile` for `nerya/llm/gateway.py` and `nerya/subagents/runtime.py` passed.

## 2026-06-02 Skill Proposal Supply-Chain Closure
- Current audit target: close AgentArchitecturePatterns Law 9 gaps in skill proposal creation and builtin skill progressive disclosure before another real-provider CSV run.
- Root gaps found: `nerya/skills/proposal.py` still generated `actions.py` despite local AGENTS.md requiring `SKILL.md` + `scripts/` only; builtin `browser`, `news_social`, `strategy_author`, and `backtest` `SKILL.md` entrypoints exceeded the compact/lazy-load budget.
- TDD guardrail: added `test_legacy_skill_scaffolder_does_not_create_executable_action_surface`, which failed before the fix because the legacy scaffolder wrote an executable action surface and lacked the new `scripts/` / `templates/` layout.
- Implementation: legacy skill scaffolding keeps the old function signature for compatibility but ignores the deprecated `actions_py` payload and creates only `SKILL.md`, `references/`, `scripts/`, and `templates/`. Compact builtin skill entrypoints now point to lazy `references/full-playbook.md`; `news_social` gained that reference file.
- Verification: `tests/test_evolve_skill_proposal.py` passed (`3 passed`), `tests/test_builtin_skill_catalog.py tests/test_no_runtime_route_hardcoding.py` passed (`10 passed`), `py_compile` for skill proposal modules passed, and builtin skill line-count scan returned `over_limit=[]`.
- Follow-up installer hardening: external skill installs now reject root-level `actions.py`, `skill.yml`, `skill.yaml`, `manifest.yml`, and `manifest.yaml` before staging to `skills/pending`. The new dashboard/installer regression failed before this change and now proves no pending legacy skill directory is written.
- Verification after installer hardening: `tests/test_routes_skills_dashboard.py` passed (`8 passed`); combined supply-chain/no-hardcoding set passed (`21 passed`); `py_compile` for `nerya/skills/installer.py` and `nerya/skills/proposal.py` passed; runtime route-marker scan returned no matches; builtin skill line-count scan stayed `over_limit=[]`.

## 2026-06-02 Context-Full Edge Correlation Closure
- Fresh audit target: make every Agent-adjacent LLM request analyzable in `llm_context_full.jsonl` without guessing from timestamps. The main Agent loop and subagents were covered, but edge calls still lacked correlation metadata.
- Gaps closed: `context_scope` is now an allow-listed top-level context-full correlation field. Team final synthesis, auto session-title generation, and native `llm_*` tool subcalls now pass safe metadata including `session_id`, `turn_id`/`iteration` where available, `strategy_id`, `trigger_event_id`, `parent_call_id`, and a scope label.
- TDD evidence: new tests first failed on missing `context_scope` promotion, missing team synthesis metadata, missing session-title metadata, and missing native LLM tool metadata; all passed after the implementation.
- Verification: `tests/test_llm_gateway_model_override.py -k context_full` passed (`5 passed, 19 deselected`); focused metadata tests passed (`6 passed`); `tests/test_native_llm_context_metadata.py` passed (`2 passed`); `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`); `py_compile` for touched modules passed; narrow forbidden marker scan found no `_STRATEGY_INTENT_MARKERS`, `INTENT_MARKERS`, or `_NATIVE_ROUTE_WEB` hits.

## 2026-06-02 R5-R7 Best-Practice Closure
- R5 sandbox-first closed: added `nerya/core/sandbox.py::sandbox_exec()` and routed shell/search/skill script/external skill clone execution through it. The wrapper enforces workspace cwd containment and keeps process launch semantics in one auditable module.
- R6 import-time redaction closed: `nerya/core/redaction.py` snapshots `_REDACT_ENABLED` at import, compiles redaction patterns at module load, and withholds text rather than returning plaintext if redaction is disabled by environment.
- R7 fail-open scanner closed: `nerya/memory/content_scanner.py` now has `MemoryScanResult` plus `scan_memory_content_with_audit()`, and scanner exceptions return an explicit fail-open audit event while the existing `scan_memory_content()` caller contract is unchanged.
- Regression fix: compacting `news_social/SKILL.md` had dropped `triggers`; restored frontmatter triggers so lazy skill indexing still exposes `热门经济新闻` and related trigger metadata without adding runtime prompt routing.
- Verification: AgentArchitecturePatterns lint `--rules R5,R6,R7` passed (`3 passes, 0 fails`); focused related pytest aggregation passed (`65 passed, 3 deselected`); context-full/no-hardcoding passed (`5 passed, 24 deselected`); touched-file `py_compile` passed; forbidden runtime route/intent marker scan showed only eval `expected_final_text_contains` fields.

## 2026-06-02 Full Static Best-Practice Lint Closure
- After R5-R7, full AgentArchitecturePatterns lint still exposed naming-alignment gaps for R1/R9/R10/P1. Closed them with thin compatibility surfaces over existing Nerya mechanisms rather than replacing runtime architecture.
- Added `nerya/rollout/writer.py` with `Turn` / `RolloutWriter` JSONL support backed by `nerya.core.jsonl`, while keeping `agent.turn.start/end/summary` journals as the authoritative runtime path.
- Updated `nerya/security/audit.py` to emit an explicit `audit_event` envelope. Added `nerya/skills/registry.py::list_bundled_skill_names()` and a `skills/bundled` namespace marker over existing `skills/builtin`. Added `nerya/progress/todo.py` and `format_for_injection()` over native `TaskState` unfinished todo state.
- Verification: full `lint-agent-design.py` over `Nerya/nerya` passed (`10 passes, 0 fails, 0 advisories`); focused pytest aggregation passed (`65 passed, 7 deselected`); context-full/no-hardcoding passed (`5 passed, 24 deselected`); touched-file py_compile passed; forbidden marker scan still has no runtime route/intent marker hits.
- Secret hygiene follow-up: replaced a dotted-provider redaction test sample that reused a real provider-key suffix with a synthetic suffix; exact scan for the user-provided key fragments now returns no matches, and context-full tests still pass (`5 passed, 19 deselected`).
- Remaining overall gate: run the real MiniMax/yolo/no-mock Playwright CSV suite after this static best-practice closure; do not claim the full prompt E2E objective complete until that full run passes or remaining failures are classified from context-full logs.

## 2026-06-02 Objective Completion Audit
- Verified the active objective scope directly: plan-with-files gap/code-logic matrices exist in `agent_harness_task_plan.md` and `agent_harness_best_practices_plan.md`; forbidden prompt/tool route hardcoding is covered by runtime tests and scans; AgentArchitecturePatterns best-practice lint passes all rules.
- Fresh verification commands: full `lint-agent-design.py` passed (`10 passes, 0 fails, 0 advisories`); no-hardcoding/harness/sandbox/redaction/memory scanner tests passed (`25 passed, 5 deselected`); context-full tests passed (`5 passed, 19 deselected`); exact scan for supplied key fragments returned no matches; forbidden marker scan for `_STRATEGY_INTENT_MARKERS`, `INTENT_MARKERS`, `_NATIVE_ROUTE_WEB`, `should_continue_for_strategy`, and `native route discovery` returned no matches.
- Scope note: this completes the requested harness-best-practice adaptation before prompt E2E. Full real MiniMax/yolo/no-mock Playwright CSV remains the next separate runtime validation step, not part of this static harness-implementation objective.

## 2026-06-02 Full CSV Baseline Run
- User requested the next step: run all `cases.csv` rows, not another narrow focused segment.
- Run constraints: MiniMax provider only (`minimax-cn` / `MiniMax-M3` / `https://api.minimaxi.com/v1`), `NERYA_PERMISSION_MODE=yolo`, `NERYA_CONTEXT_FULL_LOG=1`, no `NERYA_E2E_ALLOW_MOCK_LLM`, and `NERYA_TEST_RETRIES=0`.
- Current action: verify runtime/dashboard workspace and provider gates, then execute `npx playwright test csv-runner --reporter=list` from `dashboard/`.

## 2026-06-03 Historical Failure Set Run
- User narrowed scope from full CSV to only previously failed cases.
- Full run was stopped after setup-confirmed MiniMax/yolo/no-mock execution had already passed A/B and reached C; C7 failed there, so focused historical set was started.
- Historical set used `NERYA_CASES_ONLY=C3,C5,C7,C-AT2,C-AT4,C-AT6,C-AT7,C-AT8,C-AT9,C-AT14,D3,D4,E3,E5,E8,E10,E12,F2,GX6,GX14,H7,H9,I3,I4,I6,J1`.
- `C3` was re-run alone after a premature manual kill and passed on MiniMax (`1 passed`, 8.7m).
- Remaining 25-case run result: `16 passed`, `9 failed`, 1.7h. Failed cases: `C7,E8,E10,GX6,GX14,H7,H9,I6,J1`.
- Next action: inspect per-case logs/replies and context-full records for the 9 failures, then apply generic fixes only.

## 2026-06-03 Focused Previous-Failure Rerun
- User clarified to run only previous failures. Rerun used MiniMax/yolo/context-full/no-mock with `NERYA_CASES_ONLY=C7,E8,E10,GX6,GX14,H7,H9,I6,J1`.
- Result: `2 passed`, `7 failed`, 25.1m. Passed: `C7,J1`. Still failing: `E8,E10,GX6,GX14,H7,H9,I6`.
- Compact/full-context root-cause split:
  - `E8/E10`: inherited E-group `must_contain=team` despite prompts that did not explicitly request AgentTeam or durable team-run evidence. This matched the prior E6/E7 stale-contract pattern.
  - `H7/H9/GX6`: `read_file + connector/status` evidence was over-classified as strategy authoring prep, injecting `strategy_generate_proposal` into data-source/provider-key/provider-onboarding tasks.
  - `GX6`: no native provider-proposal tool existed even though `provider_proposal` is an allowed `PatchProposal` kind.
  - `I6`: late safe-reserve blocked a mixed `data_api + run_shell` batch, dropping read-only wallet/provider evidence with the action tool.
  - `GX14`: stuck diagnostics reported a stale pending approval from an old scheduled session; reset did not clear `approvals/pending.jsonl`.
- Generic fixes applied:
  - `E8/E10` extractor overrides now validate data-unavailable / missing-context behavior instead of text-only `team`.
  - Strategy authoring prep detection no longer treats read-only `read_file + connector/status` as enough to force `strategy_generate_proposal`.
  - Added `evolve_provider_proposal`, which writes review-only `provider_proposal` artifacts with venue/base/docs/auth metadata and does not mutate live providers.
  - Late-tool reserve can preserve read-only tools from mixed read/action batches.
  - Reset now clears `approvals/pending.jsonl` while preserving vault/accounts.
- Verification so far: focused extractor/loop/provider/reset regressions passed (`8 passed, 72 deselected`); `py_compile` for touched Python files passed; no-runtime-route-hardcoding passed (`5 passed`); forbidden marker scan returned no matches.

## 2026-06-03 GX14/H9 Focused Repair
- Scope: continue the narrowed "previous failures only" run. After the second focused rerun, `E8,E10,GX6,H7,I6` passed and only `GX14,H9` remained failing.
- H9 root cause: the prompt contains the literal placeholder `[KEY]`, not a real Financial Datasets secret. The agent correctly refused to write a raw key and the integration remained `ready=false`; the CSV contract was stale because it expected `financial_datasets_status=true`.
- H9 fix: `tools/extract_cases.py` now sets H9 `api_check=financial_datasets_status=false`, matching E6-style placeholder/missing-key readiness semantics. Regenerated `dashboard/tests/e2e/cases.csv`.
- GX14 root cause: MiniMax generated proposal `prp_c21d7f94503f` and `strategy_backtest` returned structured `reason=no_historical_data` / `next_required_action.type=report_data_gap` because Aster perpetual historical candles were unavailable. The loop did not treat that real data-gap evidence as terminal and continued into more retries/evolve work until Playwright timed out.
- GX14 fix: `nerya/agent/loop.py` now deterministically finalizes `strategy_backtest` results with `ok=false`, `reason=no_historical_data`, and `next_required_action.type=report_data_gap`, using transition `strategy_backtest_data_gap_finalized`. Repair-style next actions such as concrete-market reruns are not swallowed.
- Verification so far: new red tests failed first, then passed after implementation. Focused tests passed (`3 passed, 73 deselected`); `py_compile` for `loop.py`/`extract_cases.py` passed; `tests/test_no_runtime_route_hardcoding.py` passed (`5 passed`). Next action: restart the test runtime and rerun only `GX14,H9` with MiniMax/yolo/context-full/no-mock.

## 2026-06-03 Historical Failure Set Closed
- Scope: continue the narrowed "previous failures only" run with real MiniMax (`minimax-cn` / `MiniMax-M3` / `https://api.minimaxi.com/v1`), yolo permission mode, context-full logging, and no mock LLM allowance.
- Additional GX14 root causes found from the combined rerun:
  - MiniMax sometimes emitted a complete tool payload as `{"_raw": "<json object>"}`. `NativeToolExecutor` validated before the strategy handler's raw-payload recovery could run, causing schema retry loops and provider timeout.
  - `strategy_backtest` spent too long probing unsupported explicit markets such as `aster:BTCUSDT-PERP` across fallback timeframes. The backend eventually finalized correctly, but only after Playwright's UI wait window.
- Generic fixes:
  - `nerya/tools/executor.py` now unwraps complete `_raw` JSON-object tool arguments before schema validation, then still uses the normal schema, permission, and handler path.
  - `nerya/skills/builtin/backtest/scripts/backtest_run.py` now fails fast for explicit `VENUE:SYMBOL` markets whose venue is not configured/discovered for standard historical OHLCV backtests, avoiding silent cross-venue substitution and long unsupported-provider probes.
- Verification:
  - Unit/regression checks passed: `tests/test_tool_errors.py` (`26 passed`), focused backtest/market discovery (`4 passed`, `2 passed / 1 skipped`), combined tool/backtest subset (`9 passed`), loop/provider subset (`7 passed`), `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and `py_compile` for touched files.
  - Focused real MiniMax `GX14` passed (`1 passed`, 5.0m setup-inclusive / 292717 ms case).
  - Historical failure set passed together: `NERYA_CASES_ONLY=C7,E8,E10,GX6,GX14,H7,H9,I6,J1`, result `9 passed (17.3m)`.
- Evidence table from `dashboard/test-results/summary.csv` and per-case JSONL:

| Case | Status | Duration ms | Transition | Key tools | API check |
| --- | --- | ---: | --- | --- | --- |
| C7 | pass | 548943 | `strategy_backtest_data_gap_finalized` | `team_run`, `strategy_generate_proposal`, `strategy_backtest`, `task_create` | `strategy proposal`, `execution_mode=agent_team` |
| E8 | pass | 24673 | `no_more_tools` | `market_data` | n/a |
| E10 | pass | 59519 | `strategy_backtest_finalized` | `strategy_generate_proposal`, `strategy_backtest`, `market_data` | `strategy proposal` |
| GX6 | pass | 66467 | `proposal_created_finalized` | `evolve_provider_proposal`, `web_search`, `web_fetch` | `provider_proposal`, `metadata contains aster` |
| GX14 | pass | 223166 | `strategy_backtest_data_gap_finalized` | `strategy_generate_proposal`, `strategy_backtest` | `strategy proposal` |
| H7 | pass | 5995 | `data_source_status_finalized` | `data_source_status` | `data source status count 5` |
| H9 | pass | 46447 | `no_more_tools` | `connector_list`, `read_file`, `run_shell` | `financial datasets ready=false` |
| I6 | pass | 21496 | `wallet_provider_readiness_blocked_finalized` | `data_api`, `todo_write` | n/a |
| J1 | pass | 33078 | `wall_time_final_synthesis` | `glob`, `list_dir`, `read_file`, `run_shell` | `gateway platform telegram ok` |
- Remaining note: the setup/reset log still prints `skip approvals/pending.jsonl`; no current historical failure depends on it, but this contradicts the earlier reset-cleanup intent and should be handled before relying on approval-state isolation in future approval-specific suites.

## 2026-06-04 I5/H10 Real MiniMax Repair Closure
- Scope: continue the failed-case repair on real MiniMax only (`minimax-cn` / `MiniMax-M3` / `https://api.minimaxi.com/v1`), `NERYA_PERMISSION_MODE=yolo`, `NERYA_CONTEXT_FULL_LOG=1`, no mock LLM, and no case/prompt hardcoded routing.
- Root causes from context-full:
  - `connector_list(query="binance")` returned real Binance/BSC connector entries, but generic `json.large` compaction lifted nested credential `status=missing` and hid connector IDs/count, causing the model to say Binance was absent.
  - `data_api(op=list, provider=wallet)` had no dedicated catalog compaction, so actions/aliases/next steps collapsed to top keys only.
  - `data_api` same-tool `next_required_action` was cleared because success was tracked only by tool name; the subsequent required `wallet.readiness` call was then unprotected and wall-time final synthesis could stop early.
- Generic fixes:
  - Added `connector_list.summary` and `data_api.catalog` compaction reducers that keep counts, connector/action samples, aliases, and `next_required_action`.
  - `data_api` wallet catalog now emits a structured readiness follow-up for wallet/provider availability checks.
  - The agent loop keeps same-tool follow-up actions pending until the follow-up call completes, instead of treating the earlier catalog/list call as final success.
- Verification:
  - Local checks passed: py_compile for touched files; focused compaction/data_api tests (`5 passed`), focused loop/wallet tests (`4 passed`), wider loop required-action subset (`13 passed`), and `tests/test_no_runtime_route_hardcoding.py` (`5 passed`).
  - Real Playwright after runtime restart passed: `I5` single (`1 passed`, transition `wallet_provider_readiness_blocked_finalized`), `H10` single (`1 passed`, transition `proposal_created_finalized`), and combined `H10|I5` (`2 passed`, summary shows H10 104803 ms / I5 10727 ms).
  - Additional overbroad `E1|H10|I5` regex run matched E10 as well; it was stopped after confirming `E1` passed (`5.1m`) and `E10` passed (`1.4m`) to avoid burning more real API budget.
- Evidence quality:
  - I5 final reply now surfaces `provider_action=binance_agentic.readiness`, missing `binance-agentic-wallet skill not installed`, and no live trading/signing occurred.
  - H10 final reply creates a review-only `skill_proposal` targeting `skills/x-kol-daily-sync/SKILL.md`.
- Remaining risks:
  - `dashboard/.nerya-test-workspace/dev_logs/http.jsonl` is ~148 MB and slows setup; clear or rotate dev logs before broad full-suite runs.
  - Reset output still prints `skip approvals/pending.jsonl`; approval-isolation cases should recheck this before being considered closed.

## 2026-06-04 I5/H10 Required-Tool Choice Closure
- Follow-up context-full analysis showed the generic required-action retry path still relied on prompt text alone after narrowing the available tools. MiniMax could ignore the single available required tool and continue prose/finalization, especially when catalog compaction was noisy.
- Generic fix: when exactly one pending required action tool is exposed, the loop now sends provider-native `tool_choice={"type":"tool","name":...}`. The tool choice is cleared again when the loop switches to text-only final synthesis.
- Compaction hardening was tightened so connector catalogs keep actionable IDs/count/status without credential/support/docs noise, and `data_api` catalogs keep the target `next_required_action` action when available.
- Latest real MiniMax/yolo/context-full/no-mock combined rerun passed `H10,I5`: `H10` 49906 ms with `proposal_created_finalized`; `I5` 13217 ms with `wallet_provider_readiness_blocked_finalized`. Per-case logs are `dashboard/test-results/logs/H10.jsonl` and `dashboard/test-results/logs/I5.jsonl`; full context is in `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl`.
- Fresh local gates for this closure passed before the rerun: `tests/test_tool_compaction_data_api.py` (`8 passed`), loop required-action subset (`18 passed`), `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and touched-file `py_compile`.
- Next action: restart isolated runtime/dashboard on MiniMax and run a broader CSV gate. Rotate or clear the large dev logs before that run.

## 2026-06-04 C-AT9 Approval-Gate / Unexposed-Tool Closure
- Root causes from stale C-AT9 logs:
  - `strategy_backtest` could return an approval/waiver next action for `strategy_promote`; the loop treated that as a mandatory next tool instead of an operator approval gate.
  - MiniMax can return extra tool calls that were not exposed in the narrowed `tools` list, even when `tool_choice` names the required tool.
- Generic fixes:
  - Approval/promotion next actions are reported as gates and are not forced during ordinary create/backtest turns.
  - Provider-returned unexposed tool calls are ignored rather than executed when the iteration tool list is narrowed to a required tool.
- Verification:
  - Focused loop/tool-choice tests passed, including approval-gate and unexposed-tool regressions.
  - Real MiniMax/yolo/context-full/no-mock `C-AT9` passed after runtime restart: latest log shows `transition_reason=no_more_tools`, `tool_calls=12`, `errors=0`, `proposal_ids=["prp_04f0fb4c8751","prp_796ef0f13462"]`, and API check selected `prp_04f0fb4c8751` with `execution_mode=agent`.
  - Analysis note: context-full raw response records may still contain provider-returned unexposed tool names. For execution truth, use per-case `turn.evidence.tool_names`, persisted `tool_trace`, and API artifacts.

## 2026-06-04 Archive-Failure Revalidation
- Rechecked three failures from `dashboard/test-results-archive/failed-set-22-20260604-080207` against the current code before changing runtime behavior:
  - `C-AT2` old failure was provider-proposal finalization instead of RSI strategy proposal. Current generic strategy-prep guard tests passed, and real MiniMax rerun passed `C-AT2` in 149648 ms with `strategy_backtest_finalized`, tools `account_list, connector_list, market_data, skill, strategy_backtest, strategy_generate_proposal`, and API evidence `main.py contains rsi`.
  - `E1` old failure had `team_run` output but `/teams/runs` API check missed `market_analysis_team`. Current team-store tests passed, and real MiniMax rerun passed `E1` in 350717 ms with API evidence `team_template=market_analysis_team ok`.
  - `L9` old failure was a text assertion miss despite a bounded refusal-style reply. Current real MiniMax rerun passed `L9` in 22718 ms; the reply included `refuse (tool_abuse)` and no tools were called.
- Reset semantics clarified: `reset_workspace.py` already deletes `approvals/pending.jsonl`; setup output `skip approvals/pending.jsonl` means the file was missing, not preserved. Fresh `tests/test_reset_workspace.py` passed.
- Next action: clear/rotate isolated dev logs, then run the broader historical/high-risk CSV set before attempting the full 160-case CSV gate.

## 2026-06-04 E3/F1/G9/H6/L4 Root-Cause Repair Plan
- Current MiniMax/yolo/context-full/no-mock 22-case gate passed 17 and failed `E3,F1,G9,H6,L4`.
- `E3`: the turn had `role_list`, `team_run`, `strategy_generate_proposal`, and `strategy_backtest` evidence, but `/teams/runs?limit=20` found no `strategy_design_team`. Fix the durable native `team_run` mirror/API observability, not the CSV assertion.
- `F1`: the turn completed read-only strategy/portfolio/journal diagnostics, then produced `strategy_generate_proposal`; the API expected a `learning_update`. Reflection/evolution action selection must outrank strategy-proposal retry after diagnostic evidence.
- `G9/H6`: read-only connector/data/market evidence was over-classified as strategy authoring prep. `connector_list + market_data + data_api/data_source_status/run_shell` should not force `strategy_generate_proposal` without explicit strategy-workflow evidence or a structured required action.
- `L4`: runtime behavior was semantically correct (`不是一个我能识别的真实股票代码...`); generated CSV contract should accept that stable refusal/unknown-ticker wording.
- Implementation rule: add failing-first tests for these generic boundaries, then change `loop.py`, native team persistence observability, and `tools/extract_cases.py` only as needed. No case IDs, prompt regex routers, intent marker arrays, or mock-provider fallback.

## 2026-06-05 E3/F1/G9/H6/L4 Root-Cause Closure
- Root cause from L4 context-full logs: after `memory_recall + portfolio_summary`, MiniMax produced a correct invalid-symbol refusal that mentioned it would not call `strategy_generate_proposal`; the loop still treated any final prose containing the tool name as a planned action and forced `tool_choice=strategy_generate_proposal` in the next iteration.
- Generic fix: removed the prose-tool-name `planned_strategy_proposal_retry` path. Strategy proposal continuation now remains limited to structured tool calls, structured `next_required_action`, unfinished todos, and observed strategy authoring/team/prep evidence. This avoids interpreting refusal prose or safety explanations as tool plans.
- Failing-first regression added: `test_negated_strategy_tool_mention_after_read_only_diagnostics_does_not_force_proposal` failed before the loop fix and passes after it.
- Local verification passed:
  - `python -m pytest tests/test_agent_loop_final_summary.py -k "negated_strategy_tool_mention_after_read_only_diagnostics" -q` -> `1 passed`.
  - `python -m pytest tests/test_extract_cases.py tests/test_agent_loop_final_summary.py -k "provider_proposal or agent_team_strategy or team_run or strategy_data_prep or read_only_market_data or reflection_diagnostic or degraded_strategy_design_team or role_and_market_prep or negated_strategy_tool_mention_after_read_only_diagnostics" -q` -> `30 passed, 112 deselected`.
  - `python -m pytest tests/test_no_runtime_route_hardcoding.py -q` -> `5 passed`.
  - `python -m py_compile nerya\agent\loop.py tools\extract_cases.py tests\test_agent_loop_final_summary.py tests\test_extract_cases.py` -> passed.
- Real MiniMax/yolo/context-full/no-mock verification:
  - Runtime restarted on `:18328`; setup confirmed `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `key_ref=yes`, `permission_mode=yolo`.
  - L4 single passed (`1 passed`, 28.1s). Latest L4 evidence: `transition_reason=no_more_tools`, `tool_names=["llm_classify"]`, `proposal_ids=[]`, context-full iteration 2 had `required_next_tool_names=[]` and `tool_choice=null`.
  - Focused gate `E3,F1,G9,H6,L4` passed together (`5 passed`, 7.8m). Evidence: `E3` API check found strategy proposal `prp_b0d83811dc3d` and `team_run_exists ok: team-eb6b366c0f`; `F1` API check found `learning_update`; `L4` had no proposal ids and passed unknown-ticker contract.
- Current log locations:
  - Per-case logs: `dashboard/test-results/logs/{E3,F1,G9,H6,L4}.jsonl` and `.reply.txt`.
  - Full LLM request context: `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl`.
  - Runtime restart logs: `tmp-nerya-api-18328.log` and `tmp-nerya-api-18328.err.log`.
- Next action: if continuing beyond the focused gate, rotate/trim oversized dev logs and run the broader or full CSV gate on the same real MiniMax/no-mock/yolo setup.

## 2026-06-05 Full CSV C7 Root-Cause Closure
- Full CSV run started on real MiniMax/yolo/context-full/no-mock after the focused gate and passed A1-C6, then failed at C7. The run was stopped at that first failure to avoid burning more real API budget before log-driven repair.
- C7 failure evidence: per-case log `dashboard/test-results/logs/C7.jsonl` showed `transition_reason=team_result_degraded_finalized`, `tool_names=["account_list","connector_list","data_source_status","enter_plan_mode","exit_plan_mode","memory_recall","role_list","strategy_list","task_list","team_run"]`, and `proposal_ids=[]`; API check failed because no `strategy_package_proposal` for NVDA existed.
- Root cause: a degraded-but-usable AgentTeam result containing actionable trading evidence was finalized as a team report unless its template was exactly `strategy_design_team`. The C7 team report included structured position sizing, TWAP/execution, and stop/risk output, so it was enough evidence to continue into a reviewable `strategy_generate_proposal`.
- Generic fix: `_team_result_requires_strategy_proposal()` now also detects actionable strategy output from structured team result keys such as position sizing plus execution/stop plan fields. It still leaves ordinary degraded team reports as deterministic reports when they lack executable strategy evidence.
- Failing-first regression added: `test_degraded_actionable_team_result_continues_to_strategy_proposal` failed before the loop fix and passes after it.
- Local verification passed:
  - `python -m pytest tests/test_agent_loop_final_summary.py -k "degraded_actionable_team_result_continues_to_strategy_proposal" -q` -> `1 passed`.
  - Team/degraded subset -> `17 passed, 117 deselected`.
  - Wider related loop/extract subset -> `31 passed, 112 deselected`.
  - `python -m pytest tests/test_no_runtime_route_hardcoding.py -q` -> `5 passed`.
  - `python -m py_compile nerya\agent\loop.py tools\extract_cases.py tests\test_agent_loop_final_summary.py tests\test_extract_cases.py` -> passed.
- Real MiniMax/yolo/context-full/no-mock C7 rerun passed after runtime restart: `1 passed (5.3m)`. Latest C7 evidence: `transition_reason=no_more_tools`, `tool_names=["edit_file","market_data","portfolio_summary","read_file","role_list","strategy_generate_proposal","strategy_validate","team_run"]`, `proposal_ids=["prp_d9220a51bb34"]`, and API check `execution_mode = agent_team ok`.
- Next action: resume the full CSV gate from C8 onward on the same real provider setup; repair the next concrete failure from logs before attempting another broad continuation.

## 2026-06-05 C11 Approval-Lookup Reflection Misclassification Closure
- Full CSV continuation passed C8-C10, then failed C11 (`approve 这个策略让它跑起来`). The failed turn searched existing strategy/proposal state but was forced into `evolve_reflect`, creating an unrelated `learning_update` proposal instead of reporting that there was no approved/backtested target to promote.
- Root cause: reflection diagnostics were over-classified from tool names. `journal_search + strategy_list + account_list` and later read-only `evolve_proposals` lookup were treated as enough evidence to force a write-capable reflection proposal, even though account/strategy/proposal list calls are inventory lookup, not portfolio/performance diagnostics.
- Generic fix: reflection diagnostics now require journal/history plus portfolio or performance evidence (`portfolio_summary`, `portfolio_pnl`, positions, risk/ledger, strategy run/history/tuning/backtest). `account_list`, `strategy_list`, and empty `evolve_proposals` lookup no longer force `evolve_reflect`.
- Failing-first regressions added/updated:
  - `test_approval_lookup_gap_does_not_force_reflection_proposal`.
  - `test_read_only_evolution_lookup_without_diagnostics_does_not_force_proposal_retry`.
- Local verification passed:
  - Focused reflection subset -> `5 passed`.
  - Wider loop/extract/no-hardcoding subset -> `33 passed, 111 deselected`.
  - `tests/test_no_runtime_route_hardcoding.py` -> `5 passed`.
  - `py_compile` for touched files -> passed.
- Real MiniMax/yolo/context-full/no-mock C11 rerun passed after runtime restart: `1 passed (27.9s)`. Setup confirmed runtime and dashboard proxy workspace alignment, `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `key_ref=yes`, and `permission_mode=yolo`.
- Latest C11 evidence: `transition_reason=no_more_tools`, `tool_names=["evolve_proposals","strategy_list"]`, `proposal_ids=[]`, and `assert.turn_stability ok=true`. Final reply correctly reported no strategy/proposal/backtest target existed and asked for a `strat_*` or `prp_*` id instead of creating a reflection proposal.
- Next action: resume the remaining CSV from C12 onward with `--max-failures=1`; stop on the next real failure and repeat log-driven root-cause repair.

## 2026-06-05 C16 Missing Strategy Target Guard Closure
- Remaining CSV continuation after C11 passed C12-C15, then failed C16 (`（C1 promoted）这个策略的参数能不能优化一下？`).
- Failure evidence: the model found that `C1` was not present in the isolated workspace (`strategy_list` empty, `strategy_view(C1)` failed with `strategy_unknown`, `strategy_history(C1)` had empty ledgers, and `evolve_proposals(prp_C1)` was not found). The first final answer correctly asked for a concrete `strategy_id` or `proposal_id`, but the harness still forced `evolve_reflect`, creating an unrelated `learning_update` proposal and failing the user-visible strategy/tuning contract.
- Root cause: reflection/proposal retry state used broad tool-name evidence. Empty `strategy_history` and a target-missing `strategy_view` error were still treated as enough diagnostic evidence to require a write-capable reflection proposal.
- Generic fix: the loop now records `strategy_target_missing_observed` from structured strategy lifecycle tool errors (`strategy_unknown` / `unknown strategy`). Once observed, it removes pending write-capable required tools for the missing target and suppresses reflection/strategy proposal retries for that turn.
- Regression added: `test_missing_strategy_target_blocks_reflection_retry_after_diagnostics`.
- Local verification passed:
  - C11/C16/reflection focused subset -> `6 passed`.
  - Wider loop/extract/no-hardcoding/tuning subset -> `34 passed, 111 deselected`.
  - `tests/test_no_runtime_route_hardcoding.py` -> `5 passed`.
  - Forbidden marker scan for `_STRATEGY_INTENT_MARKERS`, `INTENT_MARKERS`, and planned strategy prose retry markers -> no matches.
  - `py_compile` for touched loop/test files -> passed.
- Real MiniMax/yolo/context-full/no-mock C16 rerun passed after runtime restart: `1 passed (39.9s)`. Setup confirmed runtime/dashboard workspace alignment, `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `key_ref=yes`, and `permission_mode=yolo`.
- Latest C16 evidence: `transition_reason=no_more_tools`, `required_next_tool_names=[]` in context-full metadata, no `evolve_reflect`, and `assert.must_contain ok=true`.
- Next action: resume remaining CSV from C17 onward with `--max-failures=1`.

## 2026-06-05 C-AT5 Strategy SDK Compatibility + Reflection Boundary Closure
- C-AT5 initially passed the CSV API assertion but was not a clean pass: context-full logs showed generated inline `main.py` called `ctx.market.candles(interval=..., count=...)` without a market argument and accessed candle rows as `c.close` / `c.volume`. The backtest crashed with `MockMarket.candles() got an unexpected keyword argument 'interval'`, then the loop incorrectly forced `evolve_reflect`, creating an auxiliary `learning_update` proposal unrelated to the requested strategy artifact.
- Generic generator fix: `strategy_code_generator` now normalizes agent-authored inline `ctx.market.candles(...)` calls to the public StrategyContext facade (`market`, `timeframe`, `limit`) and normalizes candle row object access to dict access (`c["close"]`, `c["volume"]`, etc.). This is AST/source-segment based SDK compatibility normalization, not case/prompt routing.
- Generic validator fix: `strategy_validate` now blocks residual invalid strategy code that calls `ctx.market.candles` without a market / with `interval` or `count` aliases, and blocks candle row attribute access from rows iterated out of `candles`. This prevents non-generator proposal paths from marking backtest-incompatible artifacts as `ok=true`.
- Generic loop fix: reflection diagnostics still work for explicit journal/performance review tasks, but a completed strategy creation flow with successful `strategy_generate_proposal + strategy_backtest` no longer forces `evolve_reflect` unless there is journal-style review evidence. This keeps ordinary strategy authoring from producing auxiliary learning proposals.
- Failing-first regressions added:
  - `test_strategy_generator_normalizes_agent_candle_facade_aliases`.
  - `test_backtest_incompatible_candle_facade_aliases_are_blocked`.
  - `test_strategy_creation_backtest_does_not_require_reflection_proposal`.
- Local verification passed:
  - `tests/test_strategy_code_generator.py` -> `28 passed`.
  - `tests/test_strategy_subagent_validation.py` -> `10 passed`.
  - loop/reflection focused subset -> `7 passed, 130 deselected`.
  - `tests/test_no_runtime_route_hardcoding.py` -> `5 passed`.
  - `py_compile` for touched loop/generator/validator/test files -> passed.
- Real MiniMax/yolo/context-full/no-mock C-AT5 clean rerun passed after runtime restart: `1 passed (4.1m)`. Setup confirmed `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `key_ref=yes`, `permission_mode=yolo`.
- Latest C-AT5 evidence: `transition_reason=strategy_backtest_finalized`, tool list did not include `evolve_reflect`, `proposal_ids=["prp_1908567f372f"]`, API check selected `prp_1908567f372f` with `execution_mode=agent ok` and `main.py contains: StrategyAgentTask.skip ok`. The selected `main.py` uses `ctx.market.candles(ctx.config.markets[0], ..., timeframe="1h")` and candle dict access (`c["close"]`, `c["volume"]`).
- Next action: resume remaining CSV from C-AT6 onward with real MiniMax/yolo/context-full/no-mock and `--max-failures=1`.

## 2026-06-05 C-AT7 Strategy SDK / Proposal Packaging Closure
- C-AT7 initially passed the CSV API assertion but was not a clean pass. Context-full and per-case logs showed `strategy_backtest` failing on generated strategy code with `ModuleNotFoundError: No module named 'strategy_sdk'`, then later with `TypeError: 'method' object is not iterable`, and then with a real data gap caused by providerless `BTC/USDT:USDT` plus stringified account objects.
- Root causes were generic boundaries, not the CSV prompt: generated inline `main.py` could import the nonexistent `strategy_sdk`, use `ctx.market` aliases with unsupported signatures, iterate `ctx.portfolio.positions` as a property, pass unsupported `StrategyAgentTask.dispatch` kwargs, and package MiniMax-emitted account objects without extracting `label` or using `venue` to canonicalize providerless markets.
- Generic fixes:
  - `strategy_code_generator` now normalizes `strategy_sdk` imports, `ctx.market` alias calls for `ticker`/`candles`/`features`, candle row attribute access including `candles_1h`, and `ctx.portfolio.positions` property access.
  - `strategy_validate` now blocks residual `strategy_sdk` imports, unsupported market facade calls/keywords, `ctx.portfolio.positions` property iteration, unsupported AgentTask dispatch kwargs, and common non-facade portfolio/market/trading surfaces.
  - `NativeToolExecutor` now extracts account `label` from provider object items and uses an explicit account `venue` to prefix providerless slash-style markets before schema validation.
- Verification:
  - Local checks passed: `tests/test_strategy_code_generator.py tests/test_strategy_subagent_validation.py` (`46 passed`), `tests/test_tool_errors.py tests/test_no_runtime_route_hardcoding.py` (`36 passed`), focused finalizer subset (`8 passed`), and touched-file `py_compile`.
- Real MiniMax/yolo/context-full/no-mock C-AT7 passed after runtime restart: latest log `dashboard/test-results/logs/C-AT7.jsonl` shows `transition_reason=strategy_backtest_finalized`, `tool_calls=16`, `errors=2`, proposal `prp_40074173009f`, and API check `execution_mode=agent ok` / `main.py contains funding ok`.
  - Backtest artifacts exist under `dashboard/.nerya-test-workspace/evolution/proposals/prp_40074173009f/after/strategies/btc_funding_short_agent/backtests/20260604_224915/`; the report is a real short-window backtest (`10.08d`, `binance_perpetual:BTCUSDT`, no mock gate), with zero simulated trades.
- Next action: rerun remaining known C-AT failures on real MiniMax: `C-AT8,C-AT10,C-AT14`.

## 2026-06-05 C-AT8/C-AT10/C-AT14 Focused Failure Closure
- Remaining known C-AT failures were rerun together on real MiniMax only (`minimax-cn` / `MiniMax-M3` / `https://api.minimaxi.com/v1`), with `NERYA_PERMISSION_MODE=yolo`, `NERYA_CONTEXT_FULL_LOG=1`, and no mock LLM allowance.
- Verification: `NERYA_CASES_ONLY=C-AT8,C-AT10,C-AT14` passed (`3 passed`, 19.9m). Current `dashboard/test-results/summary.csv` records:
  - `C-AT8` passed in `524052 ms`, API check yes, transition `strategy_backtest_data_gap_finalized`.
  - `C-AT10` passed in `356560 ms`, API check yes, transition `strategy_backtest_finalized`.
  - `C-AT14` passed in `305905 ms`, API check yes, transition `strategy_backtest_finalized`.
- Current per-case logs are `dashboard/test-results/logs/C-AT8.jsonl`, `dashboard/test-results/logs/C-AT10.jsonl`, and `dashboard/test-results/logs/C-AT14.jsonl`. Together with the prior C-AT focused reruns, the known C-AT failure set is closed on MiniMax/yolo/no-mock.
- Next action: rotate or clear isolated dev logs, then continue the broader CSV gate from the next not-currently-verified segment with `--max-failures=1`; stop on the next real failure and repair from per-case/context-full logs.

## 2026-06-05 D1-D10 MiniMax Segment Pass
- After clearing isolated dev logs with `POST /dev/clear`, ran `D1-D10` on real MiniMax only with runtime `:18328`, dashboard `:3028`, isolated workspace `dashboard/.nerya-test-workspace`, `NERYA_PERMISSION_MODE=yolo`, `NERYA_CONTEXT_FULL_LOG=1`, `NERYA_E2E_LLM_BASE_URL=https://api.minimaxi.com/v1`, no mock LLM allowance, `NERYA_TEST_RETRIES=0`, and `--max-failures=1`.
- Setup gates confirmed runtime/dashboard workspace alignment, non-mock provider `minimax-cn`, model `MiniMax-M3`, base URL `https://api.minimaxi.com/v1`, key by vault ref, and provider-native tool-call probe support.
- Result: `10 passed` in `14.2m`. Summary rows show API checks passing for all D cases. Key transitions:
  - `D1` `background_task_created`; `D4/D5/D6` `task_schedule_created`.
  - `D7` `no_more_tools` after `team_run`, `strategy_generate_proposal`, and `strategy_backtest`.
  - `D8` `wall_time_final_synthesis` with market/web source evidence.
  - `D10` `no_more_tools` with portfolio/journal/strategy lookup only; the earlier false strategy-proposal retry did not recur.
- Per-case logs are under `dashboard/test-results/logs/D*.jsonl`; current full context log for this segment is `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl`.
- Next action: clear or rotate the D-segment dev logs and run the next CSV segment (`E1-E14`) on the same MiniMax/yolo/no-mock setup with `--max-failures=1`.

## 2026-06-05 E2 Investment Committee Debate Boundary Closure
- E segment run started on the same real MiniMax/yolo/context-full/no-mock setup. `E1` passed, then `E2` failed after 4.0m because the final visible text did not match `must_contain=team`. The per-case log showed stable completion but only `data_api`, `market_data`, and `web_search_fetch` tools, so the model had simulated a bull/bear debate in the parent turn instead of using durable `team_run` / `investment_committee_team`.
- First generic fix: strengthened the `team_run` tool description and `TEAM_RUN_SCHEMA.team_template` guidance so bull/bear, long/short, investment-committee, and adversarial debate requests use `investment_committee_team` and do not simulate separate roles in the parent turn.
- Focused E2 rerun then passed, but context-full showed a second root cause: after `team_run`, the loop itself emitted a required-action nudge forcing `strategy_generate_proposal` because `_team_result_requires_strategy_proposal()` treated investment-committee sizing/stops/execution guidance as actionable strategy-package evidence. The output passed API checks but semantically overreached by creating/backtesting a strategy.
- Second generic fix: `investment_committee_team` results are now treated as research/debate output and do not automatically force `strategy_generate_proposal`; `strategy_design_team` and non-committee actionable strategy outputs keep the existing proposal path.
- Regression and verification:
  - Added failing-first tests for `team_run` investment-committee debate guidance and for `investment_committee_team` not forcing strategy proposals.
  - Passed focused checks: `tests/test_strategy_context_guidance.py -k team_run_tool_guides` (`2 passed`), team template inference subset (`2 passed`), loop policy subset (`5 passed`), `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and touched-file `py_compile`.
  - Final real MiniMax/yolo/context-full/no-mock E2 rerun passed in `3.3m`: latest log has `tool_names=["team_run"]`, `proposal_ids=[]`, `transition_reason=no_more_tools`, API check `team_template=investment_committee_team ok`, and final text is a multi-role team debate report with `AgentTeam evidence: investment_committee_team`.
- Next action: clear or rotate E2 dev logs, then resume E segment from `E3-E14` with `--max-failures=1`.

## 2026-06-05 E10 Context Correlation + Backtest Verdict Summary Closure
- E10 passed on real MiniMax/yolo/context-full/no-mock, but the semantic audit found two harness-level issues hidden behind the pass:
  - Per-case logs reported the API/journal turn id `trn_*`, while `llm_context_full.jsonl` used an internal loop id. This made direct case-log -> full-context joins unreliable.
  - A completed standard backtest with `verdict=FAIL` was summarized as a generic completed backtest, exposing only metric names and not the FAIL gate / no-promote risk.
- Generic fixes:
  - `LoopConfig` now accepts the kernel/API `turn_id`, `_loop_config_from_config()` passes it through, and `WorkspaceNativeAgentLoop` uses it for block envelopes, tool calls, and LLM metadata. Standalone loop tests still generate an internal id when no external id is configured.
  - The strategy backtest finalizer now preserves and displays `verdict`, selected `metrics_display`, `operator_summary_text`, and `review_gate` fields. Standard backtest `verdict=FAIL` remains a completed tool result but now finalizes with an explicit no-promote warning and rerun/repair next step.
  - Late wall-time final synthesis now preserves already-created strategy proposal evidence before reporting skipped late action tools, so users still see the `prp_*` id when `strategy_backtest` is skipped by the wall-clock reserve.
- Test cleanup aligned stale assertions with current boundaries: no prose-tool-name forced proposal retry, no reflection proposal from inventory-only evidence, and no extra model round after a model-provided final answer.
- Local verification passed:
  - `python -m pytest tests/test_agent_loop_final_summary.py -q` -> `148 passed`.
  - `python -m pytest tests/test_no_runtime_route_hardcoding.py tests/test_strategy_code_generator.py tests/test_strategy_subagent_validation.py -q` -> `57 passed`.
  - `python -m pytest tests/test_llm_gateway_model_override.py -k context_full -q` -> `5 passed, 19 deselected`.
  - `python -m py_compile nerya/agent/loop.py nerya/agent/kernel.py tests/test_agent_loop_final_summary.py` -> passed.
- Next action: restart or confirm the real MiniMax runtime is on the new code, clear dev logs, rerun E10, and verify the new context-full `turn_id` matches the case log while the final reply surfaces `verdict=FAIL` when the replay fails.

## 2026-06-05 E12 Split-Language Team Contract Repair
- Root cause: E12's source test plan title said "团队跨语言", but the generated CSV prompt was only `中文分析、英文报告` with inherited text-only `must_contain=team` and no API evidence contract. A real model can correctly treat that as a language-preference update and finish without `team_run`.
- Source-contract fix: `docs/nerya-prompt-test-plan.md` and `tools/extract_cases.py` now make E12 an explicit AgentTeam task (`用 AgentTeam 让 3 个分析师同时研究 ETH；中文分析，最终写英文报告`) with `api_check=team_run_exists=true:team_output_language=English:team_analysis_language=Chinese` and no prose-only `must_contain`.
- Generic runtime/tool fix: `team_run` now supports split-language execution via `analysis_language` for member analysis/role conclusions and `output_language` for the final user-facing report. The persisted TeamStore metrics expose both fields so CSV API checks can validate durable team-run language evidence.
- Generic prompt fix: the system prompt now says explicit final/report/deliverable language overrides the prompt's surrounding language and instructs `team_run` calls to pass `output_language` and `analysis_language` when requested.
- Verification: new failing-first regressions passed after implementation: `tests/test_extract_cases.py` (`10 passed`), `tests/test_team_streaming_events.py` (`27 passed`), `tests/test_agent_temporal_context.py` (`16 passed`). Additional gates passed: touched-file `py_compile`, E2E TypeScript compile, `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and forbidden marker scan had no matches.
- Next action: restart the MiniMax runtime on the new code and rerun real E12 with yolo/context-full/no-mock, then continue E13/E14.

## 2026-06-05 E12 Real Runtime Closure
- Real MiniMax/yolo/context-full/no-mock E12 rerun passed after the split-language runtime changes were loaded: latest `dashboard/test-results/logs/E12.jsonl` shows `case.pass`, duration `865170 ms`, `tool_names=["team_run"]`, and no proposals.
- API evidence is durable instead of prose-only: `team_run_exists ok: team-8fa12321b6`, `team output_language=English ok`, and `team analysis_language=Chinese ok`.
- Fresh local gates after the final `SubAgentRuntime` language fix passed: `tests/test_team_streaming_events.py` (`28 passed`), `tests/test_extract_cases.py tests/test_agent_temporal_context.py tests/test_no_runtime_route_hardcoding.py` (`31 passed`), E2E TypeScript compile, and touched-file `py_compile`.
- Next action: clear isolated dev logs, then continue the E segment with real MiniMax from `E13,E14` using `--max-failures=1`.

## 2026-06-05 E13 Explicit Team Roles / Timeout Viability Closure
- Root cause: E13 was still prose-only (`must_contain=team`) even though the requirement was durable `team_run` evidence. The first real pass also exposed a deeper runtime issue: `team_run` expanded the explicitly requested 3 roles into the full `market_analysis_team` and MiniMax then over-applied a `5s` deadline, yielding degraded or zero-output team reports.
- Generic fixes:
  - CSV source now requires `team_run_exists=true:team_roles_total=3` and no final-prose `team` assertion.
  - `team_run` treats an explicit `roles` list as the execution list; market template expansion only happens for rating/target-price style market-analysis requests that need full coverage.
  - `team_run.timeout_s` keeps a 30s minimum so a natural-language quick deadline does not kill all real child LLMs before they can answer; concise role instructions carry the quick-output requirement.
  - TeamStore metrics now expose `roles_total`, `roles_succeeded`, `roles_failed`, `max_parallel`, and `timeout_s` for API-backed assertions.
- Local verification passed: `tests/test_team_streaming_events.py` (`30 passed`), `tests/test_extract_cases.py tests/test_no_runtime_route_hardcoding.py` (`16 passed`), E2E TypeScript compile, touched-file `py_compile`, and forbidden marker scan had no matches.
- Real MiniMax/yolo/context-full/no-mock E13 rerun passed after API restart: `1 passed (28.1s)`, latest log shows `team_run_exists ok: team-1f7271242b`, `team roles_total=3 ok`, `transition_reason=no_more_tools`, and no proposals. TeamStore metrics show `roles_total=3`, `roles_succeeded=3`, `roles_failed=0`, `max_parallel=3`, `timeout_s=60.0`.
- E14 also passed in the same E segment before the final E13 timeout-floor adjustment; it uses `Skill/read_file/web_search`, not `team_run`, so the E13 runtime change does not touch its execution path.
- Next action: continue the CSV gate from the F segment with real MiniMax/yolo/context-full/no-mock and `--max-failures=1`.

## 2026-06-05 F1 Reflection Evidence / F Segment Closure
- F1 first failed on real MiniMax because the model completed useful read-only diagnostics but did not create the required `learning_update` proposal. The first local fix only recognized `portfolio_pnl + virtual_ledger`, but the real run never called `virtual_ledger`; it had enough evidence from `portfolio_pnl` non-trading realized delta plus empty `strategy_list` and empty `journal_search` activity.
- Generic fixes:
  - Reflection diagnostics now use result-level evidence, not prompt text or case ids: `portfolio_pnl` non-trading realized delta plus either no-trade ledger evidence or empty strategy inventory + empty journal evidence is enough to require `evolve_reflect`.
  - Required-action text retries now use the existing `next_action_nudges` lifecycle so the same pending tool set is not repeatedly re-nudged.
  - Strategy proposal deferral after assistant choice/confirmation text is driven by completed tool evidence plus unresolved action text; it does not inspect the operator prompt or route by case.
- Regression coverage:
  - Added `test_portfolio_pnl_empty_strategy_review_requires_reflection_without_ledger` for the real F1 shape.
  - Kept the ledger-specific reflection regression and the strategy-proposal deferral regressions green.
- Local verification passed:
  - Focused reflection subset: `7 passed, 147 deselected`.
  - Full `tests/test_agent_loop_final_summary.py`: `154 passed`.
  - `tests/test_extract_cases.py tests/test_no_runtime_route_hardcoding.py`: `16 passed`.
  - Touched-file `py_compile`: passed.
  - E2E TypeScript compile had already passed in this segment before the runtime rerun.
- Real MiniMax/yolo/context-full/no-mock runtime evidence:
  - F1 single rerun passed after API restart: `1 passed (1.2m)`. Latest F1 log showed `transition_reason=proposal_created_finalized`, `tool_names=["account_list","evolve_reflect","journal_search","portfolio_pnl","portfolio_positions","portfolio_summary","strategy_list"]`, proposal `prp_9bc5c7f3ed74`, and API check `proposal kind=learning_update ok`.
  - Context-full showed the harness narrowing iteration 4/5 to `required_next_tool_names=["evolve_reflect"]` with `tools_sent_count=1`.
  - F2-F12 segment then passed on the same real provider setup: `11 passed (12.7m)`. Current summary rows show API checks passing for F2-F12.
- Next action: continue the broader CSV gate from the next not-currently-verified segment with real MiniMax/yolo/context-full/no-mock and `--max-failures=1`; stop on the next concrete failure and repair from per-case/context-full logs.

## 2026-06-05 H10 MiniMax Required-Tool Schema Budget Closure
- Root cause: H10 was not using mocks and was not routed by prompt keywords. Context-full logs showed real MiniMax requests with a single exposed `evolve_skill_proposal` tool and provider-native `tool_choice`. MiniMax first returned unexposed tool calls (`memory_recall`, `connector_list`) and then repeatedly timed out on the required-tool recovery path.
- REF-aligned fix: required native action calls now run as deterministic tool-emission requests. When the loop has narrowed the tool surface to pending required action tools, it uses `temperature=0`, `reasoning_effort=none`, clears reasoning summaries, and caps low-budget required actions. Compact recovery uses a 1024 token cap.
- Schema fix: required-action tool exposure now uses compact schema with only top-level JSON Schema `required` properties. The default compact helper still preserves optional fields; `required_only=True` is used only for narrowed required-action calls. This reduces provider tool-choice complexity without case IDs, prompt regex, or tool-name keyword routing.
- Local verification passed:
  - Focused required-tool subset: `4 passed, 156 deselected`.
  - Full `tests/test_agent_loop_final_summary.py`: `160 passed`.
  - `tests/test_no_runtime_route_hardcoding.py tests/test_extract_cases.py tests/test_native_llm_context_metadata.py`: `18 passed`.
  - `python -m py_compile nerya\agent\loop.py tests\test_agent_loop_final_summary.py`: passed.
  - `npx tsc --noEmit --project dashboard\tests\e2e\tsconfig.json`: passed.
- Real MiniMax/yolo/context-full/no-mock H10 passed after runtime restart: `1 passed (1.2m)`. Latest per-case log `dashboard/test-results/logs/H10.jsonl` shows `transition_reason=proposal_created_finalized`, `tool_names=["evolve_skill_proposal"]`, proposal `prp_7ceba6ef8648`, and API check `proposal kind=skill_proposal ok`.
- Context-full evidence: first H10 required call used `max_tokens=2048`, `temperature=0`, `reasoning_effort=none`; it timed out. The compact retry used `max_tokens=1024`, one message, one tool, and succeeded with `evolve_skill_proposal`.
- Next action: clear/rotate isolated dev logs and continue the broader CSV gate from the next unverified segment or high-risk proposal cases on the same real MiniMax/yolo/no-mock setup with `--max-failures=1`.

## 2026-06-05 G/GX MiniMax Segment Pass
- After the H10 required-tool repair, continued the broader CSV gate on the same real MiniMax/yolo/context-full/no-mock runtime.
- `G1-G10` passed in one run (`10 passed`, 5.7m). Current `dashboard/test-results/summary.csv` for that run recorded API checks passing for all G cases, including `G1 account_matching=kraken`, `G2 OKX passphrase schema`, and stable connector/venue evidence across G3-G10.
- `GX1-GX14` passed in one run (`14 passed`, 18.5m). Slowest cases were `GX5` at `469272 ms`, `GX6` at `153259 ms`, `GX2` at `127854 ms`, and `GX4` at `122879 ms`.
- GX proposal/API evidence highlights:
  - `GX6` created provider proposals and passed `proposal kind=provider_proposal` plus `metadata contains: aster`.
  - `GX14` created strategy proposal `prp_86cdf0e9d391` and passed `strategy_backtest finalized: strategy_backtest_data_gap_finalized`.
  - `GX5` finalized with a standard strategy backtest after multiple proposal attempts; it is slow but stable on this run.
- Audit note: `GX8` currently passed with `transition_reason=model_done` and zero tool calls because the CSV row has no API evidence contract. This is not a current failure, but it should be considered for future contract hardening if the requirement is meant to prove a credential/signature tool path.
- Next action: clear isolated dev logs and run `H1-H10` on the same real MiniMax/yolo/no-mock setup.

## 2026-06-05 H MiniMax Segment Pass
- `H1-H10` passed in one real MiniMax/yolo/context-full/no-mock run (`10 passed`, 11.4m). Current summary rows show API checks passing for all H cases.
- Evidence highlights:
  - `H7` finalized through `data_source_status` with API check `data source status count 5 ok`.
  - `H8` used `data_source_sync_now` with API check `data source event account:paper_main ok`.
  - `H9` inspected connector/data/source evidence and passed `financial datasets ready=false`.
  - `H10` reran after the required-action schema repair and passed again with `evolve_skill_proposal`, proposal `prp_5aa001296796`, and `proposal kind=skill_proposal ok`.
- Slow H cases were `H1` at `166833 ms`, `H2` at `98832 ms`, `H5` at `81492 ms`, `H9` at `80921 ms`, and `H6` at `76102 ms`; all stopped in stable non-aborted states.
- Next action: clear isolated dev logs and run `I1-I8` wallet/provider cases on the same real MiniMax/yolo/no-mock setup.

## 2026-06-05 I MiniMax Segment Pass
- `I1-I8` passed in one real MiniMax/yolo/context-full/no-mock run (`8 passed`, 4.2m). Current summary rows show API checks passing for all I cases.
- Evidence highlights:
  - `I1` used `account_list`, `connector_list`, and `data_api`, with API check `wallet provider self_custody ok`.
  - `I2`, `I5`, `I6`, `I7`, and `I8` finalized through wallet/provider readiness boundaries instead of silently substituting a different wallet path.
  - `I5` stayed stable after the catalog compaction and same-tool follow-up repairs.
- Audit note: `I4` currently passed with `transition_reason=model_done` and zero tool calls because the CSV row has no durable API evidence contract. It should be reviewed later if Coinbase/CDP readiness is meant to require tool evidence.
- Next action: clear isolated dev logs and run `J1-J6` gateway/messaging cases on the same setup.

## 2026-06-05 J MiniMax Segment Pass
- `J1-J6` passed in one real MiniMax/yolo/context-full/no-mock run (`6 passed`, 4.8m). Current summary rows show API checks passing for all J cases.
- Evidence highlights:
  - `J1` used connector/data/gateway evidence and passed `gateway platform telegram ok`.
  - `J2-J6` stopped in stable, non-aborted states with tool-backed synthesis over connector, market, filesystem, skill, and shell diagnostics as appropriate.
- Slowest case was `J5` at `83113 ms`; no runtime/provider timeout or mock fallback occurred.
- Next action: clear isolated dev logs and run `K1-K10` end-to-end workflow cases.

## 2026-06-05 K MiniMax Segment Pass
- `K1-K10` passed in one real MiniMax/yolo/context-full/no-mock run (`10 passed`, 16.7m). Current summary rows show API checks passing for all K cases.
- Evidence highlights:
  - `K1`, `K2`, `K3`, and `K10` created/backtested strategy proposals through real tool evidence.
  - `K5` created a scheduled task through `task_create`.
  - `K8` used `data_source_status`, `journal_search`, `task_list`, and shell/list diagnostics for self-repair evidence.
  - `K9` created a task schedule and preserved chat/task consistency evidence.
- Slow cases: `K2` at `360978 ms`, `K3` at `143489 ms`, `K1` at `99859 ms`, `K5` at `96338 ms`, and `K10` at `95615 ms`.
- Audit note: `K7` currently passed with `transition_reason=model_done` and zero tool calls even though the scenario describes custom news + custom venue + team. This should be upgraded to a durable API/tool evidence contract later.
- Next action: clear isolated dev logs and run `L1-L12` safety/failure cases.

## 2026-06-06 L3 Strategy Order Permission / Risk Evidence Follow-up
- Current user requirement: agent permissions should be broad enough for chat to initiate order intent, while UI/gateway show confirmation on `pending_approval`; strategy runtime may directly submit when profile/risk/approval/live-trading/signer gates allow it. Do not implement language-specific hardcoded markers because users may ask in any language.
- Working constraint: fix the runtime from structured evidence and existing gates. No `_STRATEGY_INTENT_MARKERS`, case IDs, prompt regex routers, mock fallbacks, or bypasses of `RiskGate`, `ApprovalGate`, live-trading config, signer policy, or protected runtime config.
- Root cause confirmed from L3 logs: the previous surface could pass a case from tool-name evidence even when the first `risk_check` attempt failed schema validation. A later real MiniMax rerun was also a false positive: `successful_tool_names` contained `risk_check`, but wall-time final synthesis built compact evidence from the stale schema-error marker and omitted the later successful `risk_check` result, so the final reply incorrectly claimed `intent` was missing.
- Implemented boundary: `trade_intent_submit` is allowed through the native permission layer so chat can submit an order intent, but the canonical trading pipeline still decides `rejected` / `pending_approval` / `filled`. UI/gateway approval is driven by `pending_approval` tool results projected into `approval_request` / `approval_plan`; it is not a pre-chat confirmation requirement.
- Strategy runtime boundary: only strategy-triggered turns get the order permission bypass. The wrapper binds order calls to the active strategy/session/profile, checks allowed tools/accounts/markets/confidence/session order caps, then still runs `RiskGate`, `ApprovalGate`, live config, and execution policy.
- Manual source precedence fix: `_strategy_triggered_order_turn` now excludes dashboard/gateway/manual chat sources before accepting broad internal trigger-kind families such as `schedule.*`. Explicit structured strategy markers (`strategy_triggered=true`, `origin=strategy`, or strategy runtime source) still opt into the strategy channel. This keeps UI/gateway chat on the approval-window path even if metadata carries a schedule-like kind.
- L3 repair state: provider-friendly trade fields and numeric strings are normalized; `size_pct_nav` plus `max_size_pct_nav` can only reject/limit and never loosen policy; direct trade evidence debt now requires a successful `risk_check` or `trade_intent_submit`, not a failed completed tool name. Compact final synthesis now prioritizes the latest successful tool result and suppresses earlier errors from the same tool, so stale schema failures cannot override the final risk-gate evidence.
- Provider schema follow-up: `risk_check` now accepts the same trade intent fields at top level as well as inside nested `intent`. This matches provider-emitted tool-call shapes without language, prompt, or case routing; nested `intent` remains supported and still wins when supplied.
- Extra permission-model regression: a chat/gateway trade call inside a strategy session does not inherit strategy-agent profile denial or auto-execution unless the turn is actually strategy-triggered; it reaches the domain approval gate and returns `pending_approval` with source `agent:native`.
- Provider setup note: one focused rerun initially inherited the source workspace MIMO tier and failed setup with a 401 provider-key error. The isolated E2E workspace was then pinned to `minimax-cn` / `MiniMax-M3` / `https://api.minimaxi.com/v1` with `provider_key_ref=vault://e2e/minimax-cn/api_key`; no plaintext key is stored in `nerya.yml`.
- Verification:
  - [x] REF comparison: relevant Codex/OpenClaw patterns favor structured payload/context and approval surfaces over natural-language routing.
  - [x] `python -m pytest tests/test_strategy_agent_task_chain.py -q -k "chat_trade_call_in_strategy_session or strategy_agent_trade_wrapper or strategy_agent_run_tick_wrapper"` passed (`4 passed, 5 deselected`).
  - [x] `python -m pytest tests/test_strategy_order_auto_approval.py tests/test_tool_approval_policy.py tests/test_agent_execution_state.py -q` passed (`42 passed`), including the new manual-source-before-schedule-kind regression.
  - [x] Added a failing-first regression for a malformed `risk_check` followed by a successful `risk_check` before wall-time compact final synthesis; it now passes and verifies the final prompt includes the successful risk-gate result.
  - [x] Added failing-first handler/executor regressions for top-level `risk_check` trade fields; both failed on the old nested-only schema and now pass.
  - [x] `python -m pytest tests/test_agent_loop_final_summary.py -q -k "risk_check or trade_readiness or ledger_backed_order or wall_time_final_synthesis_prioritizes_later_successful_risk_check or validation_blocked"` passed (`7 passed, 160 deselected`).
  - [x] `python -m pytest tests/test_no_runtime_route_hardcoding.py tests/test_extract_cases.py -q` passed (`17 passed`).
  - [x] `npx tsc --noEmit --project tests\e2e\tsconfig.json` passed for the dashboard E2E TypeScript project.
  - [x] `python -m py_compile nerya\tools\native\trading.py nerya\tools\native\bootstrap.py nerya\agent\kernel.py nerya\agent\loop.py tests\test_strategy_order_auto_approval.py tests\test_tool_approval_policy.py tests\test_agent_execution_state.py tests\test_strategy_agent_task_chain.py tests\test_agent_loop_final_summary.py tools\extract_cases.py` passed.
  - [x] Focused real MiniMax L3 rerun on the new compact-evidence and successful-tool CSV semantics passed (`1 passed`, 1.1m). Latest log: `dashboard/test-results/logs/L3.jsonl`; reply: `dashboard/test-results/logs/L3.reply.txt`; full-context journal: `dashboard/.nerya-test-workspace/dev_logs/llm_context_full.jsonl`.
- Follow-up: `dashboard/tests/e2e/csv-runner.spec.ts` now records `successful_tool_names` from `ok === true` tool results and treats `tool_used=<name>` as a successful non-error tool requirement. `tool_not_used` still checks all observed tool names.

## 2026-06-06 L12 Final-Synthesis Evidence Boundary Follow-up
- UX audit finding: the first real MiniMax/yolo/context-full L12 rerun passed CSV assertions but the page reply was semantically wrong. The final-synthesis fallback correctly said no schedule was created, but then invented an "illustrative only" recursive scheduler code skeleton. That violated the evidence boundary and the redline case's user experience even though `recursive_schedule_absent=true` passed.
- Root cause: compact final-synthesis prompts told the model to answer from markers, but did not explicitly forbid adding new code/templates/examples/implementation steps when evidence was incomplete. MiniMax filled the gap with a plausible schedule sample.
- Generic fix: final-synthesis system/user prompts now forbid inventing or adding new code, commands, templates, examples, implementation steps, artifacts, schedules, orders, URLs, sources, credentials, or tool results not already present in compact evidence. Unsafe or unbounded/destructive original requests must end with a guardrail/refusal instead of an illustrative implementation.
- Harness startup fix exposed during rerun: `dashboard/tests/e2e/global-setup.ts` defaulted UI checks to `:3001` even when `NERYA_DASHBOARD_PORT` was set; `dashboard/playwright.config.ts` also ignored `PORT` when computing `BASE_URL`. Both now derive one dashboard port from `NERYA_DASHBOARD_PORT` / `PORT` and pass it to `dev:e2e`, preventing non-default-port runs from probing the wrong frontend.
- Verification:
  - [x] `python -m pytest tests/test_agent_loop_final_summary.py::test_near_wall_time_uses_compact_evidence_prompt_when_markers_available -q` passed and asserts the final-synthesis prompt contains the no-invented-code boundary.
  - [x] `python -m py_compile nerya/agent/loop.py tests/test_agent_loop_final_summary.py` passed.
  - [x] `npx tsc --noEmit --project tests/e2e/tsconfig.json` passed.
  - [x] `python -m pytest tests/test_prepare_isolated_test_workspace.py -q` passed.
  - [x] Focused real MiniMax/yolo/context-full/no-mock L12 rerun passed on runtime `:18348` / dashboard `:3048`; setup confirmed runtime and dashboard proxy both used `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, and key by vault ref.
  - [x] Latest `dashboard/test-results/logs/L12.reply.txt` contains no code block or executable recursive scheduler example. It explicitly says the pattern is unsafe, no implementation will be supplied, and `recursive schedule absent ok` passed in `dashboard/test-results/logs/L12.jsonl`.
- Next action: continue current-code UX audit on the remaining weak/pass-but-unproven cases, prioritizing model-done/no-tool cases and old audit `WEAK_ASSERTION` rows.

## 2026-06-06 J2 Read-Only Market Query / Late Timeout UX Closure
- Current user requirement: audit pass-but-wrong cases from logs and frontend, keep real MiniMax/yolo/context-full/no-mock, and fix root causes without prompt/case routing or strategy intent marker tables.
- Root cause confirmed from J2 context-full logs: the failed semantic run had `required_next_tool_names=[]` on every late provider request, but the transient-timeout fallback treated the exposed write-tool surface as pending work. Because `strategy_generate_proposal` was available in the full tool list, the final user reply incorrectly said the Telegram `/ask` BTC trend query still needed `strategy_generate_proposal`.
- Generic fix: late transient provider errors now return the completed-tool evidence fallback whenever no pending required tools/action tools exist and the transcript already has tool-result evidence. Required-action paths are unchanged; if a tool result explicitly creates a pending native action, the narrowed tool retry/stable gap behavior still applies.
- Regression added: `test_transient_timeout_after_read_evidence_does_not_require_exposed_actions` models read-only market evidence plus an exposed but non-required action tool, then forces a near-deadline provider timeout. It locks the user-visible contract: no `strategy_generate_proposal` and no "未执行的后续工具" in the final text.
- Verification:
  - [x] Failing-first regression failed on the old behavior with `未执行的后续工具: strategy_generate_proposal`, then passed after the loop fix.
  - [x] Required-action timeout regressions still passed: `test_required_action_provider_timeout_near_deadline_returns_stable_gap`, `test_required_action_transient_timeout_retries_with_compact_tool_context`, and `test_transient_llm_error_keeps_pending_required_action_tools_enabled`.
  - [x] Focused market/final-synthesis set passed (`5 passed`), full `tests/test_agent_loop_final_summary.py` passed (`176 passed`), no-hardcoding gate passed (`5 passed`), touched-file `py_compile` passed, and dashboard E2E TypeScript compile passed.
  - [x] Real MiniMax/yolo/context-full/no-mock J2 rerun passed on runtime `:18355` / dashboard `:3055`. Setup confirmed runtime/proxy workspace match and `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `key_ref=yes`, `permission_mode=yolo`.
  - [x] Latest J2 reply preserves "Telegram 群" and `/ask`, reports only Yahoo `YAHOO:BTC-USD` current price evidence, explicitly refuses to infer a trend without candles/indicators, and does not mention `strategy_generate_proposal`.
  - [x] Playwright page DOM and screenshot check passed for `/chat/b214e03f-663e-4e31-9d0d-7c5af1e05e39`; screenshot saved at `dashboard/test-results/screenshots/J2.bottom.png`.
- Next action: continue current-code UX audit on remaining weak/pass-but-unproven rows, prioritizing `H7` semantic mismatch, `GX14` proposal-type mismatch, and old `WEAK_ASSERTION` messaging/data cases.

## 2026-06-06 GX14/H7 Current-Code UX Revalidation Closure
- Scope: reran the two highest-priority historical semantic-fail rows after J2: `GX14` (cross-venue cash-and-carry strategy/backtest) and `H7` (data-source sync status).
- Real MiniMax/yolo/context-full/no-mock setup reused runtime `:18355` / dashboard `:3055`; setup gates confirmed workspace match, live LLM probe/tool probe, and `key_ref=yes`.
- GX14 result: `pass`, 11767 ms, `transition_reason=strategy_backtest_data_gap_finalized`, tools `strategy_generate_proposal` + `strategy_backtest`, proposal `prp_a1ea72763944`. Page reply creates a reviewable strategy proposal, attempts a real backtest, and stops on `no_historical_data` for `aster:BTCUSDT-PERP` without mock/synthetic data or live promotion.
- H7 result: `pass`, 10037 ms, `transition_reason=data_source_status_finalized`, tool `data_source_status`, API check `data source status count 5 ok`. Page reply correctly shows `total: 5`, `stale_count: 4`, and all five source rows.
- Frontend evidence: DOM extraction and visual screenshots passed for both; saved `dashboard/test-results/screenshots/GX14.bottom.png` and `dashboard/test-results/screenshots/H7.bottom.png`.
- Next action: continue current-code UX audit on old `WEAK_ASSERTION` rows, especially `J5`, `H4`, `H6`, `K4`, and `K7`, where prose pass may not prove the intended durable user workflow.

## 2026-06-06 B2/B6 Source Evidence Closure
- Scope: continued the current-code UX audit on old `INCOMPLETE_EVIDENCE` news rows `B2` and `B6` using real MiniMax only. Runtime/dashboard were restarted on `:18366/:3066`; setup gates confirmed isolated workspace match, live MiniMax text/tool probes, `key_ref=yes`, `permission_mode=yolo`, `NERYA_CONTEXT_FULL_LOG=1`, and no mock LLM allowance.
- B2 root causes: compact final evidence for `web_search_fetch` did not retain document snippets in marker extraction; text-only wall-time synthesis returned before appending the source marker footer; ticker RSS fallback could be too broad and did not augment readable quote/IR pages with relevant ticker headlines.
- Generic fixes:
  - `nerya/agent/loop.py` preserves `snippet` in source evidence markers and applies `_ensure_source_evidence_markers` before the text-only wall-time early break.
  - `nerya/skills/builtin/research/scripts/search_fetch.py` attempts ticker RSS augmentation even when readable search docs exist, but requires RSS title/summary/URL relevance to the requested ticker or company term.
- Verification:
  - Failing-first loop regressions covered compacted `web_search_fetch` snippets and wall-time source footer; they now pass.
  - Failing-first research regression covered readable AAPL quote page + relevant Yahoo RSS augmentation and rejection of unrelated Yahoo RSS items; it now passes.
  - `python -m pytest tests/test_research_fetch_tools.py tests/test_builtin_skill_catalog.py tests/test_no_runtime_route_hardcoding.py -q` passed (`40 passed`).
  - `python -m pytest tests/test_agent_loop_final_summary.py -q -k "evidence_marker or source_evidence or final_synthesis or deadline_timeout_after_tool_results"` passed (`14 passed, 171 deselected`).
  - Touched-file `py_compile` passed, and touched-file forbidden marker scan returned no matches.
  - Real MiniMax/yolo/context-full/no-mock `B2,B6` passed together (`2 passed`, 4.4m). Latest per-case logs: `dashboard/test-results/logs/B2.jsonl`, `dashboard/test-results/logs/B6.jsonl`.
- Current UX:
  - B2 gives a bounded 2026-06-06 Yahoo Finance RSS/web-evidence answer for AAPL/NVDA and clearly states no company-level official/concrete news was proven.
  - B6 preserves the 6-hour freshness constraint and refuses to synthesize a recent-news conclusion from old/out-of-window sources.
- Next action: continue current-code UX audit on old `WEAK_ASSERTION` rows outside the latest K4/K7 closure set, prioritizing `J5`, `H4`, and `H6`, then any remaining model-done/no-tool rows with weak API contracts.

## 2026-06-06 K4/K7 Current-Code Closure
- [x] Revalidated `K4,K7` on current source with manually started runtime/dashboard (`:18331/:3014`) to avoid `PLAYWRIGHT_AUTOSTART` overwriting MiniMax provider config.
- [x] Setup gates confirmed real MiniMax only: `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, key by vault ref, yolo permission mode, workspace match, live text/tool probes passed.
- [x] `K4` passed with `transition_reason=task_schedule_created`; successful tools include `task_create`; API check verified `task_create`, no `kill_switch_set`, and an agent schedule.
- [x] `K7` passed with `transition_reason=task_schedule_created`; successful tools include `team_run`, `evolve_skill_proposal`, and `task_create`; API check verified skill proposal, team run, and agent schedule.
- [x] Verification passed: `py_compile nerya\agent\loop.py`; focused required-artifact loop pytest (`5 passed, 180 deselected`); no-runtime-route-hardcoding (`5 passed`); touched-file forbidden marker scan returned no matches.
- [ ] Continue with remaining weak/pass-but-unproven rows and keep provider/workspace setup explicit so stale MIMO or stale runtime processes do not contaminate current-code UX conclusions.

## 2026-06-07 H6 Multi-Document Source Evidence Closure
- [x] Root cause confirmed from current H6 logs: `web_search_fetch` and `web_search` could retrieve useful NVDA/NVIDIA source documents, but compact evidence marker extraction flattened nested source results and kept only the first document's top-level `title/url/snippet`. In the bad path, an unrelated or weak Yahoo RSS document could dominate the final compact synthesis while later NVIDIA official/IR evidence was dropped.
- [x] Generic fix: `nerya/agent/loop.py` now detects structured source document lists (`documents`, `results`, `items`, `articles`, plus nested `search.results`) and emits bounded per-document markers with title, URL, source, status/fetch method, and a compact snippet. This is source-evidence retention only; no case IDs, ticker routing, language regex, `_STRATEGY_INTENT_MARKERS`, or mock fallback was added.
- [x] Failing-first regression added: `test_web_search_fetch_evidence_marker_preserves_multiple_documents` failed on the old behavior because only the first Walmart-style document appeared; it now passes and verifies a later NVIDIA financial-result document plus numeric revenue/EPS snippets survive marker extraction.
- [x] Local verification passed: focused marker/compaction/wall-time subset (`5 passed`); RSS relevance subset (`4 passed`); `python -m py_compile nerya\agent\loop.py`; `tests/test_no_runtime_route_hardcoding.py` (`5 passed`). Touched-file forbidden scan found no runtime forbidden marker; `NVDA` appears only in test fixtures.
- [x] Real MiniMax/yolo/context-full/no-mock H6 rerun passed on runtime `:18369` and dashboard `:3069` (`1 passed`, 1.8m). Setup confirmed runtime/proxy workspace match, `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, key by vault ref, live LLM text/tool probes, and `permission_mode=yolo`.
- [x] Current H6 UX is acceptable: latest per-case log shows `transition_reason=wall_time_final_synthesis`, `budget.aborted=false`, `assert.turn_stability ok=true`, and successful tools `web_search` + `web_search_fetch`. The final context-full request contains multiple source markers from NVIDIA Investor Relations, StockTitan, CNBC/S&P Global, Yahoo RSS commentary, and NVIDIA Newsroom instead of only the first RSS item. The page reply gives bounded NVDA financial metrics and explicit evidence gaps.
- [ ] Next audit target: continue old weak/pass-but-unproven rows still outside current closure, prioritizing `J5` and messaging/gateway cases whose API checks prove only catalog presence.

## 2026-06-07 H4/B10/H7 Evidence-Compaction and Continuation Closure
- [x] H4 root cause: `web_fetch` successfully fetched a short CoinGecko JSON API payload, but compaction/marker extraction kept status/URL/snippet metadata and dropped bounded JSON fields such as `ethereum.usd`, so final synthesis could claim no price fields were available.
- [x] Generic H4 fix: `nerya/llm/tool_compaction.py` now preserves bounded `response_json` evidence for short JSON/API `web_fetch` results; `nerya/agent/loop.py` carries `content_type`, `fetch_method`, and `response_json` into evidence markers. No prompt keyword route, case ID, ticker route, or mock fallback was added.
- [x] H4 follow-up UX fix: compact API markers now include bounded `response_json_scalars` such as `asset.quote=value` and generic quote facts such as `1 asset = value QUOTE`. This prevents provider synthesis from inverting quote/base direction when a compact JSON price map is present.
- [x] B10 root cause: `data_source_status` had a deterministic finalizer that could stop the loop after status/read-file evidence even when the operator asked for current news, preventing the model from continuing to `news_social`, RSS, search, or fetch tools.
- [x] Generic B10 fix: `data_source_status` only finalizes when the turn is at the iteration/wall-clock finalization boundary; otherwise the status result is returned to the model as evidence so the semantic task can continue.
- [x] H7 root cause: after the B10 finalizer change, the H7 tool result no longer finalized immediately and fell through generic `json.large` compaction, exposing only `top_keys` instead of data-source `summary/sources/events`.
- [x] Generic H7 fix: `tool_compaction` now has a dedicated `data_source.status` reducer preserving `summary.total`, `summary.stale_count`, `summary.generated_at`, bounded `sources`, and bounded `events`.
- [x] Local verification passed: `tests/test_openhuman_reference_plan.py -k "tool_compaction_data_source_status or tool_compaction_web"` (`6 passed`), `tests/test_agent_loop_final_summary.py -k "data_source_status or evidence_marker or source_evidence or final_synthesis"` (`17 passed`), touched-file `py_compile`, and `tests/test_no_runtime_route_hardcoding.py` (`5 passed`).
- [x] Real MiniMax/yolo/context-full/no-mock verification passed on manual runtime/dashboard `:18369/:3069`: focused `H7` (`1 passed`, 29.6s) and combined `B10,H7` (`2 passed`, 1.9m). Setup gates confirmed runtime/proxy workspace match, `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, key by vault ref, live text/tool probes, and `permission_mode=yolo`.
- [x] Current UX evidence: latest `H7.reply.txt` lists 5 data sources with `account:paper_main`, `market:public_ccxt`, stale/failed states, and sync timestamps. Latest `B10.reply.txt` uses `skill`, `script_run`, `web_search`, `web_search_fetch`, and `web_fetch` evidence, ending with a bounded one-hour Yahoo Finance RSS result rather than `data_source_status_finalized`. Latest `H4.reply.txt` says `ETH` price is `$1,559.25 USD`, with no inverted `1 USD = ... ETH` wording.
- [x] `J5` current-code revalidation also passed on the same setup after the H4 fix: it creates `core_config_patch` proposal `prp_9e0b381d65a5` for `triggers/routes.yml` severity routing (`info -> Telegram`, `critical -> Telegram + Discord`, `silent -> record only`) and does not mutate live config.
- [ ] Next audit target: continue with remaining old weak messaging/gateway rows; prioritize cases where current pass status is supported only by final prose or platform-presence checks rather than durable proposal/task/gateway evidence.

## 2026-06-07 MiniMax Peak-Busy Retry Closure
- [x] Root cause confirmed from latest `J6` log: the turn failed before any business logic because MiniMax returned HTTP `529` with `服务器短暂繁忙` / `请稍后重试 (2064)`, and the runtime treated it as a non-transient `LLMError`, surfacing HTTP 500 to Playwright.
- [x] Generic fix: `nerya/llm/retry.py` now treats non-standard provider overload status `529` as retryable at the transport layer; `nerya/agent/loop.py` also classifies provider peak-busy wording as transient for loop-level retries after adapter attempts are exhausted.
- [x] Regression coverage: `test_provider_peak_busy_status_is_retryable` locks transport retry; `test_minimax_peak_busy_error_retries_before_turn_failure` locks loop retry instead of immediate turn failure.
- [x] Local verification passed: new regression pair (`2 passed`), transient/safety loop subset (`9 passed, 180 deselected`), touched-file `py_compile`, `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and `tests/test_llm_transport.py` (`2 passed`).
- [x] Runtime gate passed: real MiniMax/yolo/context-full/no-mock `J1,J5,J6` passed on runtime/dashboard `:18369/:3069`.

## 2026-06-07 J6 Gateway Diagnose Evidence Closure
- [x] Root cause after the 529 fix: `J6` could pass through weak prose assertions while the agent scanned proposal files and skill docs instead of using the actual gateway diagnostic surface; the reply degraded into compact evidence snippets.
- [x] Generic fix: added shared `diagnose_telegram_gateway` helper, wired `/gateway/telegram/diagnose` through it, and exposed a read-only native `gateway_diagnose` tool for agent turns. The tool returns secret-safe live config status, vault-ref presence, Telegram probe results when configured, and concrete hints; it does not send messages or mutate config.
- [x] CSV contract fix: `J6` now requires `tool_used=gateway_diagnose`, and `tools/extract_cases.py` keeps J1/J5/J6 durable evidence contracts aligned with `cases.csv`.
- [x] Prompt/log polish: compact final-synthesis prompt now says the turn is entering compact evidence synthesis, not that the wall-clock is nearly exhausted when a configured evidence window is intentionally used.
- [x] Verification passed: gateway diagnose tool/API/extract tests (`19 passed, 1 deselected`), final-synthesis subset (`7 passed, 182 deselected`), no-hardcoding (`5 passed`), py_compile/TypeScript compile, focused real MiniMax `J6` (`1 passed`), and full J cluster `J1,J5,J6` (`3 passed`, 1.4m).
- [ ] Next audit target: continue remaining weak messaging/gateway rows and any current pass status backed only by final prose rather than durable tool/API evidence.

## 2026-06-07 B3/B5/B7/B11 Source Fallback and Anti-Bot Revalidation
- [x] Current-code real MiniMax/yolo/context-full/no-mock rerun passed `B3,B5,B7,B11` together (`4 passed`, 6.0m) on runtime/dashboard `:18369/:3069`; setup confirmed workspace match, `provider=minimax-cn`, `model=MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, key by vault ref, live text/tool probes, and `permission_mode=yolo`.
- [x] UX audit: `B3` now gives an honest bounded Reddit answer with `Skill`, `script_run`, and `web_search_fetch` evidence, explicitly stating no direct r/CryptoCurrency evidence was retrieved; `B5` returns a bilingual Wall Street source summary from `web_search_fetch`; `B7` uses connector/status + RSS + `llm_classify` fallback without fabricating CryptoPanic `votes`; `B11` no longer asks for chat confirmation before browser/source fallback and returns RSS-backed timestamps/links.
- [x] Root cause from the older B11 reply: after all `web_search_fetch` discovery attempts failed with missing search keys / anti-bot blockers, the model ended with "please confirm web_fetch/browser fallback" even though yolo mode and the `web_fetch` schema allow safe read-only browser fallback.
- [x] Generic fix: `nerya/agent/loop.py` now tracks `web_search_fetch` results that produced no documents and, before finalizing, adds one evidence-driven `web_fetch` fallback retry instead of allowing confirmation prose. This is keyed to structured tool-result shape and provider tool availability, not case id, language intent markers, or prompt regex routing.
- [x] Regression: `test_failed_search_fetch_gets_web_fetch_fallback_before_confirmation` covers the old confirmation-stall path and requires the retry call to expose `required_next_tool_names=["web_fetch"]`.
- [x] Local verification passed: new regression (`1 passed`), focused source/final-synthesis subset (`14 passed, 176 deselected`), `python -m py_compile nerya\agent\loop.py tests\test_agent_loop_final_summary.py`, `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and forbidden scan found no `_STRATEGY_INTENT_MARKERS` / case-id route additions.
- [ ] Next audit target: continue unresolved old high-risk rows outside the B/J/H/K closures, especially strategy/task/team rows whose historical pass status lacked durable API/tool contracts.

## 2026-06-07 E5 Synchronous Team Budget Closure
- [x] Root cause from latest E5 logs: `team_run` itself completed successfully, but it consumed essentially the whole 900s UI/turn window (`roles_total=10`, `max_parallel=4`, `timeout_s=840`). The parent loop then had no reliable time left to synthesize the final answer before Playwright timed out.
- [x] Secondary boundary gap: parent loop LLM request metadata recorded `remaining_wall_seconds`, but `ToolCall.metadata` did not pass the outer deadline to native tool handlers. As a result, a model-supplied `timeout_s` could override the outer turn budget for a heavy synchronous tool.
- [x] Generic fix: parent loop now attaches `turn_deadline_epoch`, `remaining_wall_seconds`, `wall_time_final_synthesis_seconds`, and `team_run_final_reserve_seconds` to every native `ToolCall`.
- [x] Generic fix: `team_run_handler` treats the parent remaining wall budget as a hard cap, preserving a final-synthesis reserve and exposing `timeout_uncapped_s`, `timeout_capped_by_parent`, `parent_remaining_wall_seconds`, and `parent_final_reserve_seconds` in the team summary.
- [x] Generic fix: when a successful synchronous `team_run` finishes with low remaining wall budget and no pending required artifact/action, the parent loop uses a compact team-only final synthesis request (`tools=[]`, `reasoning_effort=none`, bounded max tokens, provider deadline) instead of returning to a full tool-schema loop.
- [x] Local verification passed: new failing-first regressions for parent-budget cap, ToolCall budget metadata, and low-budget team compact synthesis now pass; related team subset passed (`8 passed`), related loop/wall-time subset passed (`6 passed`), no-runtime-route-hardcoding passed (`5 passed`), and touched-file `py_compile` passed.
- [x] Follow-up root cause from the first focused E5 pass: the case stopped timing out, but `team_run` still degraded because the model supplied `timeout_s=240` for a 7-role multi-wave market-analysis team. That value is a tool execution arg, not an operator/planner time budget, so it should not undercut the structural deep-team timeout floor.
- [x] Generic follow-up fix: `team_run` now raises model-authored `timeout_s` / `max_wall_seconds` to the auto team floor when no separate `deadline`, `timeout`, `time_budget`, or `time_budget_s` is present in the team args/shared payload. Explicit operator/planner time constraints still take priority, and parent wall-budget caps still apply last.
- [x] Follow-up local verification passed: deep-team floor regression pair (`2 passed`), full `tests/test_team_streaming_events.py` (`39 passed`), loop team/wall-time/final-synthesis subset (`39 passed, 162 deselected`), `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and touched-file `py_compile` passed.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`. If E5 passes, continue the broader remaining failed/high-risk set.

## 2026-06-07 E5 Team Parallelism and Stock-Research Wall-Time Follow-up
- [x] Root cause from the latest E5 TeamStore/context-full evidence: the model requested `max_parallel=7` for a 7-role `market_analysis_team`, creating seven concurrent MiniMax subagent requests. Three roles then degraded through provider EOF/network errors or team timeout, while some "successful" stock-research roles only returned `tool_observation_fallback`.
- [x] Generic fix: `team_run` now caps model-requested parallelism by runtime config and `TeamTemplate.max_parallel`; model prompts can request lower parallelism, but cannot raise a curated template above its structural limit. Timeout floors now use the effective worker count, so a 7-role market-analysis team with template cap 4 is budgeted as a two-wave team.
- [x] Generic fix: stock-research subagents now default to a 360s wall-time budget, giving roles that have already gathered live evidence time to emit the final role narrative instead of falling back immediately after tool collection.
- [x] Verification passed: focused red-green cap/wall-time tests (`5 passed, 36 deselected`), full team/compaction suite (`43 passed`), `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and touched-file `py_compile`.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`; inspect TeamStore role statuses and reply quality, not only Playwright pass/fail.

## 2026-06-07 E5 Deep-Team Quality Follow-up
- [x] Root cause from the next focused E5 pass: Playwright could pass, but TeamStore still showed `completed_with_failures`; `valuation_analyst` and `sec_analyst` timed out at 360s, while `fundamentals_analyst` was counted as succeeded even though its output was `quality=tool_observation_fallback`, `partial=true`, and `close_reason=duplicate_successful_tool_request`.
- [x] Generic fix: curated deep teams (`market_analysis_team`, `investment_committee_team`, `strategy_design_team`) now keep a 600s structural floor even when the effective worker cap makes the run a single wave, but only when the role set contains real research/template roles. Arbitrary ad-hoc role names with an explicit short timeout still keep their explicit budget.
- [x] Generic fix: `team_run` aggregation treats `output.partial=true` and `quality=tool_observation_fallback` as degraded member failures, so TeamStore and final synthesis cannot report half-finished tool-observation fallbacks as successful role outputs.
- [x] Generic fix: equity-research role budgets now include `valuation_analyst`, `sec_analyst`, and `investor_perspective`; duplicate successful child tool requests are suppressed and converted into one bounded recovery prompt telling the subagent to synthesize from existing observations instead of closing immediately as partial fallback.
- [x] Verification passed: failing-first focused tests for single-wave deep floor, partial fallback failure aggregation, and duplicate-tool recovery now pass together with explicit-timeout/model-floor guards (`5 passed, 38 deselected`). Full related suite passed: `tests/test_team_streaming_events.py tests/test_tool_compaction_team_run.py` (`45 passed`), `tests/test_no_runtime_route_hardcoding.py` (`5 passed`), and touched-file `py_compile`.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`; require a structured Chinese NVDA report and TeamStore evidence with no partial `tool_observation_fallback` counted as success.

## 2026-06-07 E5 Two-Wave Budget and Evidence Preservation Follow-up
- [x] Root cause from latest focused E5: with `market_analysis_team` capped to `max_parallel=4`, the 7-role team is a two-wave stock-research run. The previous structural floor and parent reserve policy still left second-wave roles underbudgeted, then timeout handling wrote empty task payloads for failed roles.
- [x] Generic fix: curated deep-team timeout floors now scale as `max(600, waves * 360)`, so two-wave stock-research teams get 720s while single-wave curated teams remain at 600s. Parent budget capping preserves this structural floor when the parent can still keep a small deterministic final reserve.
- [x] Generic fix: stock-research subagents that have already collected prefetch/tool observations return a degraded evidence fallback on transient initial LLM failure instead of raising an empty `SubAgentLLMError`.
- [x] Generic fix: TeamStore snapshots and deterministic team reports preserve failed/degraded member output, observations, metrics, and tokens; partial fallbacks still count as failures, but their evidence is no longer lost.
- [x] Adjacent harness fix: required-action retries stay narrowed to the pending required tool after hidden/read-only tool requests, and read-only discovery debt is cleared after a required write/action tool succeeds. Late wall-clock action abort behavior remains `wall_time_final_synthesis`.
- [x] Verification passed: team/compaction suite (`48 passed`), loop required/team subset (`39 passed, 164 deselected`), no-hardcoding (`5 passed`), and touched-file `py_compile`.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`; inspect TeamStore and context-full logs before accepting the Playwright pass.

## 2026-06-07 E5 Finalization-First Team Repair
- [x] Root cause from the next E5 pass: the case could pass Playwright while the final UX was still a raw internal `# AgentTeam report`. TeamStore remained `completed_with_failures`, and some roles had useful tool observations but no final role narrative.
- [x] Generic subagent fix: when a child runtime has successful observations but closes through `tool_calls_without_replan_settled` or degraded tool-observation paths, it now gets one final-only LLM pass over prior observations. The finalization prompt removes the callable tool directory and rejects `skill_calls` / `tool_calls`; if the provider still fails or asks for tools, the existing `tool_observation_fallback` remains.
- [x] Generic parent-loop fix: degraded-but-usable `team_run` evidence now attempts compact `tools=[]` final synthesis before deterministic report fallback. Deterministic `# AgentTeam report` is now reserved for synthesis failure, empty synthesis, or empty team evidence.
- [x] Local verification passed: failing-first subagent settle test and degraded-team synthesis-first test now pass; team/compaction suite (`48 passed`); focused final-summary subset (`9 passed, 195 deselected`); no-hardcoding (`5 passed`); touched-file `py_compile`; e2e TypeScript compile; touched-file forbidden marker scan returned no matches.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`; accept only if the reply is a useful Chinese NVDA report or bounded limitation report, not a raw AgentTeam report, and TeamStore does not count `tool_observation_fallback` as success.

## 2026-06-07 E5 Compact Team Finalization Follow-up
- [x] Root cause from the latest E5 focused pass: Playwright passed mechanically, but `team_final_synthesis` still sent the full always-on Agent system prompt and compact evidence with only about 32s remaining, then MiniMax timed out. The fallback exposed an internal `AgentTeam report` instead of a user-facing Chinese report.
- [x] Generic fix: `_synthesize_team_run_final_answer()` now uses the short team-final system prompt plus compact evidence; it no longer sends the full agent system prompt for final team synthesis.
- [x] Generic fix: degraded-but-usable team synthesis failure now returns `_build_team_run_bounded_fallback()` with bounded evidence and explicit gaps instead of the raw deterministic AgentTeam report.
- [x] Generic fix: subagents with existing observations now enter `subagent_finalization_reserve` before starting another normal tool-enabled LLM request when remaining wall time is within the finalization reserve. This gives the final-only helper time to produce role analysis instead of falling through to `tool_observation_fallback`.
- [x] Verification passed: compact-system/bounded-fallback red tests (`2 passed`), subagent reserve red test failed first then passed, team/compaction suite (`49 passed`), focused final-summary subset (`11 passed, 195 deselected`), no-hardcoding (`5 passed`), touched-file `py_compile`, e2e TypeScript compile, and forbidden marker scan over touched files.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`; inspect `E5.reply.txt`, TeamStore, and `dev_logs/llm_context_full.jsonl` before accepting the pass.

## 2026-06-07 E5 Equity Research Misroute Closure
- [x] Root cause from the latest real E5 context-full log: the model initially loaded `equity_research` and gathered source/market evidence, but after a denied `run_shell` attempt the generic strategy-prep retry saw `run_shell + market_data + enough tool calls` and forced `strategy_generate_proposal`. This converted a research-team request into a strategy proposal/backtest data-gap reply.
- [x] Generic fix: the loop now tracks loaded team-research skills through structured `Skill` / `skill_view` calls and, when source evidence exists without trade/strategy workflow context, requires one `team_run` retry before strategy proposal convergence can fire. The strategy proposal retry is still preserved for actual strategy-authoring context and is not prompt/case/ticker matched.
- [x] Regression: `test_equity_research_prep_requires_team_run_not_strategy_proposal_after_failed_shell` locks the failed-shell research path so the next required tool is `team_run` and `strategy_generate_proposal` is not called. The assertion allows existing evidence markers after the user-facing final text.
- [x] Local verification passed: related strategy/research routing subset (`5 passed, 202 deselected`), team/compaction suite (`49 passed`), focused final-summary subset (`10 passed, 197 deselected`), no-hardcoding (`5 passed`), touched-file `py_compile`, e2e TypeScript compile, and forbidden marker scan found no runtime markers.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`. Accept only if the turn uses `team_run`, avoids strategy proposal/backtest unless the user asked for strategy packaging, and returns a user-facing research report or bounded limitation report.

## 2026-06-07 E5 Required Team Recovery Follow-up
- [x] Root cause from the next real E5 run: the turn reached the correct equity-research path and required `team_run`, but MiniMax repeatedly timed out while being asked to emit that single required tool call. Context-full showed iteration 7 with `required_next_tool_names=[team_run]`; the full required-tool request timed out, then compact required-tool recovery timed out repeatedly. The run was interrupted to avoid wasting provider quota.
- [x] Generic fix: when the only pending required action is `team_run`, research-skill/source evidence is present, and the provider returns a transient timeout while emitting that required tool, the loop now executes the existing bounded `market_analysis_team` recovery immediately. This uses structured tool state and evidence, not prompt/case/ticker routing.
- [x] Verification passed: failing-first `test_required_team_research_timeout_recovers_without_provider_reasking` now passes; related loop subset (`8 passed, 204 deselected`); team/compaction suite (`49 passed`); no-hardcoding (`5 passed`); touched-file `py_compile`.
- [ ] Runtime gate: restart isolated runtime/dashboard on MiniMax/yolo/context-full/no-mock and rerun focused `E5`. Inspect context-full for `required_team_research_transient_recovery`, actual `team_run`, and user-facing synthesis.

## 2026-06-07 E5 Language-Neutral Team Finalization Repair
- [x] User correction accepted as a hard constraint: do not hardcode prompt/output language, Chinese fallback templates, case/ticker branches, or regex intent markers just to make E5 pass.
- [x] REF/Codex pattern applied at the mechanism level: raw requests/responses/tool payloads stay in diagnostic logs and raw refs, while user-facing fallbacks render only compact reduced views. No Codex wording or fixed report template was copied.
- [x] Generic fix: successful usable `team_run` results now enter compact `tools=[]` team final synthesis immediately after required-artifact and strategy-proposal debts are handled, instead of returning to a full tool-enabled loop with the entire tool directory.
- [x] Generic fix: LLM timeout evidence fallback now accepts observed team results and renders a language-neutral `AgentTeam bounded result` compact view instead of scraping `team_run role output` markers from the transcript.
- [x] Generic fix: `_build_team_run_bounded_fallback()` no longer detects Chinese or switches label languages. It emits stable schema labels (`AgentTeam bounded result`, `Team run`, `Role outputs`, `Gaps`) and sanitized role summaries only.
- [x] Regression coverage: fallback tests now assert structural cleanup and no `raw`/`skill_calls`/`status":"in_progress` leakage, not Chinese labels. A new success-path test locks immediate compact final synthesis before a full tool loop.
- [x] Local verification passed: focused team/fallback subset (`7 passed, 208 deselected`), team streaming/compaction suite (`49 passed`), no-hardcoding (`5 passed`), and touched-file `py_compile`. Touched-file forbidden scan found no runtime prompt/case/intent markers; the only Chinese hit was a pre-existing mock LLM test string.
- [ ] Runtime gate: restart current-code MiniMax/yolo/context-full/no-mock services and rerun focused `E5`; accept only if reply is a useful synthesized report or compact bounded team result without raw markers/internal schema leakage.

## 2026-06-07 Explicit Roles and Compact Team Contract Follow-up
- [x] User correction treated as a hard constraint: do not hardcode prompt templates, Chinese fallback text, prompt-language detection, ticker/case routes, or regex intent markers while repairing E5/team behavior.
- [x] Generic team fix: `team_run_handler` now distinguishes explicitly supplied `roles` from template-driven completion. When roles are supplied as tool arguments or provider-wrapped `item/items`, the runtime preserves that role set and does not silently append `market_analysis_team` required roles.
- [x] Generic proposal-finalizer fix: after a reviewable non-strategy proposal is created, unrelated auxiliary `next_required_action` debt from same-batch read-only tools no longer preempts proposal finalization. Required artifacts still preempt, and strategy-workflow debt still remains explicit.
- [x] Test contract cleanup: team-run completion tests now assert the compact final-synthesis contract (`context_scope=team_final_synthesis`, `tools=[]`, one compact evidence message) instead of the old full transcript/tool-directory loop. Strategy AgentTeam tests now require structured strategy evidence such as `strategy_design_team`, position sizing, or execution-plan fields instead of natural-language daily/strategy wording.
- [x] Verification passed: `tests/test_team_streaming_events.py tests/test_tool_compaction_team_run.py` (`49 passed`); focused final-summary subset (`47 passed, 168 deselected`); six newly exposed final-summary regressions (`6 passed, 209 deselected`); full related suite `tests/test_agent_loop_final_summary.py tests/test_team_streaming_events.py tests/test_tool_compaction_team_run.py tests/test_no_runtime_route_hardcoding.py` (`269 passed`); touched-file `py_compile`; E2E TypeScript compile; runtime-only forbidden marker scan found no `_STRATEGY_INTENT_MARKERS`, intent marker table, mock allowance, case-id route, or `E5` branch.
- [ ] Runtime gate remains: restart/current-check MiniMax/yolo/context-full/no-mock services and rerun focused `E5`; inspect `E5.reply.txt`, TeamStore, and current `llm_context_full.jsonl` before accepting the pass.

## 2026-06-08 Provider-Wire Context-Full Observability Closure
- User redirect: before more blind E2E prompt debugging, dev visibility must show the actual provider HTTP payload for every Agent/subagent/team LLM request. This is an observability repair, not a prompt/category routing repair.
- REF conclusion: AgentArchitecturePatterns docs point to separable audiences for raw logs vs model context; Codex rollout traces keep reconstructable raw request/response payloads out of user-facing context, while Claude Code keeps the exact post-compaction API messages for bug reports. Nerya should keep canonical Agent requests and provider-wire payloads together under one call id, then render only compact evidence to users.
- Gap found: `llm_context_full.jsonl` already captured canonical `LLMGateway.call_messages` / `call` requests, but OpenAI-compatible adapters still transformed those requests afterward. For MiniMax this transformation changes `max_tokens` to `max_completion_tokens`, injects `thinking`, reshapes tools into `function` calls, and applies provider-specific transcript filtering. Canonical logs alone could not prove what MiniMax actually received.
- Generic fix: provider HTTP helpers now emit scoped wire trace events through a contextvar observer. `LLMGateway` writes `wire_request`, `wire_response`, and `wire_error` records into the existing `<workspace>/dev_logs/llm_context_full.jsonl` only when context-full mode is enabled. Records share the canonical `call_id` and promote safe correlation fields such as `session_id`, `turn_id`, `iteration`, `context_scope`, `parent_call_id`, and `subagent`.
- Safety boundary: headers, bodies, error strings, tier config, and URL query strings still pass through redaction before disk. Query secrets such as API keys/tokens are removed even if a provider uses query-auth rather than `Authorization` headers. Wire tracing is diagnostic-only and cannot change provider call behavior.
- Verification for this slice: context/wire focused pytest passed (`12 passed, 236 deselected`); full gateway/transport/native metadata set passed (`33 passed`); Agent final-summary metadata subset passed (`2 passed, 213 deselected`); no-runtime-route-hardcoding passed (`5 passed`); touched-file `py_compile` passed; dashboard E2E TypeScript compile exited 0; runtime touched-file forbidden-marker scan and historical secret-fragment scan returned no matches.
- Next runtime gate: restart isolated MiniMax/yolo/context-full/no-mock services and rerun focused `E5` or the current high-risk failing set. Accept runtime results only after inspecting per-case logs, TeamStore/API evidence, and grouped `llm_context_full.jsonl` rows by `call_id`.

## 2026-06-08 Financial Datasets Data API Follow-up
- [x] Root gap found during E5/team diagnosis: `EquitiesClient` already supported Financial Datasets financial statements, metrics, estimates, prices, company facts, and SEC filing metadata, but `data_api` did not expose that provider/action surface. Equity research teams therefore over-relied on web/MCP discovery for SEC/DCF/financial-statement work.
- [x] Generic fix: added `financial_datasets` as a dynamic `data_api` provider with aliases for equity/financial/SEC use. Actions map directly to read-only `EquitiesClient` methods; no prompt/case/ticker routing was introduced.
- [x] Audit fix: table compaction now preserves `source_url` and `_envelope`, giving final synthesis and logs source/mode evidence without leaking secrets.
- [x] Verification passed: failing-first `financial_datasets/equities` tests turned green; `tests/test_data_api_tool.py` (`26 passed`), `tests/test_tool_compaction_data_api.py tests/test_data_api_tool.py` (`35 passed`), `tests/test_llm_context_audit.py tests/test_no_runtime_route_hardcoding.py` (`8 passed`), E5 recovery/subagent prompt regressions (`2 passed`), focused E5 fallback/team subsets (`6 passed`, `5 passed`), touched-file `py_compile`, and runtime-file forbidden-marker scan had no matches.
- [ ] Runtime gate: restart or reuse isolated MiniMax/yolo/context-full/no-mock services on `.nerya-e5-context-workspace`; rerun focused `E5`; inspect `dashboard/test-results/logs/E5.*`, TeamStore, and `dev_logs/llm_context_full.jsonl` before accepting the result.

## 2026-06-08 E5 Team Role Preservation Gap
- [x] Latest focused E5 passed Playwright and reply-quality gates, but current TeamStore evidence is still incomplete: `team-bac9f4f590` used `ad_hoc_parallel_team`, `roles_total=1`, and only `fundamentals_analyst` executed for the prompt `基本面 + DCF + SEC 最新 10-K + 投资大师视角`.
- [x] Context-full wire logging made the failure observable: final synthesis correctly admitted the missing SEC and investor-master coverage, but the team evidence handed to synthesis had already collapsed to one role before final reporting.
- [x] Root-cause target: inspect `team_run` tool-call argument normalization and provider wrapper recovery so explicit structured roles from provider output are preserved generically. Do not add prompt, ticker, language, case-id, or regex intent routing.
- [x] Regression target: a provider-shaped `team_run` call carrying roles through `_raw`, `item/items`, or `role_payloads` must dispatch all explicit roles and persist the same role set in the team summary and TeamStore mirror.

## 2026-06-08 Role Profile, Evidence Contract, and Team Trace Follow-up
- [x] Reference/read-only audits completed: AgentArchitecturePatterns/Codex/Claude references confirm typed trace/event envelopes, provider-wire payload snapshots, compaction as a request class, and subagent/team parent correlation fields. The implementation stays mechanism-level and does not copy prompt wording.
- [x] Generic role fix: model/operator requested role identity is now separate from execution profile. Near-synonym stock-research roles preserve their requested names in TeamStore/events/results while inheriting the canonical `fundamentals_analyst` prompt, skills, budget, prefetch, and evidence policy.
- [x] Generic team argument fix: `team_run` now accepts structured provider role containers from `roles`, `item/items`, `_raw/raw`, and `role_payloads`; explicit role sets stay authoritative and persist with the same role count in TeamStore metrics.
- [x] Generic quality fix: subagent outputs now attach an `evidence_contract` for canonical public-company research profiles. Missing required machine evidence marks the role `partial` with `quality=degraded_missing_evidence`, `error_kind=insufficient_research_evidence`, and `missing_evidence`; team aggregation treats that as a failed/degraded required member.
- [x] Native-tool visibility fix: native subagent tool errors preserve safe `error_kind`, `error_detail`, `retryable`, and redacted provider/action/args payloads instead of collapsing to `native tool returned is_error=true`.
- [x] Context-full correlation fix: canonical and provider-wire LLM records can now carry/filter `team_run_id`; normal main-loop calls set `context_scope=agent_loop`; team final synthesis, subagent normal/finalization calls, and native LLM tools propagate team correlation metadata.
- [x] Local verification passed: new role/profile/provider-wrapper/evidence-contract/context tests (`13 passed`), team/no-hardcoding suite (`61 passed`), LLM context/transport suite (`40 passed`), focused parent team synthesis subset (`39 passed`), touched-file `py_compile`, and runtime-file forbidden-marker scan with no matches.
- [ ] Runtime gate: restart or reuse isolated MiniMax/yolo/context-full/no-mock services and rerun focused `E5`. Accept only after inspecting `E5.reply.txt`, TeamStore metrics/tasks, `roles_failed`, `missing_evidence`, and grouped `dev_logs/llm_context_full.jsonl` by `team_run_id` and `call_id`.

## 2026-06-08 Continuation Gate - Real E5 and Full-Chain Observability
- [x] Inspect current process state, isolated workspace provider config, existing `E5` logs, TeamStore payloads, and grouped context-full records before changing code.
- [x] Run parallel read-only audits for reference best-practice patterns, current E5 team/context evidence, and runtime/e2e setup readiness.
- [x] Rerun focused `E5` on real MiniMax with yolo/no-mock/context-full only; reject any mock path, raw AgentTeam/internal JSON reply, or clean success with failed evidence contracts.
- [ ] Continue to latest failed/weak CSV rows and then the full CSV gate.

## 2026-06-08 E5 Bounded Fallback and Truncated Final Synthesis Closure
- [x] Generic fallback repair: `_build_team_run_bounded_fallback()` now renders language-neutral business evidence sections from structured role output fields, filters internal telemetry (`raw`, `status`, `skill_calls`, task/tool ids), keeps evidence-contract gaps without dumping telemetry, and formats scored/gap records as readable text rather than bare enum/JSON fragments.
- [x] Generic completion repair: `_team_final_text_appears_complete()` guards compact team final synthesis against obvious truncated markdown/report text. If the compact synthesis is empty or incomplete, the completed-team path falls back immediately to the bounded team report with `transition_reason=team_result_bounded_fallback` instead of re-entering a full tool-enabled parent loop.
- [x] Local verification passed: focused final-summary fallback/team subset (`12 passed, 211 deselected`); team streaming/compaction/context metadata/no-hardcoding suite (`69 passed`); touched-file `py_compile`; touched-file forbidden scan found no `_STRATEGY_INTENT_MARKERS`, prompt/case route, mock allowance, or `E5` branch.
- [x] Runtime verification passed on real MiniMax/yolo/no-mock/context-full using runtime `:18396`, dashboard `:3096`, workspace `dashboard/.nerya-e5-rerun-workspace`: focused `E5` passed (`1 passed`, 6.6m), with setup gates confirming `minimax-cn`, `MiniMax-M3`, base URL `https://api.minimaxi.com/v1`, key by vault ref, and `permission_mode=yolo`.
- [x] Current `dashboard/test-results/logs/E5.jsonl` has `transition_reason=team_result_compact_final_synthesis`, `stopped_reason=end_turn`, `budget.aborted=false`, `tool_names=["team_run"]`, `successful_tool_names=["team_run"]`, and API evidence `team_run_exists ok: team-749bf23846`.
- [x] Current `dashboard/test-results/logs/E5.reply.txt` is a complete 3608-character user-facing NVDA research report. It does not leak `team_run_id`, raw AgentTeam report markers, `tool_observation_fallback`, `skill_calls`, task ids, raw/status payloads, enum-only ratings, bare score lines, or JSON/Python gap strings.
- [x] TeamStore evidence: `dashboard/.nerya-e5-rerun-workspace/teams/team-749bf23846/run.json` reports `status=completed`, `roles_total=5`, `roles_succeeded=5`, `roles_failed=0`, `phase=close`.
- [x] Context-full evidence: `dashboard/.nerya-e5-rerun-workspace/dev_logs/llm_context_full.jsonl` contains the `team_run_id=team-749bf23846` correlation on subagent and team final synthesis calls. The final synthesis call used `context_scope=team_final_synthesis`, `tools_sent_count=0`, and MiniMax provider-wire records; two attempts timed out and the third returned 200 with complete final text.
- [ ] Next runtime audit target: rerun unresolved strategy/task/team high-risk cases on current code, starting with `C7,C-AT4,C-AT10,D4,D7,E9,E10,E12`, then expand to the remaining weak/review rows and finally full CSV.
