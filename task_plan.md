# Task Plan: Nerya Current-State Production / Parity Refresh

## Goal
Produce a current-truth audit for Nerya covering Hermes parity, Claude Code-inspired agent design gaps, production readiness, hardcoded/mock surfaces, SDK alignment, frontend/operator alignment, and a closure plan that can take the project to honest production operation.

## Phases
- [x] Phase 1: Load local instructions, audit baseline, and current repo state
- [x] Phase 2: Re-check runtime, SDK, scripts, evolution, wallet/provider, and dashboard surfaces
- [x] Phase 3: Re-check Hermes and Claude Code reference targets
- [x] Phase 4: Re-run focused verification and truth-gate tests
- [x] Phase 5: Write a new refresh document instead of partially overwriting the 2026-04-23 audit
- [x] Phase 6: Deliver findings-first summary with exact remaining closure phases

## Key Questions
1. Which old blockers are actually closed now?
2. Which claimed capabilities are still only partial, stubbed, or mock-backed?
3. Is the public SDK story really aligned across Python, TypeScript, and HTTP?
4. Which hardcoded enums and route heuristics still over-constrain agent behavior?
5. What is required for honest `prod_paper`, `canary_live`, `full_live`, Hermes parity, and Claude Code-inspired parity?

## Decisions Made
- Do not trust the 2026-04-23 audit verbatim; re-verify against current code.
- Keep the 2026-04-23 audit as history and create a new 2026-04-24 refresh doc.
- Treat native runtime truth separately from parity claims and separately from operator-surface truth.
- Treat explicit safety enums as valid, and only flag hardcoding that constrains routing/flexibility or misrepresents capability.

## Errors Encountered
- The broader desktop git root resolves to `C:/Users/Ricky`, so repo-wide `git status` is not a useful signal for Nerya-only review.
- Earlier planning files were not present on disk in the current `Nerya` tree and had to be recreated for this refresh.

## Verification
- Focused runtime/trading/evolution suite: `143 passed, 1 skipped in 264.34s`
- SDK and production truth gates: `30 passed in 3.10s`
- `npx tsc --noEmit` passed in `dashboard`
- `npx tsc --noEmit` passed in `sdk/typescript`
- `python sdk/python/examples/direct_order_strategy.py` now starts from repo root and reaches the trading kernel; current run is rejected because the active workspace does not contain `btc_momentum`

## Status
**Completed** - refresh doc written at `docs/plans/2026-04-24-nerya-production-readiness-refresh.md`, findings re-verified against current code, and focused regression/truth-gate checks passed.
