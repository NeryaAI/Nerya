# Nerya Production Alignment Plan

Status: truth-based production closure plan  
Date: 2026-04-22  
Audience: runtime maintainers, trading/runtime contributors, operator-surface contributors  
Supersedes: `docs/plans/2026-04-22-nerya-runtime-closure.md` for any current production-readiness claim

## 1. Purpose

This document is the source of truth for the question:

> "What must still be true in code, tests, operator surfaces, and runtime behavior before Nerya can honestly be declared production-runnable as its own agent runtime?"

The answer is not "all tests pass" and not "the architecture looks complete on paper".
Nerya only counts as production-aligned when:

1. it runs without Hermes code or installation,
2. it does not silently fabricate market, chain, provider, or reasoning results,
3. its provider support claims are narrower than or equal to reality,
4. its trigger, skill, agent, and trading boundaries are the same in code and in operator surfaces,
5. its trading-native features are real runtime features, not decorative review artifacts,
6. an operator can run it in paper, canary, and live modes with explicit observability and rollback.

This plan is intentionally stricter than "feature exists somewhere in the repo".

## 2. Current Truth Baseline

### 2.1 Latest verified audit snapshot

The latest targeted regression audit on 2026-04-22 ran:

```bash
python -m pytest \
  tests/test_architecture_audit.py \
  tests/test_agent_loop.py \
  tests/test_subagent_runtime_phase3.py \
  tests/test_reflection_evolution.py \
  tests/test_observability_phase10.py \
  tests/test_strategy_lifecycle_phase7.py \
  tests/test_attribution_phase8.py \
  tests/test_evolution_phase9.py \
  tests/test_runtime_truth_gate.py \
  tests/test_route_explain_and_discovery.py \
  tests/test_provider_capability_matrix.py \
  tests/test_indicators_talib.py \
  tests/test_features_indicator_fusion.py \
  tests/test_release_truth_gate.py \
  tests/test_no_placeholder_runtime_paths.py \
  -q
```

Result:

- `157 passed, 1 skipped`

This proves Nerya is no longer a mostly-half-built shell. It already has a real runtime spine:

- iterative turn-step journaling exists,
- subagent child runtime exists,
- strategy lifecycle and versioning exist,
- attribution and reconciliation exist,
- proposal-only self-improvement exists,
- route explain and capability discovery surfaces exist,
- indicator and feature surfaces are much richer than before,
- Hermes is not a runtime dependency.

### 2.2 What is still not honest to claim today

The following claims are still too strong today and must not be used in docs, release notes, or operator promises:

- "Nerya is fully Hermes-parity across provider/runtime capability."
- "All production data paths are real and never degrade into synthetic values."
- "ACP/IDE protocol parity is complete."
- "Scheduler/operator route control is fully closed."
- "Native TA-Lib is guaranteed in production by default."
- "The unique trading optimization layer is fully mature closed-loop autonomy."

### 2.3 Current blockers that still matter

| Area | Current truth | Why this blocks production closure |
|---|---|---|
| Skill/runtime truth | Some skill hot paths still fall back to `mock_exchange` / `mock_chain` or fabricate snapshots on failure. | Operators can receive synthetic success data when a real dependency failed. |
| Provider capability parity | Capability matrix is honest, but many provider features are still `experimental`, `metadata-only`, or `unsupported`. | Broad provider count does not equal Hermes-level runtime capability. |
| ACP/operator protocol | ACP is dynamic enough to be useful, but still explicitly minimal and not full ACP parity. | IDE/operator integration claims would overstate reality. |
| Trigger/scheduling surface | Scheduler core supports more than `every_seconds`, but the operator-facing skill surface still lags behind. | The runtime is more capable than its control plane, which creates partial operations. |
| Indicator runtime | TA-Lib support is real but optional, with pure-Python fallback. | If production requires native TA-Lib semantics, the current default is not strong enough. |
| Hardcoded defaults | Some runtime-facing paths still default to `binance`, `BTCUSDT`, or synthetic/manual buckets. | This reduces operator flexibility and can hide missing configuration. |

## 3. What "Production-Runnable" Means for Nerya

Nerya needs four distinct operating states. Production closure is not one switch.

| Stage | Purpose | Data truth requirement | Trading mode | Release claim allowed |
|---|---|---|---|---|
| `local_dev` | developer iteration, demos, tests | mock/paper allowed, but explicit | paper only | "development/runtime demo" |
| `prod_paper` | production environment with real providers and real connectors, but paper execution | real providers/connectors or explicit degraded result | paper only | "production paper-ready" |
| `canary_live` | small controlled live exposure with rollback and audit | real providers/connectors only on live path | limited live | "canary live-ready" |
| `full_live` | normal production trading | real providers/connectors only on live path | full live | "production live-ready" |

Nerya is not production-aligned until it can at least run `prod_paper` honestly and promote a strategy through `draft -> paper -> canary -> live` with real operator evidence.

## 4. Non-Negotiable Global Rules

These rules apply to every phase below:

1. No normal runtime path may silently fabricate market, chain, or LLM output.
2. Mock and paper behavior must be explicit in request, config, response envelope, and operator UI.
3. Unsupported provider capabilities must fail or degrade explicitly, never pass as successful parity.
4. No agent/subagent/script path may bypass `TriggerEvent`, `SkillRuntime`, `RiskGate`, or `ApprovalGate`.
5. No self-improvement path may mutate protected live scopes directly.
6. Hardcoded examples are acceptable in tests, fixtures, and sample workspaces; they are not acceptable as invisible runtime defaults in production hot paths.
7. A phase is incomplete until code path, tests, docs, and operator visibility all exist together.

## 5. Execution Order

Work must land in this order:

1. close runtime truth gaps first,
2. then close provider/runtime capability truth,
3. then close trigger/control-plane flexibility,
4. then close agent loop, subagent, and memory semantics,
5. then deepen strategy/trading intelligence,
6. then close operator surfaces and deployment/runbook,
7. only then certify paper, canary, and live readiness.

If this order is broken, later validation becomes misleading.

## 6. Phase 1 — Runtime Truth and Data Integrity

### Goal

Remove silent synthetic success from runtime hot paths and make every degraded path explicit.

### Why this is first

If the runtime can still return fake success data, every later parity or production claim is contaminated.

### Current blockers

- `nerya/skills/builtin/market_data_skill/actions.py` still falls back to mock ticker data when real connector calls fail.
- `nerya/skills/builtin/trading_skill/actions.py` still fabricates a `market_snapshot` when none is supplied.
- `nerya/skills/builtin/onchain_skill/actions.py` still falls back to `mock_chain` on balance lookup failure.
- Some runtime-facing defaults still imply a market or venue even when the caller did not provide one.

### Required implementation

- Replace silent mock fallback in skill hot paths with one of:
  - explicit degraded envelope,
  - explicit operator-visible error,
  - explicit mock/paper response only when mock mode is authorized.
- Require a real `market_snapshot` for any production trade-intent path; paper/demo may still synthesize one, but only when explicitly marked.
- Add provenance fields to all relevant responses:
  - `source`
  - `mode`
  - `degraded`
  - `fallback_used`
  - `provider`
  - `venue`
  - `connector_id`
  - `error`
- Move runtime defaults such as `binance`, `BTCUSDT`, or synthetic strategy/account buckets behind explicit operator config or request-level choices.
- Audit all workspace bootstrap/demo seeds so production-ready templates do not normalize mock markets as the default worldview.

### Primary modules

- `nerya/skills/builtin/market_data_skill/actions.py`
- `nerya/skills/builtin/trading_skill/actions.py`
- `nerya/skills/builtin/onchain_skill/actions.py`
- `nerya/skills/_connector_helpers.py`
- `nerya/api/routes_market.py`
- `nerya/core/truth.py`
- `nerya/workspace/bootstrap.py`

### Required verification

```bash
pytest \
  tests/test_runtime_truth_gate.py \
  tests/test_release_truth_gate.py \
  tests/test_no_placeholder_runtime_paths.py \
  -q
```

Add and run focused tests that assert:

- connector failure returns degraded/unavailable result rather than mock data,
- live/provider-missing path does not auto-succeed via mock,
- market/trading/onchain responses expose truth envelope fields,
- runtime defaults are explicit and operator-visible.

### Exit criteria

- No production/runtime hot path silently returns synthetic market or chain data.
- Missing real dependencies become explicit degraded responses.
- Mock/paper success remains available for tests and paper mode, but only as an explicit mode.

### Reject this phase as incomplete if

- any live or production-paper path can still return a successful synthetic ticker, balance, or market snapshot without saying so.

## 7. Phase 2 — Provider and LLM Capability Closure

### Goal

Make provider support claims exact, operator-visible, and strong enough for real production use.

### Current blockers

- capability matrix exists, but many cells are still `experimental`, `metadata-only`, or `unsupported`,
- provider family breadth is wider than provider feature depth,
- runtime truth is better than before but still not enough to claim Hermes-level provider maturity,
- the production story for missing keys, unsupported capabilities, and degraded provider modes needs to stay explicit all the way up the stack.

### Required implementation

- Keep the capability matrix conservative, but close the highest-value gaps first:
  - streaming
  - tool calling
  - tool choice
  - schema/json mode
  - reasoning/thinking
  - multimodal input
  - model discovery
- Add provider-family smoke tests or operator runbooks for each provider family that Nerya claims to support.
- Surface provider capability state in API/dashboard/operator tools rather than only inside tests.
- Distinguish:
  - `supported`
  - `experimental`
  - `metadata-only`
  - `unsupported`
  in runtime responses and operator views.
- Do not upgrade a provider to "supported" based on `/models` or nominal adapter boot alone.
- If OpenAI Responses-style or Hermes-like auxiliary capabilities remain out of scope, document them as out of scope instead of hand-waving parity.

### Primary modules

- `nerya/llm/capability_matrix.py`
- `nerya/llm/model_router.py`
- `nerya/llm/gateway.py`
- `nerya/llm/adapters/`
- `nerya/api/routes_llm.py`
- `docs/llm-gateway.md`

### Required verification

```bash
pytest \
  tests/test_provider_capability_matrix.py \
  tests/test_release_truth_gate.py \
  -q
```

Plus provider-specific smoke or simulation verification for each family Nerya lists.

### Exit criteria

- Provider claims exactly match tested behavior.
- Unsupported provider features fail explicitly.
- Production configuration errors never masquerade as success.

### Reject this phase as incomplete if

- "provider supported" still means only that the adapter imports or lists models.

## 8. Phase 3 — Trigger/Triggle, Routing, and Flexible Data I/O

### Goal

Make Trigger/Triggle the real operator control plane, not just a partial backend capability.

### Current blockers

- scheduler internals now support `cron`, `starts_at`, `ends_at`, and `enabled`,
- but `trigger_skill.add_schedule` still only accepts `every_seconds`,
- route management now exposes real CRUD (`add_route`, `update_route`,
  `pause_route`, `resume_route`, `remove_route`, `apply_routes`) with
  atomic writes and journaling; legacy `manage_routes` remains available
  as a compatibility dispatcher,
- flexible result envelopes exist but need consistent operator use.

### Required implementation

- Make the operator-facing trigger skill and API expose the same scheduler power the runtime already supports:
  - `every_seconds`
  - `cron`
  - `starts_at`
  - `ends_at`
  - `enabled`
- Route management is now operator-managed directly: the
  `trigger` skill exposes `add_route`, `update_route`, `pause_route`,
  `resume_route`, `remove_route`, and `apply_routes`, each persisting
  changes atomically and journalling the delta. Proposal-only is kept
  as a separate surface for self-improvement, but the production
  control plane no longer depends on it.
- Expand route explain and replay surfaces so operators can inspect:
  - why a route matched,
  - which candidates were rejected,
  - why tier was escalated,
  - what skills/subagents were selected,
  - whether the route was replayed or dry-run.
- Standardize flexible result envelopes for:
  - table data
  - paged results
  - partial/streamed chunks
  - blob references
- Remove runtime-facing assumptions that every market/ticker route is `binance + BTCUSDT` unless explicitly requested.

### Primary modules

- `nerya/triggers/schedule.py`
- `nerya/triggers/router.py`
- `nerya/triggers/runtime.py`
- `nerya/skills/builtin/trigger_skill/actions.py`
- `nerya/acp/server.py`
- `nerya/mcp/`
- `nerya/api/routes_market.py`
- `nerya/api/routes_agent.py`
- `nerya/api/routes_triggers.py`

### Required verification

```bash
pytest \
  tests/test_route_explain_and_discovery.py \
  tests/test_trigger_router.py \
  tests/test_trigger_sdk.py \
  -q
```

Add tests that prove the external trigger/control plane can create and inspect all supported schedule fields.

### Exit criteria

- Control-plane capability and runtime scheduler capability match.
- Route resolution is explainable and replayable.
- Data return shapes are not boxed into a single fixed JSON style.

### Reject this phase as incomplete if

- the runtime supports more scheduling or routing power than operators can actually control.

## 9. Phase 4 — Agent Loop, Subagent, and Memory Closure

### Goal

Close the remaining gap between "real runtime exists" and "production-grade autonomous runtime exists".

### Current truth

The agent loop and subagent runtime are real now, but the production bar is higher than "multi-step exists somewhere".

### Required implementation

- Deepen the main turn loop:
  - repeated observe -> think -> act -> observe cycles,
  - mid-turn re-planning after earlier actions mutate context,
  - stable resume/recovery from persisted journal state.
- Deepen subagent contribution surfaces:
  - richer artifact return,
  - stronger parent/child audit links,
  - no isolation leaks,
  - no hidden direct live-trading surface.
- Unify main turn, subagent, review, and evolution session semantics:
  - stable session identity,
  - strategy-scoped vs global memory separation,
  - TTL and compaction,
  - explainable recall budgeting.

### Primary modules

- `nerya/agent/kernel.py`
- `nerya/agent/planner.py`
- `nerya/agent/context_builder.py`
- `nerya/agent/memory.py`
- `nerya/agent/memory_recall.py`
- `nerya/agent/working_memory.py`
- `nerya/subagents/runtime.py`
- `nerya/subagents/dispatcher.py`
- `nerya/strategy_history/`

### Required verification

```bash
pytest \
  tests/test_agent_loop.py \
  tests/test_subagent_runtime_phase3.py \
  tests/test_memory_isolation.py \
  tests/test_memory_recall.py \
  -q
```

### Exit criteria

- one trigger can drive several sequential actions with journal-aware recovery,
- failed child runs do not contaminate unrelated child runs,
- session and memory semantics are consistent across runtime surfaces.

### Reject this phase as incomplete if

- the kernel still behaves like a one-pass planner in important production paths,
- or memory/session semantics still differ by surface.

## 10. Phase 5 — Strategy OS and Trading-Intelligence Closure

### Goal

Make Nerya's trading-native differentiation honestly stronger than a general-purpose agent runtime.

### Current truth

The unique trading features are real, but not all equally mature:

- attribution is real,
- reconciliation is real,
- subagent contribution exists,
- self-improvement is proposal-only and real,
- but some of the intelligence remains rule-based or heuristic-light.

### Required implementation

- Keep strategy lifecycle as the canonical operating path:
  - `draft -> paper -> canary -> live`
  - version IDs
  - promotion records
  - rollback targets
  - environment/account snapshot binding
- Deepen trading-native attribution:
  - parameter sensitivity
  - scenario replay
  - paper/live divergence analysis
  - execution-quality scoring
  - strategy-version comparisons
- Keep self-improvement proposal-only, but feed it richer evidence from:
  - attribution
  - strategy version state
  - indicator state
  - provider/runtime quality
- If the production standard is "native TA-Lib required", then make TA-Lib a production startup requirement rather than an optional accelerator in production environments.

### Primary modules

- `nerya/trading/strategy_lifecycle.py`
- `nerya/trading/strategy_versions.py`
- `nerya/skills/builtin/strategy_skill/actions.py`
- `nerya/strategy_history/attribution.py`
- `nerya/strategy_history/review.py`
- `nerya/strategy_history/explain.py`
- `nerya/agent/self_improvement.py`
- `nerya/data/indicators.py`
- `nerya/data/features.py`

### Required verification

```bash
pytest \
  tests/test_strategy_lifecycle_phase7.py \
  tests/test_attribution_phase8.py \
  tests/test_reflection_evolution.py \
  tests/test_evolution_phase9.py \
  tests/test_indicators_talib.py \
  tests/test_features_indicator_fusion.py \
  -q
```

Add explicit production-mode tests for:

- startup failure when TA-Lib is required but missing,
- paper/live divergence attribution,
- strategy-version comparison and rollback evidence.

### Exit criteria

- Nerya can explain why a trade won, lost, or was missed across strategy, subagent, execution, indicator, and runtime-quality evidence.
- Strategy changes are versioned and rollbackable.
- Self-improvement remains governed and evidence-backed.

### Reject this phase as incomplete if

- trading review still depends on operators manually mining journals for the real cause,
- or TA-Lib is still treated as optional even when the production bar says it must be native.

## 11. Phase 6 — Operator Surfaces, Deployment, and Runbook Closure

### Goal

Make production operation real, not implied.

### Required implementation

- Expose replay, explain, provider capability, degraded truth state, proposals, and trace through operator-grade API/dashboard surfaces.
- Produce a production runbook that covers:
  - environment bootstrap,
  - secret provisioning,
  - provider credential validation,
  - connector validation,
  - paper environment smoke,
  - canary promotion,
  - live kill-switch,
  - rollback.
- Add startup/health checks that fail loudly when mandatory production dependencies are missing:
  - provider keys
  - connector credentials
  - TA-Lib if required by production image
  - service/workspace path assumptions
- Add a release checklist for `prod_paper`, `canary_live`, and `full_live` instead of one generic "release done" checkbox.

### Primary modules

- `nerya/observability/trace.py`
- `nerya/api/routes_agent.py`
- `nerya/api/routes_strategy_history.py`
- `nerya/api/routes_evolution.py`
- `nerya/api/routes_market.py`
- `dashboard/`
- `docs/runbook.md`
- deployment/service scripts

### Required verification

```bash
pytest \
  tests/test_observability_phase10.py \
  tests/test_release_truth_gate.py \
  -q
```

Plus manual/operator evidence:

1. boot service in production-like environment,
2. validate provider/connectors,
3. run a production-paper strategy end-to-end,
4. inspect trace, explain, degraded state, proposal state,
5. promote one strategy to canary with rollback rehearsal.

### Exit criteria

- An operator can explain any run from ingress to outcome.
- The runbook is sufficient to boot, validate, canary, and roll back without code archaeology.

### Reject this phase as incomplete if

- the only way to diagnose a degraded run is reading raw journals on disk.

## 12. Phase 7 — Production Certification and Cutover

### Goal

Turn technical completion into an honest launch decision.

### Required implementation

- Define certification gates for each operating state:

#### Gate A — Production Paper

- real providers configured,
- real connectors reachable,
- no silent mock fallback,
- full explain/replay/trace available,
- at least one strategy completes paper cycle end-to-end in production environment.

#### Gate B — Canary Live

- strategy version pinned,
- promotion record exists,
- rollback target validated,
- canary account/wallet scope isolated,
- live kill switch tested,
- paper/live divergence and execution telemetry visible.

#### Gate C — Full Live

- provider capability matrix reviewed for the active provider set,
- no active `experimental` capability on a business-critical path without explicit operator sign-off,
- operator runbook rehearsal complete,
- strategy performance and risk limits approved,
- incident/rollback path rehearsed.

### Required evidence package

Every gate must produce:

- test results,
- service health evidence,
- provider/connectors validation evidence,
- one end-to-end trace artifact,
- one explain/review artifact,
- one rollback or dry-run rollback artifact,
- explicit unsupported/degraded capability list.

### Exit criteria

- Nerya can be truthfully declared:
  - `production paper-ready`,
  - then `canary live-ready`,
  - then `full live-ready`.

### Reject this phase as incomplete if

- "production ready" still depends on maintainers mentally compensating for known half-real paths.

## 13. Hardcoded, Mock, and Half-Implemented Audit Rules

Use these rules continuously during implementation:

### Acceptable

- mocks in tests,
- mocks in explicit local demo/paper mode,
- sample/demo workspace content that is clearly labeled as sample/demo,
- `proposal_only_unimplemented` for generated assets that are explicitly not runnable.

### Not acceptable on production paths

- hidden fallback from real connector/provider failure to synthetic success,
- runtime defaults that silently choose market/venue/account when configuration is required,
- "operator-facing" control surfaces that only work for an older subset of runtime capability,
- docs that claim parity/support beyond the capability matrix or actual runtime behavior.

### Mandatory recurring grep/audit checks

```bash
rg -n "mock_exchange|mock_chain|MockExchange|fallback_used|proposal_only|BTCUSDT|binance" nerya
rg -n "experimental|metadata-only|unsupported" nerya/llm/capability_matrix.py
rg -n "every_seconds|cron|starts_at|ends_at|enabled" nerya/skills/builtin/trigger_skill nerya/triggers
```

## 14. Final Program-Level Acceptance Criteria

Nerya may only be declared production-aligned when all of the following are true:

1. Hermes is not a runtime dependency and is not needed to run, test, or ship Nerya.
2. Normal runtime paths do not silently return mock/provider-fake data.
3. Provider support claims are evidence-backed and operator-visible.
4. Trigger/Triggle is the actual ingress/control plane, not just a backend abstraction.
5. `SkillRuntime` is the actual capability plane across agent, script, API, CLI, and dashboard.
6. Agent loop and subagent runtime are multi-step, auditable, and recoverable.
7. Strategy lifecycle is versioned, promotable, and rollbackable.
8. Trading review and optimization can attribute outcomes across strategy, indicators, subagents, risk, execution, and runtime quality.
9. Self-improvement remains proposal-only and evidence-backed.
10. Operators can boot, inspect, canary, and roll back the system from documented product surfaces and runbook steps.

## 15. Stop Conditions

Do not mark the program complete if any of the following is still true:

- skill-level silent mock fallback remains in any production/runtime hot path,
- provider support is still partial but documented as parity,
- ACP/operator protocol is still described as full parity while remaining minimal,
- scheduler core supports more than the control plane can actually configure,
- TA-Lib is still optional where production policy says it must be native,
- runtime hot paths still depend on hardcoded default markets/venues/accounts,
- unique trading optimization features still exist only as coarse rule tags without actionable operator/runtime evidence,
- operators still need raw disk/journal archaeology to understand failures.

## 16. Working Rule for Future Contributors

When updating this document:

1. downgrade any claim that is stronger than runtime truth,
2. never move a phase to complete until the verification and operator evidence both exist,
3. if the runtime improves but the operator/control surface does not, keep the phase open,
4. if production policy tightens, update the acceptance criteria first and code second,
5. prefer a truthful smaller claim over an impressive but unstable one.
