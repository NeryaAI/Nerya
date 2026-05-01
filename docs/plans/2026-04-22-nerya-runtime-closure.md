# Nerya Runtime Closure Implementation Plan

> Historical note: this document records the previous closure roadmap. For the current truth-based production readiness source of truth, use `docs/plans/2026-04-22-nerya-production-alignment-plan.md`.

> **For Codex:** Execute this plan task-by-task. Use parallel agents only for independent files/tasks.

**Goal:** Finish Nerya as a runtime-native Hermes-referenced agent/trading system without silent mocks, static bottlenecks, provider false-positives, or indicator-analysis gaps.

**Architecture:** Keep the current Nerya-native runtime spine, but close the project in the right order: first make runtime truth explicit, then deepen the core loop/subagent/session surfaces, then widen orchestration and provider capability, then land TA-Lib-backed analysis, and only then close optimization/evolution/operator release gates. Every phase must end with real runtime evidence, not placeholder code paths.

**Tech Stack:** Python 3.10+, FastAPI/HTTP routes, YAML workspace config, MCP/ACP, provider REST adapters (OpenAI/Anthropic/Gemini/Ollama/Bedrock/OpenAI-compatible), trading connectors, TA-Lib, pytest.

---

## Audit Summary

- **Already real:** Nerya is now runtime-native; `nerya/adapter/` is gone; MCP/ACP ingress exists; Phases 0 and 1 are complete; Phases 2-10 are partially implemented and backed by passing targeted tests.
- **Still risky:** runtime truth is not trustworthy enough yet because several hot paths silently fall back to mock data or mock LLM behavior.
- **Provider situation:** Nerya already supports `openai`, `anthropic`, `gemini`, `ollama`, `bedrock`, `google_code_assist`, plus OpenAI-compatible providers such as `deepseek`, `openrouter`, `xai`, `mistral`, `groq`, `moonshot`, `together`, `cerebras`. The gap is not raw provider count anymore; the gap is feature parity and truthfulness.
- **Trading-analysis situation:** `nerya/data/indicators.py` and `nerya/data/features.py` are still minimal, and native TA-Lib support does not exist yet.
- **Release interpretation:** any feature that still depends on silent fallback, placeholder generation, mock-only defaults, or static discovery on a production path must be treated as incomplete.

## Execution Order

1. **Phase 11 first**: eliminate silent mock/hardcoded truth distortions so later verification is meaningful.
2. **Phases 2-4 next**: finish the core runtime loop, subagents, and memory/session semantics.
3. **Phases 5, 6, and 12 together**: unify Trigger/Triggle, SkillRuntime, ACP/MCP/API/CLI/dashboard, and route/data flexibility.
4. **Phase 13 before parity claims**: provider support only counts after feature-matrix closure and live smoke paths.
5. **Phases 7 and 14 before deep optimization**: strategy versioning and TA-Lib-backed indicators must exist before claiming trading intelligence.
6. **Phases 8 and 9 after richer evidence lands**: optimization and self-improvement must consume real attribution, indicator, and provider/runtime evidence.
7. **Phases 10 and 15 last**: operator surfaces and final release gates close the program.

---

## Phase 0 — Truth Reset and Direction Freeze

**Status:** Completed

**Goal:** Keep the repo honest that Nerya is the runtime and Hermes is reference material only.

**Verified state:**
- `README.md`
- `docs/nerya-architecture.md`
- `docs/runtime-ownership.md`
- `tests/test_architecture_audit.py`

**Definition of done:**
- No documentation path implies Hermes is a runtime dependency.
- No production module reintroduces a Hermes bridge package.

## Phase 1 — Runtime Boundary and Ownership Model

**Status:** Completed

**Goal:** Keep provider/security/runtime boundaries fixed while capability depth grows.

**Verified state:**
- `docs/runtime-ownership.md`
- `tests/test_architecture_audit.py`

**Definition of done:**
- Provider access stays inside `nerya/llm/`.
- Secret resolution stays inside security-owned boundaries.

## Phase 11 — Production Truth Gate: Remove Silent Mocks, Hardcoded Runtime Defaults, and Placeholder Paths

**Status:** Completed

**Goal:** Make every runtime result truthfully declare whether it came from a real provider/connector, an explicitly requested paper/mock mode, or a degraded unavailable path.

**Current verified gaps:**
- `nerya/data/candles.py`, `news.py`, `social.py`, `funding.py`, `defi.py`, `onchain.py` silently fall back to mock helpers.
- `nerya/skills/_connector_helpers.py` falls back to `MockExchange` / `MockChain` on public connector resolution failure.
- `nerya/api/routes_market.py` exposes `mock` as a normal public venue and falls back to mock connectors on failure.
- `nerya/workspace/bootstrap.py` seeds example strategies around `MOCK:BTCUSDT`.
- `nerya/llm/model_router.py` falls back to mock when provider config or key resolution is missing.
- `nerya/connectors/evm_native.py` still documents a placeholder DEX mid price.
- `nerya/evolution/skill_generator.py` still emits placeholder skill/action bodies.

**Files:**
- Modify: `nerya/data/candles.py`
- Modify: `nerya/data/news.py`
- Modify: `nerya/data/social.py`
- Modify: `nerya/data/funding.py`
- Modify: `nerya/data/defi.py`
- Modify: `nerya/data/onchain.py`
- Modify: `nerya/skills/_connector_helpers.py`
- Modify: `nerya/api/routes_market.py`
- Modify: `nerya/workspace/bootstrap.py`
- Modify: `nerya/llm/model_router.py`
- Modify: `nerya/connectors/evm_native.py`
- Modify: `nerya/evolution/skill_generator.py`
- Modify: `nerya/core/config.py`
- Create: `tests/test_runtime_truth_gate.py`
- Create: `tests/test_data_source_degradation.py`
- Create: `tests/test_llm_no_silent_mock.py`

**Must deliver:**
- A shared runtime truth envelope such as `source`, `mode`, `degraded`, `fallback_used`, `error`, `provider`, `venue`, `connector_id`.
- Silent mock fallback removed from production/runtime code paths.
- Explicit opt-in mock or paper mode preserved for tests, demos, and local bootstrap.
- Example workspace seeds switched from hardcoded `MOCK:*` market assumptions to explicit paper/demo presets.
- Placeholder evolution output blocked or marked `proposal_only_unimplemented`, never passed off as a working generated skill.

**Required verification:**
- Run: `pytest tests/test_runtime_truth_gate.py tests/test_data_source_degradation.py tests/test_llm_no_silent_mock.py -q`
- Run: `pytest tests/test_architecture_audit.py tests/test_llm_gateway.py tests/test_provider_unified.py -q`
- Run: `rg -n "generated placeholder|fallback to mock|return mock_|MOCK:BTCUSDT" nerya`

**Definition of done:**
- A missing provider key, broken venue, or offline chain returns an explicit unavailable/degraded result, not fake success data.
- Mock paths are reachable only through explicit dev/test/paper configuration.
- Docs and API payloads make degraded data obvious.

**Reject this phase as incomplete if:**
- Any production route can still silently fabricate candles/news/social/funding/onchain results.
- Any provider missing credentials still returns a successful mock completion in normal runtime mode.

## Phase 2 — Native Turn Engine v2 Completion

**Status:** Completed

**Goal:** Finish the main agent as a true iterative observe -> think -> act -> observe loop instead of one decision pass.

**Current verified gaps:**
- `nerya/agent/kernel.py` already records `turn_id`, `TurnStep`, per-step costs, and `stopped_reason`.
- The kernel still consumes a one-pass result shape rather than re-planning mid-turn after state changes.

**Files:**
- Modify: `nerya/agent/kernel.py`
- Modify: `nerya/agent/planner.py`
- Modify: `nerya/agent/output_parser.py`
- Modify: `nerya/llm/gateway.py`
- Modify: `nerya/harness/`
- Extend: `tests/test_agent_loop.py`

**Must deliver:**
- Iterative step execution with explicit step outcomes.
- Mid-turn re-planning after prior skill/subagent output mutates context.
- Resume/recovery semantics from persisted turn journal.
- Stable stop reasons for success, blocked, budget, approval wait, and terminal error.

**Required verification:**
- Run: `pytest tests/test_agent_loop.py -q`

**Definition of done:**
- One trigger can cause multiple sequential skill actions in one turn.
- A mid-turn failure can be resumed or explained from the journal.

**Reject this phase as incomplete if:**
- The kernel still only supports a single planned `actions[]` pass.

## Phase 3 — Real Subagent Runtime

**Status:** Completed

**Goal:** Promote subagents from lightweight one-shot analyzers into isolated child runtimes with bounded skill use.

**Current verified gaps:**
- `nerya/subagents/dispatcher.py` already supports concurrency, `dispatch_many`, denylists, and budget envelopes.
- `nerya/subagents/runtime.py` is still a single `subagent_analysis` LLM call.

**Files:**
- Modify: `nerya/subagents/runtime.py`
- Modify: `nerya/subagents/dispatcher.py`
- Modify: `nerya/subagents/context_policy.py`
- Modify: `nerya/subagents/result_aggregator.py`
- Modify: `nerya/subagents/registry.py`
- Modify: `nerya/skills/builtin/subagent_skill/actions.py`
- Extend: `tests/test_subagent_runtime_phase3.py`
- Extend: `tests/test_subagent_routing.py`

**Must deliver:**
- Child runtimes with internal step loops and allowed-skill execution.
- Parent/child context isolation plus artifact return paths.
- Contribution metrics beyond token/cost counts: signal used, skill calls, rejected actions, uncertainty, evidence references.

**Required verification:**
- Run: `pytest tests/test_subagent_runtime_phase3.py tests/test_subagent_routing.py -q`

**Definition of done:**
- At least two subagents can run concurrently and independently within one parent turn.
- A child can invoke allowed skills without breaking isolation.

**Reject this phase as incomplete if:**
- `SubAgentRuntime.run()` is still only “build prompt -> one LLM call -> return dict”.

## Phase 4 — Native Session and Memory Model

**Status:** Completed

**Goal:** Unify main-turn, subagent, review, and evolution memory/session semantics.

**Current verified gaps:**
- `nerya/agent/working_memory.py` exists.
- Persisted session identity and TTL/compaction/recall rules are not unified yet.

**Files:**
- Modify: `nerya/agent/memory.py`
- Modify: `nerya/agent/memory_recall.py`
- Modify: `nerya/agent/working_memory.py`
- Modify: `nerya/agent/context_builder.py`
- Modify: `nerya/strategy_history/`
- Extend: `tests/test_memory_recall.py`
- Extend: `tests/test_memory_isolation.py`

**Must deliver:**
- Session IDs and persistence rules for main turns, child turns, reviews, and evolution.
- Strategy-scoped versus global memory separation.
- TTL, compaction, recall-budget, and explain surfaces.

**Required verification:**
- Run: `pytest tests/test_memory_recall.py tests/test_memory_isolation.py -q`

**Definition of done:**
- Memory behavior is bounded, explainable, and strategy-safe.

**Reject this phase as incomplete if:**
- Session semantics still differ by surface or rely on ad hoc file conventions.

## Phase 5 — Trigger/Triggle Orchestration Plane

**Status:** Completed

**Goal:** Make Trigger/Triggle the single ingress/control plane for runtime work.

**Current verified gaps:**
- Trigger kinds have expanded, but explain/replay/operator surfaces are still thin.
- Scheduling is still shaped mainly around `every_seconds`.

**Files:**
- Modify: `nerya/triggers/event.py`
- Modify: `nerya/triggers/router.py`
- Modify: `nerya/triggers/runtime.py`
- Modify: `nerya/triggers/cron.py`
- Modify: `nerya/triggers/schedule.py`
- Modify: `nerya/triggers/routes.py`
- Modify: `nerya/sdk/trigger_api.py`
- Modify: `nerya/api/routes_triggers.py`
- Extend: `tests/test_trigger_router.py`
- Extend: `tests/test_trigger_sdk.py`
- Extend: `tests/test_trigger_stats_phase5.py`

**Must deliver:**
- One ingress matrix covering user, schedule, webhook, script, market, review, replay, and operator events.
- Replay/debug surfaces for route decisions.
- Schedule schema that can evolve beyond only `every_seconds`.

**Required verification:**
- Run: `pytest tests/test_trigger_router.py tests/test_trigger_sdk.py tests/test_trigger_stats_phase5.py -q`

**Definition of done:**
- Every runtime entry becomes a `TriggerEvent`.
- Route decisions are explainable and replayable.

**Reject this phase as incomplete if:**
- Any supported automation path bypasses Trigger/Triggle.

## Phase 6 — Skill Runtime and SDK Unification

**Status:** Completed

**Goal:** Make `SkillRuntime` the one capability boundary for agent, scripts, API, CLI, and dashboard.

**Current verified gaps:**
- Skill dispatch is already central, but versioning/tags/trace/caller identity are incomplete.

**Files:**
- Modify: `nerya/skills/runtime.py`
- Modify: `nerya/skills/registry.py`
- Modify: `nerya/skills/permissions.py`
- Modify: `nerya/skills/schema.py`
- Modify: `nerya/sdk/`
- Modify: `nerya/api/routes_skills.py`
- Modify: `nerya/api/routes_agent.py`
- Modify: `nerya/cli/`
- Extend: `tests/test_strategy_skill.py`
- Create: `tests/test_skill_runtime_contracts.py`

**Must deliver:**
- Stable capability/version metadata.
- Caller attribution across SDK/API/CLI/dashboard.
- Removal of any duplicated or bypassed capability path.

**Required verification:**
- Run: `pytest tests/test_strategy_skill.py tests/test_skill_runtime_contracts.py -q`

**Definition of done:**
- No supported runtime path bypasses `SkillRuntime`.

**Reject this phase as incomplete if:**
- API/CLI/dashboard still behave differently for the same skill action.

## Phase 12 — Flexible Routing, Data IO, and Dynamic Capability Discovery

**Status:** Completed

**Goal:** Remove narrow routing/data-return assumptions so the agent can fetch and return data flexibly instead of being boxed into static patterns.

**Current verified gaps:**
- `nerya/agent/planner.py` is config-driven but still pattern-based and narrow.
- `nerya/triggers/schedule.py` is `every_seconds`-centric.
- `nerya/acp/__init__.py` explicitly says capability discovery is static.
- Data-return surfaces are still biased toward fixed JSON blobs and small fixed routes.

**Files:**
- Modify: `nerya/agent/planner.py`
- Modify: `nerya/triggers/schedule.py`
- Modify: `nerya/triggers/routes.py`
- Modify: `nerya/acp/__init__.py`
- Modify: `nerya/acp/server.py`
- Modify: `nerya/mcp/`
- Modify: `nerya/api/routes_market.py`
- Modify: `nerya/api/routes_agent.py`
- Modify: `nerya/api/routes_llm.py`
- Extend: `tests/test_acp_server.py`
- Create: `tests/test_route_explain_and_discovery.py`

**Must deliver:**
- Richer schedule grammar: cron/RRULE/windowed/event-conditioned scheduling.
- Dynamic ACP/MCP capability/tool discovery from live runtime registries.
- Route explain surfaces that show why a trigger matched and why a tool/skill was exposed.
- Flexible result envelopes for tables, paged results, partials, streaming chunks, and binary/blob references.

**Required verification:**
- Run: `pytest tests/test_acp_server.py tests/test_route_explain_and_discovery.py -q`

**Definition of done:**
- Route resolution is flexible, explainable, and not hardcoded to a few patterns.
- Capability discovery is dynamic for production surfaces.

**Reject this phase as incomplete if:**
- Planner/routing behavior still depends on brittle hardcoded match lists that diverge from runtime state.

## Phase 13 — AI Provider Parity and Model Routing Completion

**Status:** Completed

**Goal:** Match Hermes-level provider capability where it matters, excluding Hermes platform-specific APIs, and make provider support claims evidence-backed.

**Current verified gaps:**
- Provider coverage is broad, but feature support is still thinner than Hermes in several places:
  - no OpenAI Responses/Codex lane comparable to Hermes auxiliary client behavior
  - limited provider-native reasoning/thinking handling
  - limited streaming/tool-streaming normalization
  - limited tool-choice/schema/multimodal parity
  - missing provider capability matrix and smoke-test gate
- `ModelRouter` still hides provider configuration failures behind mock fallback.

**Files:**
- Modify: `nerya/llm/model_router.py`
- Modify: `nerya/llm/model_catalog.py`
- Modify: `nerya/llm/gateway.py`
- Modify: `nerya/llm/adapters/openai.py`
- Modify: `nerya/llm/adapters/anthropic.py`
- Modify: `nerya/llm/adapters/gemini.py`
- Modify: `nerya/llm/adapters/bedrock.py`
- Modify: `nerya/llm/adapters/google_code_assist.py`
- Modify: `nerya/llm/adapters/ollama.py`
- Modify: `docs/llm-gateway.md`
- Extend: `tests/test_llm_providers.py`
- Extend: `tests/test_llm_providers_extended.py`
- Extend: `tests/test_provider_unified.py`
- Extend: `tests/test_llm_adapters_bedrock_google.py`
- Create: `tests/test_provider_capability_matrix.py`

**Must deliver:**
- Provider-neutral request surface with provider-specific branching kept inside adapters.
- Capability matrix covering at least: sync call, stream, tool calling, tool choice, schema/json mode, reasoning/thinking, multimodal input, model discovery, auth modes, timeout/retry, pricing metadata.
- Live smoke-test path per supported provider family.
- Explicit support classification: `supported`, `experimental`, `metadata-only`, `unsupported`.
- OpenAI-compatible providers handled without leaking provider-specific hacks into the common runtime interface.

**Required verification:**
- Run: `pytest tests/test_llm_providers.py tests/test_llm_providers_extended.py tests/test_provider_unified.py tests/test_llm_adapters_bedrock_google.py tests/test_provider_capability_matrix.py -q`

**Definition of done:**
- A provider is only listed as supported when the mandatory capability matrix passes.
- Missing credentials, unsupported features, or degraded provider modes are surfaced explicitly.

**Reject this phase as incomplete if:**
- “Provider supported” only means `/models` works.
- Provider failures are still hidden by mock success paths.

## Phase 7 — Strategy OS and Lifecycle v2

**Status:** Completed

**Goal:** Finish strategy versioning, promotion, rollback, and runtime binding.

**Files:**
- Modify: `nerya/trading/strategy_lifecycle.py`
- Modify: `nerya/skills/builtin/strategy_skill/actions.py`
- Modify: `nerya/trading/strategies.py`
- Modify: `nerya/trading/accounts.py`
- Modify: `nerya/strategy_history/`
- Extend: `tests/test_strategy_lifecycle_phase7.py`
- Extend: `tests/test_strategy_history.py`

**Must deliver:**
- Strategy version IDs, promotion records, rollback targets, and runtime binding to prompts/routes/accounts/environment snapshots.

**Required verification:**
- Run: `pytest tests/test_strategy_lifecycle_phase7.py tests/test_strategy_history.py -q`

## Phase 14 — Native TA-Lib Indicator Engine and Collaborative Analysis

**Status:** Completed

**Goal:** Add native TA-Lib support and make indicators first-class inputs to analysis, review, and optimization instead of lightweight helper math.

**Current verified gaps:**
- `nerya/data/indicators.py` only exposes SMA, pct change, and breakout detection.
- `nerya/data/features.py` only computes a tiny fixed feature set.
- No native TA-Lib dependency wiring exists in `pyproject.toml`.

**Files:**
- Modify: `pyproject.toml`
- Modify: `nerya/data/indicators.py`
- Modify: `nerya/data/features.py`
- Modify: `nerya/skills/builtin/market_data_skill/actions.py`
- Modify: `nerya/skills/builtin/strategy_review_skill/actions.py`
- Modify: `nerya/strategy_history/review.py`
- Modify: `nerya/strategy_history/explain.py`
- Modify: `docs/skill-first-trading.md`
- Create: `tests/test_indicators_talib.py`
- Create: `tests/test_features_indicator_fusion.py`

**Must deliver:**
- Native TA-Lib integration with installation/runtime checks.
- Indicator registry covering at least RSI, MACD, ATR, Bollinger Bands, ADX, Stoch, CCI, OBV, VWAP, EMA family, volatility/range indicators.
- Multi-timeframe indicator bundles.
- Collaborative analysis layer that can combine indicator signals with candles/funding/onchain/news/subagent evidence.
- Indicator outputs persisted in review/explain/optimization artifacts.

**Required verification:**
- Run: `pytest tests/test_indicators_talib.py tests/test_features_indicator_fusion.py tests/test_strategy_review_flow.py -q`

**Definition of done:**
- Strategies, subagents, and review flows can request indicator bundles through normal runtime APIs.
- Indicator outputs are explainable and attributable in downstream review/optimization.

**Reject this phase as incomplete if:**
- TA-Lib exists only as a wrapper utility with no runtime integration.
- Backtest/paper/live flows compute different indicator semantics.

## Phase 8 — Trading-Native Optimization Engine

**Status:** Completed

**Goal:** Make the system able to explain trading outcomes in terms of runtime, strategy, execution, and indicator evidence.

**Files:**
- Modify: `nerya/strategy_history/attribution.py`
- Modify: `nerya/skills/builtin/strategy_review_skill/actions.py`
- Modify: `nerya/strategy_history/review.py`
- Modify: `nerya/strategy_history/explain.py`
- Modify: `nerya/trading/reconciliation.py`
- Modify: `nerya/trading/paper.py`
- Extend: `tests/test_attribution_phase8.py`
- Extend: `tests/test_strategy_review_flow.py`

**Must deliver:**
- Subagent contribution attribution.
- Parameter/indicator sensitivity and scenario replay.
- Paper/live divergence analysis.
- Execution-quality attribution beyond coarse slippage thresholds.

**Required verification:**
- Run: `pytest tests/test_attribution_phase8.py tests/test_strategy_review_flow.py -q`

## Phase 9 — Evidence-Driven Reflection and Evolution

**Status:** Completed

**Goal:** Keep self-improvement proposal-only, but upgrade it to consume the richer evidence from Phases 8, 11, 13, and 14.

**Files:**
- Modify: `nerya/agent/self_improvement.py`
- Modify: `nerya/agent/reflection.py`
- Modify: `nerya/evolution/`
- Extend: `tests/test_reflection_evolution.py`
- Extend: `tests/test_evolution_phase9.py`

**Must deliver:**
- Proposal ranking fed by attribution, indicator, and provider/runtime quality signals.
- Better rationale, rollback plans, and test-plan generation.

**Required verification:**
- Run: `pytest tests/test_reflection_evolution.py tests/test_evolution_phase9.py -q`

## Phase 10 — Operator Surfaces, Observability, and Replay Closure

**Status:** Completed

**Goal:** Make runtime behavior inspectable from ingress to final action.

**Files:**
- Modify: `nerya/observability/trace.py`
- Modify: `nerya/api/routes_agent.py`
- Modify: `nerya/api/routes_strategy_history.py`
- Modify: `nerya/api/routes_evolution.py`
- Modify: `nerya/api/routes_market.py`
- Modify: `dashboard/`
- Modify: `docs/runbook.md`
- Extend: `tests/test_observability_phase10.py`

**Must deliver:**
- End-to-end replay/explain/trace surfaces in API and dashboard.
- Proposal, review, route, and provider/degradation visibility for operators.

**Required verification:**
- Run: `pytest tests/test_observability_phase10.py -q`

## Phase 15 — Final Release Gate: No Half-Finished Features

**Status:** Completed

**Goal:** Prevent the repo from declaring completion while real behavior is still mock-backed, placeholder-backed, or operator-invisible.

**Files:**
- Modify: `tests/test_architecture_audit.py`
- Modify: `docs/nerya-native-runtime-plan.md`
- Modify: `README.md`
- Create: `tests/test_release_truth_gate.py`
- Create: `tests/test_no_placeholder_runtime_paths.py`

**Must deliver:**
- CI truth gate for silent mock usage in runtime paths.
- CI gate for placeholder generation in production flows.
- Provider support matrix gate.
- Docs truth gate: docs may not claim parity/support unless the corresponding tests and surfaces exist.
- Release checklist that explicitly records unsupported/degraded/experimental areas.

**Required verification:**
- Run: `pytest tests/test_release_truth_gate.py tests/test_no_placeholder_runtime_paths.py tests/test_architecture_audit.py -q`

**Definition of done:**
- A feature counts as implemented only when runtime path, tests, docs, and operator visibility all exist together.

**Reject this phase as incomplete if:**
- Any major capability is “present in code” but lacks a real success path or verification surface.

---

## Program-Level Acceptance Criteria

- Nerya can run without Hermes code or installation.
- No normal runtime path silently returns mock/provider-fake data.
- Provider support claims are backed by capability-matrix tests and live smoke paths.
- Trigger/Triggle is the single ingress plane.
- SkillRuntime is the single capability plane.
- TA-Lib-backed indicators are natively supported and used by analysis/review flows.
- Trading optimization can attribute outcomes across strategy, indicators, subagents, risk, execution, and provider/runtime quality.
- Self-improvement remains proposal-only and evidence-backed.
- Operators can replay and explain any run end-to-end.

## Stop Conditions

Do not mark the program complete if any of the following is still true:

- mock fallbacks remain in production/runtime hot paths
- provider support is only partial but documented as full
- routing/discovery is still static where runtime state should be authoritative
- TA-Lib exists only as a library dependency with no runtime integration
- optimization/review does not consume the richer indicator/provider/subagent evidence
- dashboard/API cannot explain degraded or failed runs
