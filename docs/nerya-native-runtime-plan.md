# Nerya native runtime plan

Status: re-audited after latest Nerya update  
Decision: **Nerya is the runtime; Hermes is reference material only.**  
Audience: maintainers, runtime contributors, dashboard contributors, operator tooling contributors

## 1. Decision freeze

This plan locks the direction for the repository:

- Nerya will not attach to Hermes as its production runtime.
- Hermes remains reference material for behaviors, boundaries, and maturity targets.
- Nerya must own its own:
  - agent loop
  - subagent runtime
  - session and memory model
  - LLM gateway and provider routing
  - Trigger/Triggle orchestration plane
  - skill runtime and SDK boundary
  - trading kernel
  - strategy lifecycle
  - review, optimization, reflection, and evolution pipeline
- External-agent ingress is now `nerya/mcp/` and `nerya/acp/`, not a Hermes bridge layer.

## 2. Current audit snapshot

### 2.1 Clearly completed

- **Phase 0 completed**
  - `README.md` states that Nerya owns its own runtime.
  - `docs/nerya-architecture.md` no longer models Hermes as an attached kernel.
  - the old `nerya/adapter/` package is gone.
  - `tests/test_architecture_audit.py` contains native-runtime guardrails.
- **Phase 1 completed**
  - `docs/runtime-ownership.md` exists as a runtime-boundary ADR.
  - import-boundary rules are documented and tested.

### 2.2 Implemented but still incomplete

- **Phase 2 partial**
  - `nerya/agent/kernel.py` now records `turn_id`, explicit turn steps,
  per-step budget attribution, and `stopped_reason`.
  - still missing a full iterative think/act/observe loop with re-planning.
- **Phase 3 partial**
  - `nerya/subagents/dispatcher.py` now supports `dispatch_many`,
  concurrency, budget caps, denylists, and `SubAgentResult`.
  - `nerya/subagents/runtime.py` is still a single-call analysis runtime.
- **Phase 4 partial**
  - `nerya/agent/working_memory.py` now provides per-turn scratch memory.
  - persisted session/memory semantics remain lighter than the target model.
- **Phase 5 partial**
  - `nerya/triggers/` has expanded ingress primitives:
  `user_command`, `dry_run`, `dead_letter`, `stats`, `inbox`, `price`.
  - replay/explain/operator tooling around Trigger/Triggle is still thin.
- **Phase 6 partial**
  - `SkillRuntime` remains the main capability fan-out point.
  - contract versioning and full surface parity across API/CLI/dashboard are incomplete.
- **Phase 7 partial**
  - `nerya/trading/strategy_lifecycle.py` exists.
  - `strategy_skill.set_status` enforces `draft -> paper -> canary -> live`.
  - strategy versioning and promotion bundles are still missing.
- **Phase 8 partial**
  - `nerya/strategy_history/attribution.py` exists.
  - `nerya/trading/reconciliation.py` exists.
  - attribution is still rule-based and narrower than the target engine.
- **Phase 9 partial**
  - proposal guardrails exist.
  - attribution bundles can seed proposal creation.
  - `agent/self_improvement.py` is still heuristic-light and narrow.
- **Phase 10 partial**
  - `nerya/observability/trace.py` exists.
  - release-without-Hermes tests exist.
  - operator-grade replay/explain surfaces are still incomplete.

### 2.3 Targeted test evidence from this audit

- `tests/test_architecture_audit.py`: `24 passed`
- `tests/test_agent_loop.py`
`tests/test_subagent_runtime_phase3.py`
`tests/test_reflection_evolution.py`
`tests/test_observability_phase10.py`: `26 passed`
- `tests/test_strategy_lifecycle_phase7.py`
`tests/test_attribution_phase8.py`
`tests/test_evolution_phase9.py`: `15 passed`

### 2.4 Revised priority

The roadmap is no longer "build everything from zero". The immediate priority is:

1. finish the partially implemented runtime phases:
  Phases 2, 3, 4, 5, 6
2. deepen the partially implemented product phases:
  Phases 7, 8, 9, 10
3. avoid reopening Phases 0 and 1 unless a boundary regression appears

## 3. Current-state grounding

The current repository already contains a real Nerya-native runtime spine:

- `nerya/agent/kernel.py` — main turn orchestration
- `nerya/skills/runtime.py` — skill dispatch, journaling, caller context
- `nerya/llm/gateway.py` + `nerya/llm/model_router.py` — tiered LLM access
- `nerya/triggers/router.py` + `nerya/triggers/cron.py` — trigger routing and scheduling
- `nerya/skills/builtin/trading_skill/actions.py` + `nerya/trading/risk.py` — intent -> risk -> approval -> execution
- `nerya/skills/builtin/strategy_skill/actions.py` + `nerya/trading/strategy_lifecycle.py` — strategy lifecycle baseline
- `nerya/skills/builtin/strategy_review_skill/actions.py` + `nerya/strategy_history/review.py` — review baseline
- `nerya/strategy_history/attribution.py` — attribution baseline
- `nerya/trading/reconciliation.py` — reconciliation baseline
- `nerya/observability/trace.py` — trace reconstruction baseline
- `nerya/agent/self_improvement.py` + `nerya/evolution/`* — proposal-oriented evolution baseline
- `nerya/mcp/` + `nerya/acp/` — external agent protocol ingress

The current repository does not contain a Hermes runtime bridge package, and
that is now intentional architecture rather than a missing feature.

## 4. End-state definition of done

Nerya reaches the target state only when all of the following are true:

1. The repository can run, test, and ship without Hermes code or installation.
2. Every external action reaches the world through a Nerya-owned boundary:
  `TriggerEvent`, `SkillRuntime`, `SDK`, `RiskGate`, `ApprovalGate`,
   `ExecutionEngine`, `MessagingPipeline`, `Proposal`.
3. The agent loop is multi-step and stateful, not a single LLM call plus one
  dispatch pass.
4. Subagents are real child runtimes with isolation, budget, and audit.
5. Trigger/Triggle is the only orchestration plane for schedule, script,
  gateway, webhook, market, and review events.
6. Strategy state, review state, and evolution state are first-class runtime
  concepts, not scattered file conventions.
7. Trading optimization can attribute outcomes to trigger quality, subagent
  quality, risk policy, execution quality, and strategy configuration.
8. All self-improvement remains proposal-only and cannot mutate protected live
  scopes directly.
9. Operator observability can explain any run from ingress event to trade,
  review, proposal, and rollback.

## 5. Capability classification

### 5.1 Must reach Hermes parity

These are general autonomous-agent runtime capabilities that Nerya should
match in maturity, but implement natively:

- agent turn loop
- subagent and delegation runtime
- session and memory handling
- provider routing and failover
- cron and scheduler behavior
- gateway and messaging session handling
- script sandbox and bounded execution
- turn-level audit and recovery

### 5.2 Must exceed Hermes

These are Nerya-specific differentiators and should become stronger than any
generalist agent runtime:

- Trigger/Triggle orchestration plane
- skill-first trading boundary
- Risk Gate and Approval Gate
- strategy lifecycle and versioning
- strategy history and explainability
- paper/live divergence analysis
- trade review and optimization attribution
- reflection and proposal-driven self-improvement

## 6. Global constraints

These constraints apply to every phase:

- No raw exchange, wallet, signer, or platform token access from agent context.
- No direct live-trading bypass around `RiskGate` or `ApprovalGate`.
- No agent-authored mutation of protected scopes outside proposal flow.
- No second capability surface that bypasses skills and SDKs.
- No temporary Hermes runtime dependency added back into hot paths.
- Docs and tests must describe the actual implementation, not future intent.

## 7. Phase roadmap

## Phase 0 — Truth reset and direction freeze

Status after latest audit: **completed**

### Goal

Make the repository tell the truth: Nerya is the runtime, Hermes is only a
reference map.

### Delivered already

- README and architecture docs now say Nerya runs natively.
- external ingress is documented as MCP/ACP.
- the old adapter-based story is gone from the code layout.
- architecture guardrail tests exist.

### Exit criteria

- A new contributor can read the docs and correctly conclude that Nerya runs natively.
- Hermes is documented only as a reference source, not a required runtime.

## Phase 1 — Runtime boundary and ownership model

Status after latest audit: **completed**

### Goal

Lock the core Nerya runtime boundaries and ownership rules before adding more capability depth.

### Delivered already

- `docs/runtime-ownership.md` defines the authoritative call graph.
- module ownership is documented package by package.
- import-boundary rules are test-enforced.

### Exit criteria

- No module outside `llm/` talks to model providers directly.
- No module outside `security/` resolves or handles raw secrets.
- The runtime call graph is stable enough to deepen features without moving boundaries again.

## Phase 2 — Native turn engine v2

Status after latest audit: **partial**

### Goal

Upgrade the main agent from a one-pass decision runner into a true Nerya-native multi-step turn engine.

### Already achieved

- explicit `TurnStep` journaling exists
- `turn_id`, `step_id`, `stopped_reason`, per-step budget and wall time exist
- max-step and budget-stop behavior exists

### Remaining target

- introduce repeated observe -> re-think -> act cycles
- allow mid-turn re-planning after earlier actions mutate state
- support resumable turn continuation from persisted turn state
- make recovery journal-aware, not just journal-recording

### Primary modules

- `nerya/agent/kernel.py`
- `nerya/agent/planner.py`
- `nerya/agent/output_parser.py`
- `nerya/harness/`*
- `nerya/llm/gateway.py`

### Exit criteria

- One trigger can cause several sequential skill actions within the same turn.
- The kernel can re-think after earlier actions, not only consume one fixed `actions[]` list.
- Failure of one step does not corrupt the whole run state.
- `test_agent_loop.py` covers iterative execution and recovery.

## Phase 3 — Real subagent runtime

Status after latest audit: **partial**

### Goal

Promote subagents from lightweight analyzers into isolated child runtimes.

### Already achieved

- `dispatch_many` exists
- concurrency exists
- `SubAgentResult` envelopes exist
- budget and denylist controls exist

### Remaining target

- make subagents true child runtimes with multiple internal steps
- allow safe use of allowed skills inside the child runtime
- record richer contribution signals than cost/count alone
- preserve isolation while supporting child artifacts

### Primary modules

- `nerya/subagents/runtime.py`
- `nerya/subagents/dispatcher.py`
- `nerya/subagents/result_aggregator.py`
- `nerya/subagents/context_policy.py`
- `nerya/subagents/registry.py`

### Exit criteria

- At least two independent subagents can run concurrently in one parent turn.
- A subagent can perform more than one internal step.
- A failed child no longer collapses unrelated child execution.
- Contribution data is rich enough for later optimization attribution.

## Phase 4 — Native session and memory model

Status after latest audit: **partial**

### Goal

Unify session state, memory, strategy-session artifacts, and recall policy under one Nerya-owned model.

### Already achieved

- per-turn working memory exists
- memory files and strategy learnings already exist
- reflection writes strategy/global memory artifacts

### Remaining target

- define persisted session identity for main turn, child turn, review, and evolution
- unify strategy memory, review memory, and evolution memory semantics
- formalize TTL, compaction, and recall budgeting
- guarantee cross-strategy isolation in all memory surfaces

### Primary modules

- `nerya/agent/memory.py`
- `nerya/agent/memory_recall.py`
- `nerya/agent/working_memory.py`
- `nerya/agent/context_builder.py`
- `nerya/strategy_history/`*

### Exit criteria

- Main agent, subagent, review, and evolution all read/write the same session semantics.
- Memory is bounded, explainable, and strategy-scoped when needed.
- Working memory and persisted memory have clearly separated responsibilities.

## Phase 5 — Trigger/Triggle orchestration plane

Status after latest audit: **partial**

### Goal

Make Trigger/Triggle the only ingress and orchestration plane for runtime work.

### Already achieved

- triggers now cover more ingress shapes
- dry-run exists
- cooldown, dedupe, rate-limit, dead-letter logic exist

### Remaining target

- unify every external path into a documented ingress matrix
- add operator-facing route explain and replay surfaces
- expose replay/debug flows through API and dashboard
- make Trigger/Triggle the obvious control plane for all automation

### Primary modules

- `nerya/triggers/event.py`
- `nerya/triggers/router.py`
- `nerya/triggers/runtime.py`
- `nerya/triggers/cron.py`
- `nerya/triggers/routes.py`
- `nerya/sdk/trigger_api.py`

### Exit criteria

- Every runtime entry path lands as a `TriggerEvent`.
- Route decisions are explainable and replayable.
- Scheduler behavior is deterministic and side-effect free except event emission.
- API/dashboard surfaces can inspect and debug routes.

## Phase 6 — Skill runtime and SDK unification

Status after latest audit: **partial**

### Goal

Make `SkillRuntime` and Nerya SDKs the single capability surface for agents,
scripts, API, CLI, and dashboard.

### Already achieved

- `SkillRuntime` is the main capability fan-out point
- skill manifests and actions are first-class contracts
- most runtime flows already dispatch through skill actions

### Remaining target

- add stronger contract versioning and capability tags
- remove any remaining bypassed or duplicated call paths
- align API, CLI, and dashboard semantics to the exact same skill boundary
- improve surface tracing and caller attribution

### Primary modules

- `nerya/skills/runtime.py`
- `nerya/skills/registry.py`
- `nerya/skills/permissions.py`
- `nerya/sdk/`*
- `nerya/api/`*
- `nerya/cli/*`

### Exit criteria

- No supported runtime path bypasses `SkillRuntime`.
- Scripts, CLI, API, and dashboard use the same capability boundary.
- Skill contracts are stable enough for operator tooling and automation.

## Phase 7 — Strategy OS and lifecycle v2

Status after latest audit: **partial**

### Goal

Turn strategies into first-class runtime entities with versioned lifecycle,
not just folders plus prompts.

### Already achieved

- `canary` exists as a first-class lifecycle state
- transition rules are validated at module level and skill surface
- canonical promotion path now includes `draft -> paper -> canary -> live`

### Remaining target

- add strategy version ids and promotion records
- add rollback to named strategy versions
- bind strategy state to routes, prompts, environment, and account/wallet snapshots
- support side-by-side comparison between strategy versions

### Primary modules

- `nerya/trading/strategy_lifecycle.py`
- `nerya/skills/builtin/strategy_skill/actions.py`
- `nerya/trading/strategies.py`
- `nerya/trading/accounts.py`
- `nerya/strategy_history/`*

### Exit criteria

- A strategy can move cleanly from draft to paper to canary to live.
- A strategy change has a version, a promotion record, and a rollback target.
- Runtime components no longer rely on implicit defaults hidden in directory layout.

## Phase 8 — Trading-native optimization engine

Status after latest audit: **partial**

### Goal

Build the Nerya-specific optimization layer that goes beyond generic agent
parity and makes trading performance explainable and improvable.

### Already achieved

- rule-based attribution exists
- root-cause taxonomy exists
- proposal seeds can be emitted from attribution bundles
- reconciliation exists

### Remaining target

- add subagent contribution attribution
- add parameter sensitivity and scenario replay
- add paper/live divergence analysis
- deepen execution analysis beyond coarse slippage/latency thresholds
- compare backtest, canary, and live outcomes

### Primary modules

- `nerya/strategy_history/attribution.py`
- `nerya/skills/builtin/strategy_review_skill/actions.py`
- `nerya/strategy_history/review.py`
- `nerya/strategy_history/explain.py`
- `nerya/trading/reconciliation.py`
- `nerya/data/features.py`
- `nerya/trading/paper.py`

### Exit criteria

- The system can answer why a trade won, lost, or was missed.
- The system can separate strategy logic problems from execution problems.
- Optimization output is rich enough to guide proposals without manual log mining.

## Phase 9 — Evidence-driven reflection and evolution

Status after latest audit: **partial**

### Goal

Upgrade self-improvement from lightweight heuristics to a governed,
evidence-backed proposal pipeline.

### Already achieved

- protected-scope enforcement exists
- proposal lifecycle artifacts exist
- attribution bundles can already seed proposal creation

### Remaining target

- replace narrow heuristics with broader evidence mining
- feed optimization outputs directly into proposal ranking
- improve proposal rationale, rollback plans, and test-plan generation
- make self-improvement consume the richer Phase 8 surfaces

### Primary modules

- `nerya/agent/self_improvement.py`
- `nerya/agent/reflection.py`
- `nerya/evolution/`*
- `nerya/strategy_history/attribution.py`

### Exit criteria

- Every generated change is proposal-only and evidence-backed.
- Proposal quality is sufficient for operator review without manual journal digging.
- Self-improvement can target prompts, triggers, strategy config, scripts, and skills without bypassing governance.

## Phase 10 — Operator surfaces, observability, and release closure

Status after latest audit: **partial**

### Goal

Make the runtime operable in production and close the Hermes-decoupling effort.

### Already achieved

- trace reconstruction exists
- release-without-Hermes tests exist
- backend observability is stronger than before

### Remaining target

- expose replay, explain, and trace through product surfaces
- add operator-grade API/dashboard views for proposals and trace
- make observability end-to-end, not journal-first only
- close documentation after runtime behavior stabilizes

### Primary modules

- `nerya/observability/trace.py`
- `nerya/core/devmode.py`
- `nerya/api/`*
- `dashboard/`*
- `docs/runbook.md`

### Exit criteria

- An operator can trace any trigger from ingress to final outcome.
- The runtime can be tested and shipped without Hermes code present.
- Docs, tests, and runtime behavior are aligned.

## 8. Revised execution order after the latest audit

### Immediate lane A — runtime deepening

- finish Phase 2
- finish Phase 3
- finish Phase 4
- finish Phase 5
- finish Phase 6

### Immediate lane B — productizing what is already partially built

- deepen Phase 7 after Phase 6 contracts stabilize
- deepen Phase 8 once Phase 7 produces trustworthy strategy/runtime state
- deepen Phase 9 only after Phase 8 produces richer attribution evidence
- deepen Phase 10 continuously, but close it last

## 9. Quality gates

- Phase 0: documentation truth gate
- Phase 1: module-boundary gate
- Phase 2: iterative turn-engine gate
- Phase 3: child-runtime gate
- Phase 4: session-memory gate
- Phase 5: trigger-orchestration gate
- Phase 6: skill-surface gate
- Phase 7: strategy-versioning gate
- Phase 8: optimization-attribution gate
- Phase 9: evidence-backed proposal gate
- Phase 10: release-without-Hermes and operator-surface gate

## 10. Immediate execution plan

If work resumes now, the recommended sequence is:

1. finish Phase 2 by adding iterative re-think/re-plan semantics to the main kernel
2. finish Phase 3 by making subagents real child loops with allowed skill use
3. finish Phase 4 by formalizing persisted session and memory semantics
4. finish Phase 5 and Phase 6 together so Trigger/Triggle, SDK, API, CLI, and dashboard all land on one capability boundary
5. deepen Phase 7 with strategy versioning and promotion records
6. deepen Phase 8 with richer attribution, replay, and paper/live divergence
7. deepen Phase 9 so evolution consumes real optimization evidence
8. close Phase 10 with operator-grade replay/explain/trace surfaces

## 11. Final success statement

This roadmap is complete only when Nerya is recognizably a standalone
skill-first, trading-native autonomous runtime:

- Hermes-inspired in capability maturity
- Nerya-native in runtime implementation
- stronger than Hermes in trading, optimization, and governed evolution
- auditable and operable without hidden side paths