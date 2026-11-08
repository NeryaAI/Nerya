---
name: strategy_author
description: "Create, edit and repair Nerya script, script-gated Agent and scheduled/event Agent strategies, including CEX, on-chain, LP and DeFi candidates. Save an editable proposal; validate as needed and load backtest only for requested historical research."
version: 0.22.0
license: MIT
author: Nerya
---

# Strategy Author

## Create the candidate first

A creation request is complete with a saved, editable strategy proposal and an
honest account of its behavior, assumptions and unfinished bindings. It does not
require tests, backtests, reports, a tuning plan or complete pool history.
This file owns that completion boundary; detailed references supply interfaces
and optional methods, not additional mandatory stages.

1. Preserve the user's strategy thesis, markets, timeframe, risk limits and
   execution intent. Choose reasonable reversible implementation defaults and
   explain them. Research only what helps implement the candidate. Missing
   accounts, credentials, pool identity or historical data can remain explicit
   pending bindings; they do not prevent saving a draft. Ask only when a choice
   materially changes the user's goal, cost or authority. Do not repeatedly ask
   permission to continue ordinary authoring.
2. For a script-driven Agent, read `references/trend_follow_subagent.md`
   **before the first draft call** to choose synchronous script-owned
   confirmation or Agent-executor dispatch. For synchronous confirmation, use
   the reference's canonical scaffold fields and reuse its returned files.
   Use `strategy_draft_proposal` to save new work, or continue the current
   proposal for edits and repairs. Use its actual ID and returned paths, with
   only advertised tool fields. Finish the requested logic in that candidate;
   distinguish an incomplete scaffold from an implemented strategy. Leave
   unresolved resources clearly unbound rather than inventing accounts, pools,
   provider IDs or SDK calls.
3. Choose a suitable template, adapt existing code or implement the strategy
   yourself. Templates are examples, not required package shapes. Include
   dependencies the chosen implementation actually imports. Read only the
   references and files needed for the current decision or edit; no whole-package
   reading pass, per-script explanation format or checklist is required.
4. Save useful changes before optional verification. Create/draft defaults to
   `validate:false`; explicit `validate:true` requests diagnostics. Default to
   no test execution. Use `strategy_validate` or targeted checks to investigate a
   concrete uncertainty or satisfy an explicit request. Preserve diagnostics,
   repair the same candidate as appropriate, and report failed, missing or stale
   checks honestly; independent `strategy_validate` still reports real failures.
   Validation success is not a prerequisite for saving,
   delivering a candidate or submitting it for review.
5. Deliver the saved candidate and explain its rules, chosen defaults, pending
   bindings and actual verification status. Creation alone needs no further
   action. When submission is requested, use `strategy_submit_proposal` on the
   same candidate and report its actual state. Submission performs static
   diagnostics without smoke tests or test execution; failures do not prevent
   `pending_review`. Submission is not activation.
   Add tuning or a review workflow only when requested or needed by the chosen
   design, with its schedule inactive.

## Permissions and recovery

Keep all authored changes in the proposal lane; never overwrite an active
strategy. Preserve explicit no-submit/no-test restrictions, existing account and
risk limits, and separately authorized approval, promotion, activation and trading
operations. Creating or submitting a proposal grants none of those permissions.
Keep new trading and review schedules disabled.

Use the installed SDK and approved execution paths. Any chosen validation,
test or replay must retain its execution sandbox and approval boundaries.
Resolve pending approvals and unknown side effects through the original
operation's recovery path before retrying; a continuation or fresh authorization
does not confirm an earlier uncertain write or model call.

On continuation, reuse the confirmed candidate and inspect only the saved state
needed for the next action. If its identity is missing, use
`evolve_proposals(scope="task")` and the returned ownership/recovery evidence;
an unrelated same-name proposal is not a substitute. For stale-file or revision
errors, reread the named file/state and correct the same operation.
Retry only after a relevant input or known precondition changes.

## Requested backtests and research

Load `Skill(skill="backtest")` when the user explicitly requests a backtest,
historical replay or comparison. Follow that Skill's autonomous period selection
and partial-result workflow. Preserve explicit dates, assets, strategy settings
and risk rules; explain chosen periods and gaps. Missing history or incomplete LP
states limit research claims, not candidate delivery.

A script-Agent strategy must retain an actual Agent decision path when requested.
Authoring that path does not require running the model. For requested historical
model execution, use the backtest Skill's existing authorization and sandbox
path. Separate branch fixtures, historical dispatch, model decisions, simulated
fills and account activity in the evidence.

## Delivery evidence

The strategy-authoring minimum is `source_binding` plus `candidate`.
`strategy_contract`, behavioral tests, replay, coverage and report checks are
optional diagnostics with their actual statuses. A creation-only delivery should
not add them as required stages. Use the existing contract key and tool schema;
for example:

```json
{"skill_id":"strategy_author","workflow_id":"strategy_authoring",
 "requested_scope":{"summary":"Create the requested strategy; unresolved account/pool bindings remain pending"},
 "stages":[{"id":"author","state":"in_progress"},
   {"id":"deliver","required_checks":["source_binding"],"required_artifacts":["candidate"]}]}
```

When using delivery tools, register the actual producer-returned candidate as
`{"artifact_id":"candidate","ref":"<source_binding.artifact_id>","revision":"<source_binding.revision>"}`.
Use the latest `delivery_revision` as `expected_revision` for updates/finalization.
Producers own their checks; omit model-authored `checks` from `delivery_update`.
Reconcile current state on a revision conflict instead of rerunning producers for
bookkeeping. Inspect the actual finalizer result before claiming workflow
completion; source binding proves the saved version, not behavior or profitability.
The installed `references/delivery-contract.json` supplies the contract IDs.

Bind verification claims to actual receipts and source revisions. A later edit
can stale earlier evidence. Report tests/replay as not run when absent; a saved
script, `ok:true` envelope or `pending_review` state proves neither execution nor
returns. Preserve partial/failed diagnostics without turning them into authoring
gates. If work stops, say what was saved, what remains unresolved and the next
concrete action.

## Load references by need

Use `Skill(skill="strategy_author", file="<relative path>")` for the relevant
section; reuse already loaded material.

- SDK calls, errors and signal handling: `references/script-sdk.md`.
- Script/Agent routing, schedules and canvas: `references/workflows.md`.
- Authoring edge cases and evidence: `references/authoring-contract.md`.
- Market/protocol implementation options: `references/mainstream-strategies.md`;
  use `financial_ops` for installed financial component contracts as needed.
- LP sampled studies or rate-policy implementations:
  `references/lp-sampled-research.md` or `references/rate-policy.md`.
- Position sizing: `references/position-sizing.md`; preserve explicit sizing and
  existing limits, using percentage-of-NAV defaults for new unspecified sizing.
- Actual persistent listeners: `references/continuous.md`; finite event Agents
  remain ordinary routed invocations.
- Requested tuning/review: `references/review-plan.md` and
  `references/review.md`; requested bounded optimization: `quant-strategy-loop`.

Use closed candles, the injected clock, settled positions and persistent dedupe
where the strategy needs them. Preserve financial action identity and protection;
a missing protocol binding is not permission to substitute a spot trade.
Pure script ticks make no model calls; a requested script gate runs before the
Agent. Explain strategy logic in the user's language without imposing a mandatory
annotation block on every script.
