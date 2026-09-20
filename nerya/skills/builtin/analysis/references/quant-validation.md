# Quantitative Research and Signal Validation

Use when a hypothesis requires statistical evidence, not just a narrative.
Define the hypothesis, universe, signal timestamp, target, horizon and naive
baseline before examining performance. Inspect missingness, identifiers,
survivorship, corporate actions and timestamp alignment. Separate training,
validation and genuinely out-of-sample periods; never leak later data into a
decision or choose a benchmark after seeing the outcome.

Measure only metrics relevant to the hypothesis: IC, hit rate, turnover,
cost-adjusted returns, drawdown, exposure, capacity or attribution. Report
sample sizes, parameter choices, transaction costs and regime splits.
Sensitivity and a realistic baseline matter more than a single optimized
score. Distinguish observed results, simulated results and assumptions.

Return reproducible inputs/commands, the evidence for and against the signal,
robustness limits and a decision on further testing. Use `backtest` for the
controlled backtest workflow, not an invented execution path. Passing a
statistical test never authorizes a trade or a live strategy change.

Advanced historical checks remain available at
`Skill(skill="quant_research", file="references/full-playbook.md")`.
