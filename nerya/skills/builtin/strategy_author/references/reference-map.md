## Load only the reference matching this strategy

- Lending deposit-rate entry/exit and historical APY simulation:
  references/rate-policy.md for the saved policy and host-owned replay.
- Creation/edit completion: `templates/review-plan.md` for the separately authored
  strategy review plan. Every review invocation uses `references/review.md` to
  adapt its checks to that version and run's evidence without changing the strategy
  or future schedule automatically.
- Script-gated/scheduled/finite event Agent: `references/workflows.md` and the
  relevant `references/script-control.md` section. A Python gate runs BEFORE AI;
  skipped branches do not call a model. Native Agent replay checks dispatch/inputs,
  not historical model trading performance (`agent_execution:not_run`).
- Actual persistent/WebSocket listener: `references/continuous.md`; do not place an
  infinite listener in a cron tick. Finite candle events are not continuous mode.
- Detailed SDK, protection, exotic instruments: `references/specialized-contracts.md`.
  Never delete live `protection` without an explicitly implemented historical exit
  model. Bar-close exits are not equivalent to exchange intrabar stop execution.
- Detailed source/review annotations: `references/explanations.md`; only when the
  compact annotation contract above is insufficient.
- Advanced verification: `references/verification.md`; data-only/offline requests:
  `Skill(skill="backtest", file="references/history-data.md")`.
- Other edge cases: `references/authoring-contract.md`. It is not a mandatory read.
- Requested research charts (not duplicate backtest cards):
  `Skill(skill="research", file="references/visual-deliverables.md")`.

An observer declares `evaluation.mode:observation`, real input sources and
`policy.allow_direct_order:false`. `allowed_tools:[]` means no tools, not market-only
tools. Keep the Agent role editable. Requested input `outputs` must already be
published, not the future model answer. Keep historical observations separate from
current analysis, and fees/percentages/source limitations visible in the final answer.
