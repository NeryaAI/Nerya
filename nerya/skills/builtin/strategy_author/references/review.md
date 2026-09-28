# Default strategy review: script → Agent

Use one built-in collector and one review Agent acting as Proposer. The runtime
already calls `build_strategy_review_context` before dispatching the Agent;
do not create another collector, team or workflow just to pass the same data.
The collector is a runtime script, not an editable `main.py` in the strategy.
Its selection settings are `tuning.lookback`; its output is `performance`.

Read the supplied strategy_id, manifest, run_id and frozen package/evidence
context. Use only records attributable to that strategy, version, mode and
requested time window. Cite real runs, fills, errors and Agent task evidence.
Use market_context and news_context when available; state missing or degraded
data explicitly. Do not mix other strategies or current files into old reviews.

Return one focused proposal: what happened, why a change is justified, the
smallest coherent change, and how it will be checked. Do not force a change
after every review. Insufficient evidence or no worthwhile improvement means
`proposed_changes: []` with an explanation, not an invented patch or profit claim.
Do not delegate to extra Agents or manufacture competing candidates by default;
preserve explicitly requested custom review instructions.

Use the supplied materializable_output_contract. Return summary, evidence,
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
validation. Scheduling remains independent of trading and does not add a third
node to the default review canvas. Existing prompts and custom layouts are not
rewritten just to adopt this simpler presentation.
