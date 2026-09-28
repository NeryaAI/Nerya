<!-- nerya-skill-frontmatter-start -->
---
name: quant_strategy_research
description: "Orchestrate safe, auditable, fail-fast quantitative strategy research. Use when an agent intakes or formalizes a natural-language/Pine strategy, selects a pipeline profile, evaluates correctness/viability/robustness gates, downloads market data, runs backtests or bounded batch parameter research, manages Proposal/Candidate/Trial evidence, performs cost or full stress tests, records component/regime evidence, prepares dry-run work, or creates strategy/experiment/report artifacts."
version: 0.1.0
license: MIT
author: Nerya Community
---
<!-- nerya-skill-frontmatter-end -->

# Quant Strategy Research

Use this skill when the user asks to formalize a strategy idea, run a research pipeline,
evaluate gates, manage batch parameter research, or produce strategy/experiment artifacts.

## Flow

1. CLASSIFY the request as one or more configured intents. When uncertain, choose the stricter route.
2. READ every required document for each selected intent completely before acting.
3. Check every required precondition. Stop and report missing conditions; do not downgrade them to warnings.
4. Use only the route's allowed tools through project CLI/API-compatible interfaces.
5. Produce every required output artifact and append audit events for key actions and failures.
6. Report the intent, documents read, preconditions, approvals, tools, artifact keys, tests, and unresolved gaps.

## Pipeline profiles

Select `smoke`, `fast_screen`, or `full_validation` from pipeline configs; do not invent a hidden pipeline.

Execute in order: correctness -> fast screen -> viability -> cheap sensitivity -> regime/Pine -> full validation/locked/full stress -> dry-run.

Stop on a correctness, smoke, or fast-screen failure. A failed viability Gate may continue only into explicitly authorized inexpensive attribution, regime diagnostic, and component-hypothesis capture.

## Research integrity

- Save the original source before formalization.
- Treat AI and Agent output as draft/proposal until the user confirms it.
- Freeze baseline v0 once. A rule or economic-mechanism change requires a new Proposal/Candidate StrategyVersion.
- Test one explicit hypothesis at a time; require ablation for compound changes.
- Keep failed experiments, contaminated-test markers, costs, data versions, and stopping reasons.
- Never optimize solely for maximum historical profit or inspect locked test repeatedly.
- Never treat validation in one Market Profile as validation in another.

## Bounded parameter search

Do not tune parameters conversationally or one-by-one without a budget. Require an approved ExperimentPlan containing:

- frozen baseline and one falsifiable hypothesis;
- ParameterSpace, Objective, risk Constraints;
- train, validation, and locked-test splits;
- complete cost model;
- max Trials or time budget;
- stopping conditions and explicit user approval.

Only then create a `parameter_search` Job. The deterministic Worker, not the Agent, runs Trials.

## Lazy References

Read only the selected reference with `Skill(skill="quant_strategy_research", file="<path>")`.

- `references/pipeline-gates.md` for gate evaluation, strategy/component outcome classification, regime validation, or stress-job creation.
- `references/batch-research.md` for batch parameter search, retry, result summaries, or ComponentEvidence aggregation.
- `references/research-artifacts.md` for Artifact, ToolCall, Trial, Run, or Report records.
- `references/streamlined-research.md` for ResearchAuthorization, failure diagnostics, ComponentHypothesis drafts, or Run Bundle presentation.
