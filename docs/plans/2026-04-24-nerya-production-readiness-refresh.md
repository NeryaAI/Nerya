# Nerya Production Readiness Refresh

Status: refreshed against current repo state  
Date: 2026-04-24  
Audience: runtime maintainers, SDK owners, dashboard owners, strategy/runtime owners  
Purpose: answer the current truth after the latest project update, then turn that truth into a production-closure plan.

---

## 1. Scope

This refresh re-checks four separate questions:

1. Is Nerya now genuinely following the previously stated plan?
2. Is Nerya actually aligned with Hermes and Claude Code expanded, or only aligned in selected ideas?
3. Which trading-native features are real, which are bounded, and which are still stubbed/mock?
4. What must still be implemented before Nerya can be honestly claimed as production-runnable?

This document does **not** trust the 2026-04-23 audit as ground truth. It uses that audit only as a baseline and then re-verifies current code.

---

## 2. Verification Run In This Refresh

### 2.1 Code areas re-checked

- `nerya/agent/*`
- `nerya/subagents/*`
- `nerya/triggers/*`
- `nerya/llm/*`
- `nerya/scripts/*`
- `nerya/evolution/*`
- `nerya/api/*`
- `nerya/wallet/*`
- `nerya/workspace/*`
- `sdk/python/*`
- `sdk/typescript/*`
- `dashboard/*`
- `README.md`
- `docs/runbook.md`
- `docs/trading-sdk.md`
- `docs/script-system.md`
- `docs/llm-gateway.md`
- `docs/reference-capability-map.md`

### 2.2 Reference material re-checked

- `../hermes-agent/website/docs/user-guide/features/cron.md`
- `../hermes-agent/website/docs/user-guide/features/provider-routing.md`
- `docs/plans/2026-04-24-hermes-parity-cron-session.md`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/bootstrap/state.ts`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/compact/sessionMemoryCompact.ts`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/AgentTool/built-in/verificationAgent.ts`

### 2.3 Verification commands

Focused runtime/trading/evolution suite:

```bash
python -m pytest \
  tests/test_trigger_sdk.py \
  tests/test_script_context.py \
  tests/test_strategy_driver_schema.py \
  tests/test_strategy_skill.py \
  tests/test_strategy_lifecycle_phase7.py \
  tests/test_llm_ops_surfaces.py \
  tests/test_subagent_runtime_phase3.py \
  tests/test_indicators_talib.py \
  tests/test_features_indicator_fusion.py \
  tests/test_agent_loop.py \
  tests/test_scenario_replay.py \
  tests/test_attribution_phase8.py \
  tests/test_self_improvement_evidence.py \
  tests/test_certification_gates.py \
  tests/test_trading_sdk.py \
  tests/test_direct_order_sdk_risk_gate.py \
  tests/test_strategy_version_compare.py \
  tests/test_skill_scaffold.py \
  tests/test_evolution_scaffold_phase3.py \
  -q
```

Result:

- `143 passed, 1 skipped in 264.34s`

SDK + production-truth gates:

```bash
python -m pytest tests/test_sdk_smoke.py tests/test_production_gate_phase6.py -q
```

Result:

- `30 passed in 3.10s`

Typecheck:

```bash
cd dashboard && npx tsc --noEmit
cd ../sdk/typescript && npx tsc --noEmit
```

Result:

- both passed with no diagnostics

Python SDK example probe:

```bash
python sdk/python/examples/direct_order_strategy.py
```

Result:

- script now starts correctly from repo root
- current workspace rejects the trade because strategy `btc_momentum` is not present:
  `strategy_unknown: unknown strategy: btc_momentum`

That means the **import/runtime path is fixed**, but the example still assumes a seeded strategy context.

---

## 3. Executive Verdict

### 3.1 Short answer

1. **Nerya is a real native runtime.**
   The core is not fake: agent loop, subagents, triggers, schedules, strategy history, attribution, scenario replay, provider routing, and TA-Lib-aware indicator fusion are real and regression-covered.
2. **The repo did continue implementing the plan.**
   Several items that were blockers on 2026-04-23 are now genuinely closed.
3. **Nerya is still not Hermes parity.**
   It has strong runtime pieces, but it still does not have Hermes's scheduled general-purpose agent-session product shape.
4. **Nerya is still not Claude Code expanded parity.**
   It adopts selected ideas, but it still lacks Claude Code's broader session-state model, transcript-preserving compaction discipline, and dedicated verification-agent contract.
5. **The trading-native differentiators are mostly real.**
   Strategy versions, attribution, scenario replay, direct trading SDK through Risk Gate, and TA-Lib-backed indicators are real and test-backed.
6. **The main remaining honesty gap has moved again.**
   It is no longer mainly "runtime core missing". It is now:
   - documentation truth drift,
   - public SDK contract fragmentation,
   - wallet/on-chain execution partials,
   - hardcoded routing heuristics that still over-steer the agent,
   - structural parity gaps vs Hermes and Claude Code.

### 3.2 Production interpretation

- `local_dev`: yes
- `prod_paper`: close, but not yet fully honest
- `canary_live`: no
- `full_live`: no

Why:

- `prod_paper` is close because the kernel, review paths, provider routing, trigger plane, and paper-trading stack are real.
- It is not fully honest yet because docs and SDK surfaces still do not define one clear operator contract.
- `canary_live` and `full_live` are still blocked by wallet/on-chain execution partials plus missing end-to-end live evidence packaging.

---

## 4. What Changed Since The 2026-04-23 Audit

These items were real blockers before and are now effectively closed:

### 4.1 Closed blocker: TypeScript SDK route and port drift

Current truth:

- `sdk/typescript/src/client.ts` now defaults to `http://127.0.0.1:8787`
- it now targets current routes:
  - `/trading/submit`
  - `/trading/cancel`
  - `/trading/history`
  - `/strategy/history`
  - `/strategy/explain`

This is also locked by `tests/test_sdk_smoke.py` and `tests/test_production_gate_phase6.py`.

### 4.2 Closed blocker: Python example import path

Current truth:

- `sdk/python/examples/_bootstrap.py` fixes repo-root execution
- `pyproject.toml` now exposes `nerya_sdk` as a package
- `python sdk/python/examples/direct_order_strategy.py` no longer fails with `ModuleNotFoundError`

Remaining nuance:

- example bootability is fixed
- example business-context assumptions are not fully self-contained yet

### 4.3 Closed blocker: script-system docs/runtime mismatch

Current truth:

- `docs/script-system.md` now matches the narrow sandboxed script model
- `nerya/scripts/script_context.py` and `nerya/scripts/runner.py` still enforce read-only `ctx.skill_call(...)`
- docs now explicitly separate sandboxed approved scripts from non-sandboxed external `nerya_sdk` callers

### 4.4 Closed blocker: evolution scaffold path

Current truth:

- `nerya/skills/builtin/evolution_skill/actions.py` now exposes `generate_skill_scaffold`
- `nerya/evolution/skill_generator.py` stages a runnable scaffold
- `nerya/evolution/script_generator.py` no longer emits `Client.local()`
- this is covered by `tests/test_evolution_scaffold_phase3.py`

### 4.5 Closed blocker: dashboard/operator truth on major surfaces

Current truth:

- trigger/schedule CRUD is real in API and dashboard
- `TopHeader` no longer contains the cosmetic global strategy selector
- strategies page uses discovery-backed accounts, wallets, statuses, and drivers
- `LlmOpsPanel` now points at the real `workspace/llm/provider_routing.json`

---

## 5. Current Capability Truth

### 5.1 Real and production-meaningful today

| Capability | State | Evidence |
|---|---|---|
| Agent loop | real | `tests/test_agent_loop.py` |
| Subagent runtime | real | `tests/test_subagent_runtime_phase3.py`, `nerya/subagents/*` |
| Trigger routing | real | `nerya/triggers/router.py`, `nerya/api/routes_triggers.py` |
| Schedule lifecycle CRUD | real | `nerya/triggers/cron.py`, `dashboard/app/triggers/page.tsx` |
| Provider routing | real | `nerya/llm/provider_routing.py`, `nerya/api/routes_llm.py`, `dashboard/components/LlmOpsPanel.tsx` |
| Strategy history / explain / review | real | `nerya/api/routes_strategy_history.py` |
| Attribution | real | `tests/test_attribution_phase8.py` |
| Scenario replay | real | `tests/test_scenario_replay.py` |
| Strategy versioning and compare | real | `tests/test_strategy_version_compare.py` |
| Direct trade intent through Risk Gate | real | `tests/test_trading_sdk.py`, `tests/test_direct_order_sdk_risk_gate.py` |
| TA-Lib indicator fusion | real | `tests/test_indicators_talib.py`, `tests/test_features_indicator_fusion.py` |
| SDK truth gates | real | `tests/test_sdk_smoke.py`, `tests/test_production_gate_phase6.py` |

### 5.2 Real, but bounded or deliberately narrower

| Capability | State | Current boundary |
|---|---|---|
| Approved scripts | real, narrow | read-only `ctx.skill_call(...)`, no trading/LLM inside sandbox |
| Evolution | real, operator-gated | proposals plus runnable skill scaffold, not autonomous self-apply |
| Dashboard settings | real, mixed | some runtime-backed, some local browser UI preference only |
| Python SDK | real | in-process and public package, but not the same public surface as TS SDK |
| TS SDK | real | current routes/port fixed, but narrower than Python/internal SDK |
| Wallet/provider catalog | real | capability metadata is honest, but not every provider is fully executable |

### 5.3 Explicit stubs / mock / partials still present

| Surface | State | Evidence |
|---|---|---|
| `self_custody.quote()` | stub | `nerya/wallet/providers/self_custody.py` |
| `self_custody.swap()` | stub/partial | `nerya/wallet/providers/self_custody.py` |
| TS wallet skill template | stub template | `nerya/wallet/providers/templates/ts_wallet_skill.js` |
| Workspace bootstrap defaults | demo/bootstrap bias | `nerya/workspace/manager.py` |
| Mock/paper data modes | explicit and allowed in certain modes | `nerya/core/truth.py`, `nerya/ops/preflight.py` |

The key point is: these stubs are no longer hidden. Capability metadata and preflight checks now surface several of them honestly. The remaining problem is that some docs and plans still talk as if those surfaces were more complete than they are.

---

## 6. Findings

Findings are ordered by operational impact.

### 6.1 BLOCKER - documentation truth drift is now the biggest honesty problem

The most serious current problem is no longer missing code. It is repo text that still describes old or broader behavior.

#### Evidence

- `docs/trading-sdk.md` still says local HTTP uses `POST /trading/intent`
- `nerya/skills/builtin/onchain_skill/skill.yml` still says "Uses mock chain by default"
- `docs/reference-capability-map.md` still:
  - marks Hermes cron/schedules as `implemented` without clarifying the scheduled-agent-session gap
  - claims `onchain_skill` exposes `get_balance`, `simulate_swap`, `prepare_signed_tx`, `broadcast_tx`, which is not the current skill surface
- `docs/llm-gateway.md` shows one `client.llm.*` surface without distinguishing internal in-process SDK from public `nerya_sdk` and TS HTTP SDK

#### Impact

- Operators can overestimate production readiness.
- External integrators can code against stale routes or non-public SDK methods.
- Capability documents can incorrectly suggest Hermes parity where only boundary inspiration exists.

#### Required closure

1. Normalize docs and skill descriptions to current runtime truth.
2. Separate internal SDK examples from public SDK examples.
3. Update the capability map so "implemented" means product-parity honest, not just boundary exists.

### 6.2 HIGH - public SDK contract is still fragmented

The worst SDK drift from yesterday is fixed, but there is still no single canonical external contract.

#### Evidence

- public Python SDK (`sdk/python/nerya_sdk/client.py`) exposes:
  - triggers
  - trading
  - strategy
  - messages
  - `llm.classify`, `llm.extract_json`, `llm.analyze_signal`, `llm.compress`
- TypeScript SDK (`sdk/typescript/src/client.ts`) exposes:
  - triggers
  - trading
  - strategy
  - `llm.classify`, `llm.extractJson`
- internal in-process SDK (`nerya/sdk/llm_api.py`) exposes more than the public Python facade, including `generate_script_proposal`

#### Impact

- different integration paths see different capability surfaces
- docs can easily drift because "client" no longer means one thing
- parity claims like "external SDK aligned" are still too strong

#### Required closure

1. Define one public SDK matrix:
   - public Python SDK
   - public TypeScript SDK
   - HTTP API
2. Explicitly mark which surfaces are internal-only.
3. Add one generated or shared contract source so TS/Python/docs do not drift separately.

### 6.3 HIGH - wallet and on-chain execution are still only partially production-grade

The wallet plane is much more honest than before, but several providers still stop at capability skeletons.

#### Evidence

- `nerya/wallet/providers/self_custody.py`
  - `quote()` returns a stub note
  - `swap()` still returns `ok=False` for the real path and points callers elsewhere
- `nerya/wallet/providers/templates/ts_wallet_skill.js`
  - `balance`, `quote`, `swap` are template stubs

#### Impact

- Nerya can honestly claim wallet/provider capability introspection.
- It still cannot honestly claim that all exposed wallet providers are production-ready execution paths.

#### Required closure

1. Pick at least one production-grade on-chain execution path and fully implement it.
2. Demote or hide the remaining provider templates from "ready" operator flows.
3. Keep the capability metadata visible so operators can tell real from partial.

### 6.4 HIGH - hardcoded routing heuristics still over-constrain the agent

This is the part closest to your requirement that the agent should infer more and rely less on enumerated paths.

#### Evidence

- `nerya/core/config.py` uses static `match` lists and `escalate_high_on.text_contains` keyword lists to route or escalate user intents
- `nerya/subagents/registry.py` hardcodes subagent-to-skill mappings
- `nerya/workspace/manager.py` seeds default routes, strategies, and subagents around a fixed bootstrap worldview (`paper_main`, `btc_momentum`, fixed trigger routes)

#### Current judgment

Not every enum here is bad:

- risk states
- lifecycle states
- permission names
- denylist/allowlist policy

should stay explicit.

The harmful part is where natural-language routing still depends on fixed keyword buckets or demo-world assumptions instead of inference or config-driven planners.

#### Required closure

1. Replace keyword-based escalation with a classifier or structured planner decision.
2. Move bootstrap/demo seeds behind an explicit example-workspace flag or template choice.
3. Keep hard safety enums, but reduce hardcoded world-model routing.

### 6.5 MEDIUM - frontend is now mostly truthful, but not yet fully dynamic

#### Evidence

- `dashboard/lib/settings.ts` still hardcodes `KlineVenue` as a union of specific venues even though the runtime venue catalog is dynamic
- `MarketStreamPreference` is still local UI-only and not tied to runtime capability
- some settings are intentionally browser-only, which is fine, but the type layer is still partly static

#### Impact

- dashboard truth is much better than yesterday
- frontend flexibility still lags runtime extensibility

#### Required closure

1. Make venue typing capability-driven or string-backed instead of fixed union-backed.
2. Keep local-only settings explicitly local-only.
3. Avoid static UI types that silently lag registry discovery.

### 6.6 MEDIUM - Hermes and Claude Code parity are still structural gaps, not finishing polish

#### Hermes gap

Current Nerya schedules are still trigger emitters:

- `ScheduleEntry` and `CronScheduler` drive `kind`, cadence, `target`, `strategy_id`, payload
- they do **not** yet create a fresh agent session with:
  - prompt
  - attached skills
  - delivery targets

That gap is already scoped in:

- `docs/plans/2026-04-24-hermes-parity-cron-session.md`

#### Claude Code gap

Nerya currently has:

- session files
- invoked-skill state
- recovery/open turns
- compression with dropped-reference store
- verification lane prompt

It still does **not** have Claude Code expanded equivalents for:

- broader bootstrap/global process state
- transcript-aware compaction preserving tool/result pairing invariants at the same level
- dedicated verification agent with a hard PASS/FAIL verification contract

---

## 7. Hardcoded / Mock Inventory

### 7.1 Hardcoded and explicit boundaries that should stay

- lifecycle states
- driver enums
- permission names
- risk gate thresholds
- script sandbox allowlist
- subagent denylist / bounded skill scopes
- provider routing schema keys

These are contracts or safety controls, not bad rigidity.

### 7.2 Hardcoded or pre-seeded behavior that should be reduced

- `nerya/core/config.py`
  - keyword-triggered escalation and static planner route buckets
- `nerya/workspace/manager.py`
  - default `paper_main`
  - default `btc_momentum`
  - fixed example trigger routes
- `dashboard/lib/settings.ts`
  - fixed `KlineVenue` union
  - UI-only `MarketStreamPreference`

### 7.3 Mock/stub surfaces that must remain clearly labeled

- mock/paper data modes
- self-custody quote/swap placeholders
- TS wallet skill template
- proposal-only skill scaffolds
- example/demo workspace seeds

---

## 8. Production Closure Plan

This plan is split into:

- what is required before Nerya is honestly production-runnable
- what is required before Nerya can honestly claim Hermes parity
- what is required before Nerya can honestly claim Claude Code-inspired parity

### Phase 1 - Documentation Truth Normalization

**Goal**

Make the repo text match the runtime again.

**Files**

- `docs/trading-sdk.md`
- `docs/reference-capability-map.md`
- `docs/llm-gateway.md`
- `nerya/skills/builtin/onchain_skill/skill.yml`
- `README.md` if any wording still overstates parity

**Must achieve**

- remove stale `/trading/intent`
- remove "Uses mock chain by default"
- make the on-chain capability map match the real skill surface
- distinguish internal SDK vs public SDK surfaces
- reword Hermes cron parity to "schedule lifecycle implemented, scheduled-agent-session parity open"

**Exit criteria**

- docs diff reviewed
- `tests/test_sdk_smoke.py` green
- `tests/test_production_gate_phase6.py` green
- a new doc-truth regression test is added for the onchain skill description and capability-map claims

### Phase 2 - Public SDK Contract Unification

**Goal**

Define one honest external integration contract.

**Files**

- `sdk/python/nerya_sdk/*`
- `sdk/typescript/src/*`
- `nerya/api/routes_*.py`
- `docs/trading-sdk.md`
- `docs/llm-gateway.md`

**Must achieve**

- publish a public capability matrix for:
  - Python SDK
  - TypeScript SDK
  - HTTP API
- decide whether TS should grow toward Python, or Python should narrow toward TS
- mark internal-only APIs explicitly
- make examples self-contained enough to run against a freshly initialized workspace, or document the required bootstrap strategy explicitly

**Exit criteria**

- generated SDK matrix checked in
- one cross-SDK smoke test asserts expected surface parity/difference intentionally
- examples run from documented commands with documented prerequisites only

### Phase 3 - Agent Flexibility And Inference Path Cleanup

**Goal**

Reduce harmful hardcoded routing and demo-world assumptions.

**Files**

- `nerya/core/config.py`
- `nerya/subagents/registry.py`
- `nerya/workspace/manager.py`
- `dashboard/lib/settings.ts`

**Must achieve**

- replace keyword escalation lists with classifier or structured intent routing
- keep safety allowlists but reduce keyword-based lane forcing
- split example workspace seeding from production workspace initialization
- make dashboard venue state string/capability-driven rather than fixed-union driven

**Exit criteria**

- no routing decision is blocked on a hardcoded English keyword list alone
- fresh production workspace can initialize without forcing `btc_momentum` worldview
- UI venue selection can represent any discovered venue from `/market/venues`

### Phase 4 - Wallet / On-Chain Execution Closure

**Goal**

Turn at least one wallet path from capability metadata into real production execution.

**Files**

- `nerya/wallet/providers/self_custody.py`
- `nerya/wallet/providers/templates/ts_wallet_skill.js`
- dashboard wallet/integration surfaces

**Must achieve**

- choose one primary production path:
  - self-custody
  - aggregator-backed path
  - TS wallet subprocess path
- implement real quote + swap flow for that path
- mark all others explicitly as partial/template
- ensure operator UI does not present stub paths as production-ready

**Exit criteria**

- live quote and swap smoke test passes for one supported chain
- preflight distinguishes production-ready wallet path from partial/template paths
- dashboard badges match execution truth

### Phase 5 - Hermes Cron / Session Parity

**Goal**

Close the scheduled-agent-session gap, not full Hermes parity.

**Reference**

- `docs/plans/2026-04-24-hermes-parity-cron-session.md`

**Must achieve**

- schedules can spawn fresh agent sessions
- schedules can attach multiple skills
- schedules can declare delivery targets
- optional NL-to-schedule entrypoint stays schema-bounded

**Exit criteria**

- the dedicated Hermes cron/session parity plan is completed
- parity gate for scheduled sessions exists and is green

### Phase 6 - Claude Code-Inspired Verification / Session Improvements

**Goal**

Close the biggest remaining Claude Code-inspired operating-model gaps that matter to reliability, not branding.

**Files**

- `nerya/agent/session.py`
- `nerya/llm/compression.py`
- verification-lane and release-gate surfaces

**Must achieve**

- formalize a verification agent contract or equivalent PASS/FAIL evidence bundle
- tighten compaction invariants so tool/result-safe transcript preservation is explicit
- broaden session state only where it improves recovery and auditability

**Exit criteria**

- verification contract is machine-checkable
- transcript compaction invariants are test-covered
- session/recovery docs explain what is preserved and why

### Phase 7 - Production Rehearsal And Evidence Package

**Goal**

Prove that the implemented surfaces can really operate in production, not just compile and pass unit tests.

**Files**

- `docs/runbook.md`
- production evidence package directory/checklist

**Must achieve**

For `prod_paper`:

- initialize clean workspace
- configure real providers/connectors
- pass `/ops/preflight?mode=prod_paper`
- run one full strategy cycle
- produce explain, attribution, and review artifacts

For `canary_live`:

- choose the real wallet/exchange path
- pass `/ops/preflight?mode=canary_live`
- rehearse kill-switch and rollback
- collect divergence evidence

For `full_live`:

- repeat with signed policy and two-operator release evidence

**Exit criteria**

- `docs/runbook.md` exactly matches the commands that were executed
- evidence artifacts exist for each gate
- no production claim remains based only on unit tests

---

## 9. Honest Status After This Refresh

### 9.1 Required before honest `prod_paper`

- Phase 1
- Phase 2
- Phase 3
- Phase 7 `prod_paper` rehearsal

### 9.2 Required before honest `canary_live` / `full_live`

- Phase 4
- Phase 7 live rehearsals

### 9.3 Required before honest Hermes parity claim

- Phase 5

### 9.4 Required before honest Claude Code-inspired parity claim

- Phase 6

---

## 10. Final Judgment

Nerya is no longer a fake shell around a missing runtime. That question is closed.

The current truth is:

- the native agent/trading runtime is real,
- several old readiness blockers are genuinely fixed,
- the unique trading-native features are mostly real and usable,
- the remaining work is now about truthfulness, public contract clarity, execution-path completeness, and structural parity claims.

So the correct statement today is:

**Nerya is a real Nerya-native trading agent runtime with strong paper/runtime foundations, but it is not yet honest to claim full production readiness, Hermes parity, or Claude Code-expanded parity.**

The shortest path to honest production is not "rewrite the core again".  
It is:

1. fix doc truth,
2. unify the public SDK contract,
3. remove harmful routing hardcoding,
4. close one real wallet/on-chain execution path,
5. run the production evidence package end-to-end.
