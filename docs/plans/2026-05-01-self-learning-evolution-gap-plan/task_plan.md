# Task Plan: Nerya Self-Learning and Self-Evolution Upgrade

## Goal
Compare Nerya, Hermes, and Evolver from code-level evidence, then produce a concrete implementation plan to improve Nerya's self-learning, self-reflection, and self-evolution capability.

## Scope
- Target repo: `Nerya/`
- References: `hermes-agent/`, `evolver/`
- Output: Chinese implementation plan under this folder
- Original planning non-goal: runtime code changes.
- Follow-up implementation scope: Nerya-native runtime code, API, CLI,
  dashboard, and focused tests for P0-P4 of the plan.

## Phases
- [x] Phase 1: Confirm local instructions, create plan files, and anchor scope
- [x] Phase 2: Trace current Nerya memory, reflection, evolution, strategy tuning, and proposal paths
- [x] Phase 3: Trace Hermes memory, session search, skill improvement, tool exposure, cron, and trajectory paths
- [x] Phase 4: Trace Evolver gene/capsule/GEP event loop and integration hooks
- [x] Phase 5: Build gap matrix, target architecture, tasks, acceptance flow, and roadmap
- [x] Phase 6: Review file references and deliver summary
- [x] Phase 7: Implement event/signal stores, evolution assets, policy,
  quality gate, and structured validation plans
- [x] Phase 8: Wire proposal metadata, apply/rollback events, strategy
  tuning validation, kernel hooks, memory quality checks, and selected
  asset recall
- [x] Phase 9: Expose self-evolution API/CLI/dashboard workbench
- [x] Phase 10: Add focused regression tests and run Python/dashboard checks
- [x] Phase 11: Upgrade dashboard into an evolution history control console
  with stitched timeline, detail inspection, action-oriented empty states,
  config/gate visibility, and a read-only aggregation endpoint

## Key Questions
1. What does Nerya already implement versus only document?
2. Which Hermes patterns are worth porting without breaking Nerya's skill-first trading runtime?
3. Which Evolver patterns are worth adapting as native Nerya evolution assets?
4. How should Nerya preserve proposal-first safety while gaining stronger autonomous improvement loops?

## Decisions Made
- The plan will be repo-stored, not chat-only, because this is a multi-system architecture task.
- The target design must preserve Nerya's `PatchProposal` boundary and live-trading safety gates.
- Evolver will be treated as a reference pattern for evolution assets and event protocols, not as a direct runtime dependency unless the plan explicitly marks an optional bridge.

## Errors Encountered
- Some broad `rg` searches over the aggregate checkout returned noisy or obfuscated Evolver files. Resolution: kept evidence to scoped files, README/SKILL, visible modules, and exact path references.
- PowerShell did not expand `src\gep\*.js` for `rg` as intended. Resolution: switched to `rg ... src\gep scripts -g "*.js"`.

## Status
**Implemented** - the plan is now backed by runtime modules, operator
surfaces, CLI/API entrypoints, a timeline-centered dashboard console,
and focused tests.
