# Scoped Research Authorization and Cheap Diagnostics

Read this reference for `ResearchAuthorization`, automatic pipeline continuation, artifact-only failure analysis, deterministic ComponentHypothesis drafts, and Run Bundle presentation.

## Authorization

- Bind the authorization to one `subject_id` and `session_id`.
- Record allowed stages, auto-continue, cost/time/Trial budgets, stop conditions, expiry and user approver.
- The guided template may cover correctness, smoke, fast screen, saved-metric viability, and the three cheap diagnostic stages.
- Internal stage completion is not a new approval point. Baseline freeze, Proposal acceptance, component/parameter Batch budget, locked test and dry-run remain separate approvals.
- Authorization alone runs nothing. A reviewed white-listed Job and registered strategy executor are still required.

## Failure behavior

- Correctness, smoke or fast-screen failure stops the path.
- Viability failure rejects the standalone outcome. If the same authorization includes cheap diagnostics, continue only to loss attribution, regime diagnostic and hypothesis generation.
- Do not run cost/full stress, parameter search, locked test or strategy restoration after failed viability.

## Artifact-only attribution

Prefer existing trades, signals and metrics. Separate train and validation and report side, exit reason, holding time, UTC session/weekday, stop distance, cost share, first entry/reentry, streaks, skip reasons and the multi-timeframe funnel. Keep unavailable stages explicit.

Do not claim causality. If validation was inspected before creating a hypothesis, mark the hypothesis and later diagnostic Trials as screening/contaminated rather than independent OOS.

## Regime and components

Use closed 1h bars with an explicit decision lag. `regime_diagnostic` may only create screening or insufficient-history evidence and cannot produce a formal suitability claim.

Generate no more than three transparent deterministic ComponentHypothesis drafts when Embedded AI is unavailable. Each draft changes one component, suggests 3-5 Trials and records evidence, failure conditions and contamination. Do not run Trials until the user approves one exact Diagnostic Batch budget.

## Run Bundle and Handoff

Show one Run Bundle per user-visible run. Keep manifest, key metrics and failure reason authoritative; retain trades/signals when attribution requires them; label charts and detailed logs rebuildable/archiveable.

The final Handoff must say which stages ran, which did not, why the process stopped, whether user action is needed, and the exact subject or hypothesis IDs for the next optional action.
