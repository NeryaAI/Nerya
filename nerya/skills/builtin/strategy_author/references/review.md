# Strategy review: scheduler → evidence script → Agent

This is the execution contract, not the authoring template. After implementing
a strategy, the author must separately adapt `templates/review-plan.md` and save
the plan in its package-local `tuning.subagent.prompt_file`, with independent
`tuning.schedule`, lookback, objectives and safeguards in strategy.yml. A generic
scaffold alone is not a completed strategy-specific plan.

Use one independent review scheduler, one built-in collector and one review Agent
acting as Proposer. The runtime calls `build_strategy_review_context` before
dispatching the Agent; do not create another collector or team just to pass the
same data. The scheduler triggers the collector, which feeds the Agent.
The collector is a runtime script, not an editable `main.py` in the strategy.
Its selection settings are `tuning.lookback`; its output is `performance`.

Read the supplied strategy_id, manifest, run_id and frozen package/evidence
context. Use only records attributable to that strategy, version, mode and
requested time window. Cite real runs, fills, errors and Agent task evidence.
Use market_context and news_context when available; state missing or degraded
data explicitly. Do not mix other strategies or current files into old reviews.

## Adapt the plan on every review invocation

Use this version's frozen package-local plan and `tuning_prompt` as the baseline.
First state a short public `review_plan` for this run: strategy_id, package_hash,
run_id, evidence window, prioritized checks, evidence gaps and hold conditions.
Choose checks from the actual strategy and available evidence, not a fixed checklist
repeated regardless of conditions. No new fills means inspect skipped signals,
rejections or data health without inventing a profitability sample; recent errors
prioritize execution correctness; sufficient settled trades allow cost/exit/risk
analysis. Agent strategies additionally examine dispatch paths and task evidence.
Explain what was deferred and why. Do not call another planning Agent.

The per-run plan is part of the recorded result, not a newly installed task or a
rewrite of the persistent plan. To change future review timing, lookback or checks,
propose a complete strategy.yml or prompt-file change through the same Proposer;
do not self-reschedule, auto-apply or read unrelated review sessions.

Return one focused proposal: what happened, why a change is justified, the
smallest coherent change, and how it will be checked. Do not force a change
after every review. Insufficient evidence or no worthwhile improvement means
`proposed_changes: []` with an explanation, not an invented patch or profit claim.
Do not delegate to extra Agents or manufacture competing candidates by default;
preserve explicitly requested custom review instructions.

Use the supplied materializable_output_contract. Return review_plan, summary, evidence,
proposed_changes, expected_effect (a hypothesis, not a verified outcome),
validation_plan and risk_flags. For changed source/prompt files provide complete
after_content; for strategy.yml provide complete config_after or yaml_after.
Preserve strategy identity, user constraints, explanatory comments and protected
settings. Read `references/explanations.md` when documenting changed behavior.
Respect allowed_targets, forbidden_targets and guardrails. A prose suggestion
or diff alone is not an applicable file change.

Return the structured output once. The runner materializes eligible changes as
a pending-review PatchProposal; do not create a second proposal manually.
Validation, operator approval, application, rollback and version history remain
in their existing lifecycle. Never bypass them, edit the running strategy,
place orders, enable live trading or claim that a suggested change has passed
validation. Keep the review scheduler visible on its own canvas. Proposal,
validation and approval remain lifecycle settings rather than more wired steps.
Preserve explicit custom restrictions when updating an existing plan; a strategy
change must update any affected checks, not overwrite the plan with the template.
