# Notes: Nerya, Hermes, and Evolver Self-Learning Comparison

## Sources

### Nerya
- `Nerya/AGENTS.md`
- `Nerya/docs/memory-reflection-evolution.md`
- `Nerya/nerya/agent/*`
- `Nerya/nerya/evolution/*`
- `Nerya/nerya/strategies/*`
- `Nerya/nerya/strategy_history/*`

### Hermes
- `hermes-agent/AGENTS.md`
- `hermes-agent/run_agent.py`
- `hermes-agent/hermes_state.py`
- `hermes-agent/agent/*`
- `hermes-agent/tools/*`
- `hermes-agent/cron/*`
- `hermes-agent/plugins/memory/*`

### Evolver
- `evolver/README.md`
- `evolver/SKILL.md`
- `evolver/src/*`
- `evolver/assets/gep/*`
- `evolver/memory/*` if present

## Findings

### Nerya current implementation
- `AGENTS.md` defines Nerya as a skill-first, trading-native, self-evolving runtime and requires all agent-authored strategy/script/skill/config changes to go through `nerya.evolution.PatchProposal`; live trading remains gated by `risk_gate` and `approval_gate`.
- `nerya/agent/memory.py:26` keeps a whitelist of global memory files, and `Memory.append_global` / `Memory.append_strategy_learning` append durable notes to global or per-strategy memory.
- `nerya/agent/kernel.py:1403` runs an after-turn evolution hook through `maybe_propose_from_turn`; `kernel.py:1517` has optional after-turn memory writes; `kernel.py:1602` and `kernel.py:1646` perform periodic/session-end compaction.
- `nerya/agent/session_search.py:4` explicitly ports the Hermes-style idea of searchable session recall and uses `session_search_fts` when FTS5 is available.
- `nerya/agent/memory_index.py:82` defines structured fact records and `MemoryIndex`, with a `search` method at `memory_index.py:240`; this is closer to a fact index than a full evolution-asset store.
- `nerya/evolution/reflection_engine.py:33` to `:258` scans strategy losses, bad triggers, slippage, stale data, subagent disagreement, overtrading, missed opportunities, attribution, and paper/live divergence; `run_reflection` writes global and per-strategy learning notes.
- `nerya/evolution/runner.py:1` states the workspace-wide evolution tick only creates a `learning_update` proposal; `runner.py:36` ranks reflection seeds and creates the proposal.
- `nerya/agent/self_improvement.py:178` still contains the lightweight per-turn no-op detector; it emits `learning_update` proposals when 9/10 recent turns are no-ops.
- `nerya/evolution/patch_proposal.py:17` defines protected scopes including risk limits, accounts, vault, live-trading toggles, signer/approval policy, and trigger rate limits; `patch_proposal.py:54` allows kinds such as `learning_update`, `skill_proposal`, `core_feature_proposal`, `strategy_package_proposal`, and `strategy_tuning_proposal`.
- `nerya/strategies/evolution.py:1` implements the per-strategy self-evolution loop. `StrategyEvolutionRunner.run_once` snapshots performance, asks a `strategy_tuner` subagent, drops unsafe/unaccepted changes, writes a review markdown, and creates a `strategy_tuning_proposal` rather than applying it.
- `nerya/strategies/performance.py:7` says tuning evidence comes from the same ledgers/dashboard data that operators inspect.
- `nerya/api/routes_strategies_runtime.py:239` exposes tuning generate/schedule/pause/resume/run/status/snapshot endpoints; `dashboard/lib/clientApi.ts:1720` and `dashboard/app/strategies/[id]/page.tsx` consume those strategy tuning surfaces.
- `dashboard/lib/clientApi.ts:1247` exposes evolution proposal/reflect/rank/evidence APIs; the dashboard has proposal counts and a settings memory-vector surface, but self-evolution evidence, assets, validation, and outcome learning are not a single coherent operator workbench.

### Hermes reference patterns
- `hermes-agent/agent/memory_provider.py:1` defines a pluggable `MemoryProvider` abstraction with built-in memory plus one external provider to avoid tool-schema bloat.
- `memory_provider.py:16` lists lifecycle hooks: initialize, system prompt block, prefetch, sync turn, tool schemas, tool handling, shutdown; later optional hooks include turn start, session end, pre-compress, delegation, and memory write.
- `hermes-agent/agent/memory_manager.py:178` prefetches all providers; `memory_manager.py:197` queues background prefetch; `memory_manager.py:249` dispatches provider tool calls; `memory_manager.py:271` through `:331` centralizes lifecycle hooks.
- `hermes-agent/hermes_state.py:93` creates an FTS5 `messages_fts` table with triggers; `hermes_state.py:1016` performs FTS search with sanitization and fallback behavior.
- `hermes-agent/tools/session_search_tool.py:1` documents the long-term conversation recall flow: FTS search, group by session, load context, summarize with a cheaper model, return focused summaries.
- `hermes-agent/agent/trajectory.py:30` saves completed/failed trajectories as JSONL; `agent/insights.py:95` turns session history into usage, cost, and tool-usage insights.
- `hermes-agent/tools/registry.py:176` registers tool schema, handler, toolset, `check_fn`, and `requires_env`; `registry.py:258` exposes only tools whose checks pass; `registry.py:371` returns available toolsets for UI/config.
- `hermes-agent/cron/scheduler.py:695` runs scheduled jobs through `AIAgent` and initializes `SessionDB` so cron sessions are also searchable.
- Hermes' useful lesson for Nerya is lifecycle breadth and context hygiene, not trading strategy evolution. Nerya should port the hook discipline and recall UX, while keeping its trading-native proposal/risk boundary.

### Evolver reference patterns
- `evolver/README.md:41` describes Evolver as a GEP-powered self-evolution engine; `README.md:109` to `:112` says each run scans `memory/`, selects Gene/Capsule assets, emits a protocol-bound GEP prompt, and writes an `EvolutionEvent`.
- `README.zh-CN.md:158` explicitly says Evolver is a prompt generator, not a code modifier; `README.zh-CN.md:180` warns loop mode is background self-maintenance, not a real-time assistant for a currently running host agent.
- `README.md:197` to `:202` highlights auto-log analysis, repair guidance, GEP assets, mutation/personality protocol, configurable strategies, and signal deduplication.
- `README.md:430` to `:437` distinguishes prompt assembly, selector, and solidify; validation commands are gated by `isValidationCommandAllowed`.
- `README.md:447` to `:451` stages external Gene/Capsule candidates and requires local review/validation before promotion.
- `src/gep/assetStore.js:108` defines default Genes; `assetStore.js:169` to `:172` defines `genes.json`, `capsules.json`, and `events.jsonl`; `assetStore.js:349` and `:360` upsert/append assets under file locks.
- `src/gep/signals.js:520` runs multi-layer signal extraction; `signals.js:555` deduplicates over-processed signals; `signals.js:593` detects saturation and injects exploration/steady-state signals.
- `src/adapters/claudeCode.js:8`, `src/adapters/cursor.js:8`, and `src/adapters/codex.js:15` show host-runtime hook patterns for session start, post-edit signal detection, and session end.
- `scripts/gep_append_event.js:45` validates `EvolutionEvent` shape including `mutation_id`, `personality_state`, `blast_radius`, and bounded outcome score.
- Evolver's strongest reusable idea is not direct integration, but a Nerya-native asset loop: Signal -> Gene-like rule -> Capsule-like validated case -> Event audit trail -> proposal/validation/promotion.

### Gap synthesis
- Nerya has durable memory, reflection finders, proposals, and per-strategy tuning, but lacks a first-class evolution-asset layer equivalent to Evolver's Genes/Capsules/Events.
- Nerya has some after-turn/session-end hooks, but lacks a Hermes-like central memory/evolution provider lifecycle with pre-turn recall, post-turn sync, pre-compress learning extraction, session-end harvesting, delegation observation, and memory-write observation.
- Nerya reflection is mostly deterministic and trading-ledger based; it needs a broader signal extractor covering user corrections, repeated tool failures, proposal outcomes, validation failures, skill/script friction, and dashboard/operator decisions.
- Nerya proposals are safe, but proposal outcomes are not yet systematically fed back into memory/assets as reusable lessons.
- Strategy tuning exists, but validation is too reliant on the tuner recommendation/review artifact. It needs auto-generated validation plans, backtest/shadow/canary gates, and promotion evidence attached to the proposal.
- Existing dashboard surfaces expose pieces of this system, but operators lack a single self-evolution workbench showing signals, assets, events, proposals, validation status, and promotion history.
