# Optional authoring workflow

The root `strategy_author/SKILL.md` owns creation and delivery. A saved editable
candidate is sufficient for a creation request. Validation, tests, tuning,
submission and backtests are independent actions, not mandatory creation stages.

## Save and revise

Use `strategy_draft_proposal` with a chosen strategy ID and the available inputs.
Markets, accounts and pool bindings may remain unresolved in the candidate.
Use the returned proposal ID and paths for subsequent edits. Choose a template,
reuse suitable existing code or implement the requested logic directly.
Default saving records static diagnostics without executing candidate code.

Use `strategy_submit_proposal` when review submission is requested or appropriate.
Its static diagnostics remain visible even when the candidate needs repairs;
`pending_review` is a saved review state, not activation or trading permission.

For a concrete implementation uncertainty, use `strategy_validate` or a relevant
bounded check. Read its actual outcome and repair the same candidate when useful.
Failed, missing or stale diagnostics remain so, without preventing saving.
Load the `backtest` Skill only for requested historical calculations or comparisons.

## Delivery

The authoring contract requires `source_binding` and the `candidate` artifact.
Producers record source receipts automatically. Use actual returned identities
and the latest `delivery_revision` with `delivery_update`/`delivery_finalize`.
Stages list artifact IDs; artifact records contain refs. Do not insert
model-authored passing checks. Reports and run receipts are added only for
calculations actually performed, and are not required for creation alone.

Describe implemented behavior, pending resource bindings and verification status
separately. An editable scaffold is not an implemented algorithm; a saved script
or successful save is not evidence of execution, complete data or profitability.

## Execution boundaries

Keep source changes in the proposal lane and new schedules inactive. Approval,
promotion, activation and trading retain their existing permissions.
Any selected test/replay uses the execution sandbox. Reconcile pending or unknown
operations before retrying their effects; preserve the original request/receipts.
Missing history can limit a later study without blocking candidate delivery.
