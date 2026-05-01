# Nerya Production Readiness / Parity Audit

Status: refreshed against the latest repo state  
Date: 2026-04-23  
Audience: runtime maintainers, SDK owners, dashboard owners, trading/runtime owners  
Goal: answer the current truth, not the previous plan:

1. Did the repo continue implementing the plan?
2. How close is Nerya to Hermes now?
3. How close is Nerya to the useful agent-design ideas in Claude Code `anthropic-ai-claude-code-2.1.88-expanded`?
4. Which "trading-native" features are truly runnable, and which are still partial / decorative / stubbed?
5. What must still be done before Nerya can be honestly claimed as production-runnable?

---

## 1. Audit Method

This refresh intentionally did **not** trust older conclusions.

### 1.1 Code areas re-checked

- `nerya/agent/`*
- `nerya/subagents/`*
- `nerya/triggers/`*
- `nerya/llm/*`
- `nerya/scripts/*`
- `nerya/evolution/*`
- `nerya/api/*`
- `nerya/wallet/providers/*`
- `sdk/python/*`
- `sdk/typescript/*`
- `dashboard/*`
- `README.md`
- `docs/script-system.md`
- `docs/trading-sdk.md`
- `docs/reference-capability-map.md`

### 1.2 External references re-checked

- `../hermes-agent/website/docs/user-guide/features/provider-routing.md`
- `../hermes-agent/website/docs/user-guide/features/cron.md`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/bootstrap/state.ts`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/compact/sessionMemoryCompact.ts`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/AgentTool/built-in/verificationAgent.ts`

### 1.3 Targeted verification executed in this audit

Regression set A:

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
  -q
```

Result:

- `80 passed, 1 skipped in 86.24s`

Regression set B:

```bash
python -m pytest \
  tests/test_agent_loop.py \
  tests/test_scenario_replay.py \
  tests/test_attribution_phase8.py \
  tests/test_self_improvement_evidence.py \
  tests/test_certification_gates.py \
  tests/test_trading_sdk.py \
  tests/test_direct_order_sdk_risk_gate.py \
  tests/test_strategy_version_compare.py \
  -q
```

Result:

- `48 passed in 93.07s`

UI / SDK typecheck:

```bash
cd dashboard
npx tsc --noEmit

cd ../sdk/typescript
npx tsc --noEmit
```

Result:

- both passed with no diagnostics

Additional live probes:

```bash
python -c "import importlib.util; print(importlib.util.find_spec('nerya_sdk'))"
python sdk/python/examples/direct_order_strategy.py
```

Result:

- `find_spec('nerya_sdk') -> None`
- example failed with `ModuleNotFoundError: No module named 'nerya_sdk'`

That last probe materially changes the SDK assessment: the Python SDK examples are currently not runnable exactly as documented.

---

## 2. Executive Verdict

### 2.1 Short answer

1. **Nerya is now a real native runtime.**
  Agent loop, trigger plane, subagent runtime, skill runtime, LLM gateway, strategy history, attribution, scenario replay, and TA-Lib-aware feature fusion are not fake.
2. **The repo did continue implementing the plan.**
  Several previously-audited blockers are now actually closed:
  - `strategy.create(canary)` lifecycle bug is fixed.
  - `portfolio_auditor` now exists in seeded workspace subagents.
  - provider routing now has native code, API routes, and dashboard surface.
  - trigger/schedule operator CRUD is now real, not read-only.
3. **Nerya is still not Hermes parity.**
  It has the core runtime spine, but not Hermes-level platform breadth around cron jobs, job delivery, multi-skill scheduled sessions, and more generalist agent-operating surfaces.
4. **Nerya is still not Claude Code parity.**
  It has session state, compression, and a verification-oriented lane, but not Claude Code's richer global session state, transcript-aware compaction, dedicated verification-agent contract, or broader multi-agent operating model.
5. **The trading-native differentiators are real.**
  Strategy versions, compare/rollback, attribution, scenario replay, direct trading SDK through Risk Gate, and TA-Lib-backed indicators/features are real and regression-covered.
6. **The biggest remaining honesty gap has moved outward.**
  The core runtime is stronger than the outward-facing SDK/script/doc/UI layer. The main blockers now are:
  - broken TypeScript SDK/API alignment,
  - broken Python SDK example execution path,
  - script-system docs/runtime mismatch,
  - self-evolution still exposed through stale proposal-only / invalid generator paths,
  - some wallet/provider surfaces still return stubs while UI/config make them look more complete than they are.

### 2.2 Production interpretation

- `local_dev`: real
- `prod_paper`: close and plausible
- `canary_live`: still not honest yet
- `full_live`: not honest yet

The reason is no longer "the runtime core is fake."  
The reason is "operator surfaces and external entrypoints still overclaim or drift from runtime truth."

---

## 3. Capability Matrix

## 3.1 Nerya vs Hermes


| Capability                                | Current Nerya state                             | Hermes parity                       |
| ----------------------------------------- | ----------------------------------------------- | ----------------------------------- |
| Agent loop                                | native and tested (`tests/test_agent_loop.py`)  | partial-to-strong                   |
| Skill runtime                             | native and real                                 | strong                              |
| Subagent runtime                          | native, registry-backed, concurrency + denylist | partial                             |
| Trigger router                            | native, explain/replay/stats present            | strong                              |
| Schedule lifecycle                        | add/update/pause/resume/run-now/tick/status     | partial                             |
| Provider routing                          | native and operator-configurable                | strong for OpenRouter-style routing |
| Model catalog / provider ops              | native and dashboard-visible                    | partial-to-strong                   |
| Conversational memory compaction          | has token-aware compression + refs              | partial                             |
| Cron as multi-skill scheduled agent jobs  | shipped 2026-04-24 (`session_kind='agent'`, `attached_skills`, `ScheduledSessionRunner`) | partial (scheduled-agent product shape only) |
| Cron delivery targets / messaging fan-out | shipped 2026-04-24 (`delivery_targets` — `messages` + `webhook`, journaled)              | partial (no multi-channel broadcast)         |
| Self-improvement / self-evolution         | evidence-driven but still proposal-first        | partial                             |
| Runtime verification lane                 | strategy-specific verification lane exists      | partial                             |


### 3.1.1 Important Hermes parity nuance — closed (2026-04-24)

Hermes cron is a generalist scheduled-agent system:

- natural-language or cron schedule creation,
- pause/resume/edit/run/remove,
- multiple attached skills,
- delivery targets,
- fresh agent sessions.

Nerya schedules used to be trigger emitters only. As of 2026-04-24 the
scheduled-agent product shape has shipped under
`[docs/plans/2026-04-24-hermes-parity-cron-session.md](2026-04-24-hermes-parity-cron-session.md)`:

- `ScheduleEntry` supports `session_kind`, `attached_skills`,
  `delivery_targets`, `session_ttl_seconds` (backwards compatible
  defaults for every existing schedule).
- `nerya/triggers/scheduled_session.py` spawns a fresh single-turn
  agent session per tick for `session_kind='agent'` entries, with the
  pinned skill whitelist forwarded to `AgentKernel.run_turn`.
- `nerya/triggers/delivery.py` fans the turn result out to
  `messages` (via the MessagePipeline) and `webhook` targets, with a
  per-delivery journal row.
- `trigger_skill.add_schedule_from_text` exposes bounded
  natural-language → schedule creation: a deterministic parser handles
  the common cadences and the light LLM tier handles the rest, with
  the `add_schedule` schema as the final gate.
- The dashboard triggers page renders a "mode" column, the attached
  skills, delivery targets, and a dedicated NL input that calls
  `scheduleAddFromText`.
- The release gate lives in `tests/test_hermes_parity_cron_session.py`
  and runs alongside the 2026-04-23 production gate.

That closes **scheduled-agent product parity** in the sense this
section defined it. Full platform-breadth parity with Hermes
(multi-channel fan-out, multi-tenant isolation, broader NL planner) is
deliberately still out of scope; it is not a production blocker.

## 3.2 Nerya vs Claude Code expanded


| Capability                                                            | Current Nerya state              | Claude Code parity    |
| --------------------------------------------------------------------- | -------------------------------- | --------------------- |
| Session state file                                                    | yes (`nerya/agent/session.py`)   | partial               |
| Preserved per-skill state                                             | yes (`skill_state`)              | partial               |
| Turn/session listing and inspection                                   | yes                              | partial               |
| Context compression                                                   | yes (`nerya/llm/compression.py`) | partial               |
| Transcript-aware compaction with message/tool invariants              | no equivalent depth              | no                    |
| Large global bootstrap state / session latches / plugin/channel state | no                               | no                    |
| Dedicated verification agent with hard contract                       | no exact equivalent              | no                    |
| Static verification-oriented lane                                     | yes (`verification_lane`)        | partial idea adoption |
| General-purpose team / agent operating model                          | no                               | no                    |


### 3.2.1 Practical judgment

Nerya has adopted **selected design ideas** from Claude Code:

- session index,
- preserved skill state,
- context compression,
- verification as a first-class concern.

It has **not** adopted Claude Code's full operating model:

- no built-in verification agent with an independent PASS/FAIL contract,
- no comparable transcript compactor that preserves tool/result invariants,
- no large persistent process/session state map,
- no broad team/session lifecycle comparable to Claude Code's general-purpose coding-agent runtime.

So the correct statement is:

**Nerya is inspired by Claude Code in some agent-operating patterns, but it is not design-parity with Claude Code.**

---

## 4. What Is Real Today

These features should be treated as real, not decorative:

### 4.1 Core runtime

- Agent loop (`tests/test_agent_loop.py`)
- Subagent runtime and policy denylist (`tests/test_subagent_runtime_phase3.py`)
- Skill runtime / permission gating
- Trigger emit / dry-run / result wait / explain / replay
- Schedule CRUD lifecycle

### 4.2 Trading-native differentiation

- Direct trading SDK through Risk Gate (`tests/test_trading_sdk.py`, `tests/test_direct_order_sdk_risk_gate.py`)
- Strategy versioning + compare (`tests/test_strategy_version_compare.py`)
- Attribution (`tests/test_attribution_phase8.py`)
- Scenario replay (`tests/test_scenario_replay.py`)
- Certification gate execution (`tests/test_certification_gates.py`)
- TA-Lib-aware indicator and feature fusion (`tests/test_indicators_talib.py`, `tests/test_features_indicator_fusion.py`)

### 4.3 LLM/operator control plane

- Provider readiness surface
- Tier listing
- Model catalog refresh/list
- OpenRouter-style provider routing
- Dashboard-facing LLM ops panel

### 4.4 Frontend/operator improvements that are real

- strategy scaffold/create flow now uses discovery-backed accounts/wallets/statuses/drivers
- triggers page now performs schedule lifecycle actions
- dashboard now pulls real portfolio/strategy/trade/equity/candle data instead of defaulting to seeded sparklines

---

## 5. Findings

Findings are ordered by production impact.

### 5.1 BLOCKER — external SDK surface is still not production-usable as claimed

#### Evidence

TypeScript SDK:

- `sdk/typescript/src/client.ts` still targets:
  - `http://127.0.0.1:17211`
  - `/trading/submit_intent`
  - `/trading/cancel_order`
  - `GET /strategy/history?...`
  - `/strategy/explain_trade`
- actual HTTP API exposes:
  - `POST /trading/submit`
  - `POST /trading/cancel`
  - `POST /trading/history`
  - `POST /strategy/history`
  - `POST /strategy/explain`
- dashboard/runtime defaults point to `8787`, while service install docs talk about `18317`

Python SDK:

- `sdk/python/examples/direct_order_strategy.py` imports `from nerya_sdk import connect`
- repo-level direct execution failed with `ModuleNotFoundError: No module named 'nerya_sdk'`
- there is no install metadata under `sdk/python/` in this repo snapshot, only source files
- README and runbook still tell operators to run:
  - `python sdk/python/examples/price_tracker.py`
  - `python sdk/python/examples/direct_order_strategy.py`

#### Verdict

The core runtime may be real, but the external SDK story is still not honest:

- TypeScript SDK compiles, but its routes and base URL are stale.
- Python SDK examples are documented as runnable, but do not run as documented.

#### Required closure

1. Make one canonical API port story.
2. Align TS SDK routes and example payloads with actual HTTP routes.
3. Either package/install the Python SDK properly or make docs/examples use a correct `PYTHONPATH` / editable install flow.
4. Add SDK smoke tests that hit a live local server, not just `tsc`.

### 5.2 BLOCKER — script-system documentation overclaims what approved scripts can do

#### Evidence

Docs claim scripts can:

- call `client.trading.submit_intent(...)`
- call `client.llm.`*

Actual runtime behavior:

- `nerya/scripts/script_context.py` only whitelists read-only skill actions from:
  - `market_data`
  - `onchain`
  - `news_social`
- `nerya/scripts/runner.py` injects a constrained `ctx` and explicitly says it is for whitelisted skill actions with "no wallet / order / LLM paths"
- scenario evidence already records `PermissionError` when a script tries `trading.submit_trade_intent`

#### Verdict

Right now "script strategy can trade / call llm" is **not true in the sandboxed approved-script runtime**.

The truthful current state is:

- approved scripts can call a narrow read-only `ctx.skill_call(...)` surface,
- scripts do get an `LLMSession` budget object internally, but the sandboxed operator script path is **not** exposing an actual `client.llm.`* facade,
- trading/LLM script claims in docs are ahead of runtime truth.

#### Required closure

Pick one and make the repo consistent:

1. **Narrow script model**:
  keep scripts read-only + trigger-emitting only, and rewrite docs/manifests/examples accordingly.
2. **Broader script model**:
  actually inject a safe SDK facade for trading/LLM into approved scripts, then cover it with static analyzer, runtime policy, and tests.

Until that is done, production docs must stop claiming script-level trading/LLM capability.

### 5.3 HIGH — self-evolution is still partial at the agent-facing surface

#### Evidence

Positive progress:

- `nerya/evolution/skill_generator.py` now has a real `scaffold_skill(...)` path for runnable scaffolds.

But the agent-facing evolution skill still routes through legacy proposal-only entrypoints:

- `nerya/skills/builtin/evolution_skill/actions.py`
  - `generate_script_proposal -> propose_script`
  - `generate_skill_proposal -> propose_skill`

And the legacy script generator is stale:

- `nerya/evolution/script_generator.py` still emits:
  - `from nerya_sdk import Client`
  - `Client.local()`

That does not match the current Python SDK shape in this repo.

#### Verdict

The underlying evolution primitives improved, but the **agent-accessible self-evolution path is still only partially upgraded**.

Skill generation:

- underlying runnable scaffold exists,
- agent-facing evolution action still uses proposal-only skill generation.

Script generation:

- still emits stale skeleton code.

#### Required closure

1. Expose runnable scaffold generation through `evolution_skill`.
2. Replace stale script skeletons with a current runtime-compatible scaffold.
3. Add truth-gate tests that generated script/skill artifacts actually boot or fail with explicit, intended proposal markers.

### 5.4 HIGH — some wallet/provider paths are still surfaced more broadly than their real capability

#### Evidence

- `nerya/wallet/providers/self_custody.py`
  - `quote()` returns a best-effort stub with note `"self_custody quote is a stub"`
  - `swap()` returns `ok=False` and instructs the caller to use connectors / auto-swap path
- `nerya/wallet/providers/templates/ts_wallet_skill.js`
  - `balance`, `quote`, and `swap` are stub template responses
- generic chain connectors still intentionally do not provide full exchange-style order/ticker semantics:
  - `nerya/connectors/evm_native.py`
  - `nerya/connectors/solana_native.py`

#### Verdict

This is not "feature absent everywhere."  
It is "some provider choices are still scaffolds / partial implementations while the surrounding operator surface can make them look closer to ready than they really are."

#### Required closure

1. Add capability truth to provider/wallet surfaces:
  - `quote_supported`
  - `swap_supported`
  - `stub`
  - `production_ready`
2. Hide or clearly mark non-production-ready providers in the dashboard.
3. Stop treating dependency-readiness alone as equivalent to execution readiness.

### 5.5 MEDIUM — frontend/operator panel is much improved, but still not fully aligned

#### Evidence

Still cosmetic or local-only:

- `dashboard/components/TopHeader.tsx`
  - strategy selector is explicitly marked `cosmetic, global scope stub`
- `dashboard/app/settings/page.tsx`
  - timezone is hardcoded to `utc+8`
  - language is hardcoded to `en`
  - market stream is local-only `basic|standard|pro`
- `dashboard/components/LlmOpsPanel.tsx`
  - says provider routing is saved to `~/.nerya/llm_routing.yml`
  - actual runtime writes `workspace/llm/provider_routing.json`

#### Verdict

The operator panel is no longer mostly fake, but it is still **not fully runtime-truthful**:

- some pages are real control plane,
- some settings are still local UI scaffolding,
- some labels/documentation drift from the runtime path.

#### Required closure

1. Every visible setting must either:
  - persist to runtime truth, or
  - be labeled explicitly as local UI preference only.
2. Remove or wire the cosmetic global strategy selector.
3. Fix path/documentation drift in LLM ops text.

### 5.6 MEDIUM — port/story drift still exists across README, CLI, dashboard, and SDKs

#### Evidence

- `nerya/api/local_server.py` defaults to `8787`
- `dashboard` proxy defaults to `8787`
- `nerya serve` defaults to `8787`
- `service install` defaults to `18317`
- README talks about `18317`
- TypeScript SDK uses `17211`

#### Verdict

This is not a runtime-core bug, but it is a serious operator and integration bug.  
A production system needs one clear port model, or explicit separation between:

- dev port,
- local daemon port,
- service port,
- dashboard proxy target.

Right now the repo exposes all four ideas at once.

---

## 6. Hardcoded / Hard Enum Audit

## 6.1 Hardcoded values that should stay

These are safety or contract enums, not harmful rigidity:

- strategy lifecycle states
- strategy drivers (`prompt`, `script`, `manual`)
- provider routing keys
- script skill allowlist
- subagent denylist for live-trading-critical skills
- risk/approval gate thresholds and required permission names

The goal is **not** to remove explicit safety boundaries.

## 6.2 Hardcoded values that still harm flexibility or truthfulness

- TypeScript SDK default base URL `17211`
- TypeScript SDK old route names
- settings page hardcoded timezone/language choices as if they were runtime state
- local-only market stream enum with no runtime capability tie
- stale generated script skeleton using `Client.local()`
- demo/example commands that imply runnable Python SDK without packaging/install path

## 6.3 Current judgment

Nerya has already removed a lot of earlier harmful hardcoding through discovery endpoints and runtime-backed operator surfaces.  
The remaining hardcoding problem is now mostly:

- **external surface drift**
- **UI cosmetics presented like runtime state**
- **legacy generator/examples not updated with the new runtime truth**

---

## 7. Real vs Partial vs Stub

### 7.1 Real and production-relevant now

- native agent loop
- subagent dispatch
- skill runtime
- trigger router
- schedule lifecycle
- provider routing
- strategy version compare/rollback
- attribution
- scenario replay
- TA-Lib indicator/fusion path
- direct Risk-Gated trade submission

### 7.2 Partial

- Hermes parity as a whole
- Claude Code parity as a whole
- self-evolution end-to-end
- approved-script runtime capabilities vs docs
- wallet/provider production readiness truth
- frontend/operator parity

### 7.3 Explicit stubs / scaffolds still present

- self-custody quote
- self-custody swap surface
- TS wallet skill template
- legacy generated script skeleton
- proposal-only skill proposal path
- cosmetic top-header strategy selector

---

## 8. Production Closure Plan

The following phases are ordered so that after implementation the repo can make an honest production claim.

## Phase 1 — External Surface Truth

### Goal

Make every outward-facing SDK/doc example actually runnable against the current runtime.

### Required changes

- Align `sdk/typescript/src/client.ts` with actual HTTP routes.
- Pick one default port story and apply it consistently across:
  - local server,
  - CLI docs,
  - dashboard proxy,
  - TS SDK README,
  - installer/service docs.
- Make Python SDK installable or documented with a real invocation path.
- Add SDK smoke tests:
  - Python example boot
  - TypeScript client against live local server

### Exit criteria

- README commands run as written.
- Python example imports succeed from documented invocation.
- TS SDK can submit a direct intent to a local live server.
- CI contains at least one SDK smoke test per language.

## Phase 2 — Script Runtime Truth

### Goal

Remove the current ambiguity around what approved scripts are allowed to do.

### Required changes

- Choose narrow or broad script capability model.
- Align:
  - `docs/script-system.md`
  - `docs/trading-sdk.md`
  - script manifest semantics
  - `nerya/scripts/runner.py`
  - `nerya/scripts/script_context.py`
  - examples and tests

### Exit criteria

- There is one truthful statement for approved-script capability.
- Docs, manifests, runner, allowlist, and tests all agree.
- A passing scenario proves the intended capability.
- A denied scenario proves forbidden capability is really blocked.

## Phase 3 — Self-Evolution Closure

### Goal

Ensure the agent-facing self-evolution path no longer dead-ends into stale or proposal-only artifacts unless that is explicitly intended.

### Required changes

- Expose runnable skill scaffolding through `evolution_skill`.
- Replace stale script proposal skeleton.
- Separate clearly:
  - review-only proposal
  - runnable scaffold
- Add tests for generated outputs.

### Exit criteria

- `evolution_skill` can generate:
  - a review-only proposal intentionally,
  - a runnable scaffold intentionally.
- No generated default artifact references dead APIs or non-existent SDK entrypoints.

## Phase 4 — Provider / Wallet Capability Honesty

### Goal

Prevent provider choices from looking production-ready when they still contain stubs.

### Required changes

- Add capability metadata to wallet/provider surfaces.
- Mark stub providers as experimental in API + dashboard.
- Either implement real self-custody quote/swap path or hide that path behind explicit experimental status.
- Keep templates clearly separated from shipped runtime capability.

### Exit criteria

- Operator UI can tell:
  - installed,
  - dependency-ready,
  - execution-ready,
  - experimental/stub.

## Phase 5 — Frontend Operator Truth

### Goal

Ensure every visible operator control either hits the runtime or is explicitly local-only.

### Required changes

- Persist or relabel settings page items.
- Remove or wire the top-header strategy selector.
- Fix LLM ops storage-path text.
- Audit remaining cosmetic controls and local state.

### Exit criteria

- No runtime-looking control is a cosmetic stub.
- No persistence-looking control only mutates ephemeral local state without saying so.

## Phase 6 — Honest Production Gate

### Goal

Move from "runtime mostly works" to "repo can honestly claim production-ready agent operation."

### Required changes

- Add release truth checks for:
  - SDK examples,
  - script runtime claims,
  - provider capability honesty,
  - no stale route/path docs.
- Keep targeted regression suites for:
  - agent loop,
  - subagents,
  - trigger/schedule control plane,
  - trading SDK,
  - attribution/replay,
  - TA-Lib indicator path.

### Exit criteria

- `prod_paper` is fully honest.
- `canary_live` has no outward-surface drift on the claimed path.
- docs/examples/operator panel/SDKs all match the code actually shipping.

### Implementation status (2026-04-24)

Phases 1-6 are now code-complete and pinned by regression tests:

- Phase 1 — `tests/test_sdk_smoke.py` (TS SDK routes + 8787 port + Python SDK
packaging/examples).
- Phase 2 — `tests/test_script_context.py`, `tests/test_script_sandbox.py`
(narrow script model, read-only whitelist, trading/LLM paths refused).
- Phase 3 — `tests/test_evolution_scaffold_phase3.py` (runnable skill
scaffold via `evolution_skill`, legacy `Client.local()` removed from
`script_generator` output).
- Phase 4 — `tests/test_wallet_capabilities_phase4.py` (every wallet
provider declares `WalletCapabilities`; API + dashboard carry
`installed` / `stability` / per-method capability cells).
- Phase 5 — timezone / language / market-stream persisted via
`UiSettings`; cosmetic top-header strategy selector removed;
`LlmOpsPanel` now documents the real `workspace/llm/provider_routing.json`
storage path; settings rows labelled as dashboard-only UI preferences
when they do not persist to runtime.
- Phase 6 — `tests/test_production_gate_phase6.py` aggregates the above
as a single honest-production gate covering external surface truth,
script runtime truth, self-evolution closure, provider/wallet honesty,
frontend operator truth, and the regression-suite coverage this phase
requires.
- Phase 6 — `tests/test_agent_prompt_driven_e2e.py` proves the operator
contract end-to-end without any handwritten Python: skill scaffold via
`evolution_skill.generate_skill_scaffold`, trigger route via
`trigger_skill.add_route`, natural-language schedule via
`trigger_skill.add_schedule_from_text`, a live `AgentKernel.run_turn`
with subagents and Risk Gate, and a full schedule lifecycle
(create/pause/resume/remove) on a scheduled-agent entry. It is wired
into `scripts/run_truth_gate.sh` so it can't silently regress.

`canary_live` remains gated by operator config (`live_trading_enabled`,
signed approvals); the production-gate truth checks themselves no
longer produce outward-surface drift on the claimed path.

---

## 9. Final Answer to the User's Questions

### Did the repo follow the plan?

**Yes, materially.**  
The runtime core advanced a lot and several earlier blockers are now actually fixed.

### Is Agent capability aligned with Hermes?

**No, not fully.**  
Core runtime boundaries are largely there. Full Hermes product parity is not.

### Is Agent design aligned with Claude Code expanded?

**No, not fully.**  
Some design ideas were adopted; the broader operating model was not.

### Are the unique trading-native features decorative?

**Mostly no.**  
They are real and tested.

### Are there still mock / half-implemented / stale areas?

**Yes.**  
But they are now concentrated in the outward-facing layer:

- external SDKs,
- script-system truth,
- self-evolution entrypoints,
- wallet/provider capability honesty,
- a few frontend/control-plane surfaces.

### Can Nerya be honestly claimed as production-runnable today?

**As a native runtime core: yes.**  
**As a fully aligned production product surface: not yet.**

The remaining work is not "build the runtime from scratch."  
It is "make every external/operator-facing promise match the runtime that now exists."

### Closure update (2026-04-24)

The six-phase closure plan in Section 8 has now been implemented:

1. Phase 1 — external SDK surfaces align with the runtime routes and use
  the canonical `8787` local port; Python SDK ships as the installable
  `nerya_sdk` package.
2. Phase 2 — approved scripts are pinned to the narrow, read-only
  `ScriptContext` model; trading/LLM claims in docs match runtime.
3. Phase 3 — `evolution_skill` exposes `generate_skill_scaffold`;
  generator output no longer references `Client.local()`.
4. Phase 4 — every wallet provider declares `WalletCapabilities`;
  `list_providers` / `readiness_report` expose `installed` +
  `stability` + per-method capability cells; the dashboard shows them.
5. Phase 5 — timezone / language / market-stream preferences are
  persisted via `UiSettings` and clearly labelled as dashboard-only UI
  preferences; cosmetic strategy selector is gone; `LlmOpsPanel` points
  at the real `workspace/llm/provider_routing.json`.
6. Phase 6 — `tests/test_production_gate_phase6.py` locks the above
  in place as a single release gate; the audit-listed regression
  suites still pass.

The outward-facing layer no longer overclaims relative to the runtime.
`canary_live` and `full_live` still require operator-signed approvals
and `live_trading_enabled` — that gating is intentional, not drift.