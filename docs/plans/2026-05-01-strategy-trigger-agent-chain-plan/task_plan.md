# Task Plan: Strategy Trigger Agent Chain

## Goal
把方案文档重写为新的目标语义：trigger 唤醒 Agent Session 执行策略任务，策略脚本负责获取数据、计算指标/因子并组装最终 prompt 字符串，Agent Session 复用下单规则和工具权限完成受控交易。

## Phases
- [x] Phase 1: 复核现有文档和当前代码事实
- [x] Phase 2: 按新语义重定义链路边界
- [x] Phase 3: 重写主方案文档
- [x] Phase 4: 同步 notes / task_plan
- [x] Phase 5: 实现 `StrategyAgentTask` 契约与 prompt helper
- [x] Phase 6: 实现 trigger -> Agent Session executor
- [x] Phase 7: 实现 session profile 与交易工具 guard
- [x] Phase 8: 补测试、HTTP/API 可观测性和验收

## Key Questions
1. trigger 如何从 routed 记录变成一次 Agent Session 任务？
2. 策略脚本如何产出最终 prompt，而不是让 runtime 自动拼 K line / indicators？
3. Session 如何稳定复用策略下单规则、工具权限和历史上下文？
4. Agent 直接调用交易工具时，如何确保仍经过 RiskGate / ApprovalGate？

## Decisions Made
- 新 target 建议为 `skill:strategy.agent_task`，专门表示“策略脚本生成 prompt，然后投递给 Agent Session”。
- 保留 `skill:strategy.run_tick` 作为旧语义：策略 runtime tick，策略代码自己提交交易意图。
- 新增 `StrategyAgentTask` 契约，策略脚本返回 `dispatch` / `skip` / `error`。
- runtime 不再自动理解 `klines` / `indicators` 字段，只负责把策略脚本产出的 prompt 字符串传给 Agent。
- Session profile 承载长期下单规则、允许工具、风控约束和策略身份；本轮 prompt 只承载本轮数据和任务。
- Agent 可以在策略任务 Session 中直接调用 `trade_intent_submit`，但该调用必须被 guard，并继续经过 RiskGate / ApprovalGate。

## Errors Encountered
- 旧方案把重点放在 `ctx.agent.analyze_signal()` 和结构化 Agent decision，和用户澄清的“Session Agent 直接执行任务和交易”不一致；已重写。
- 当前 `TriggerRuntime.emit()` 仍只 route，不执行；新方案把这作为 `StrategyAgentTaskExecutor` 的核心修复点。
- 当前 scheduled agent session 每次使用 `sched:<id>:<ts>` fresh session；新方案要求策略任务默认使用稳定 session policy。
- 实现测试中发现 Windows CSV artifact 会带 `\r\n`，测试已按 prompt 语义做换行归一化，不改变运行时代码。
- `TriggerAPI.update_schedule()` 曾返回列表最后一个 schedule，而不是被更新的那个；实现时已修正并加回归测试。

## Status
**Completed** - 已实现策略脚本组装 prompt、trigger 投递稳定 Agent Session、session profile 注入、交易工具 guard、agent task history/API 回放，并完成测试验收。

## Verification
- `python -m pytest tests/test_strategy_agent_task_chain.py -q` -> 4 passed
- `python -m pytest tests/test_strategy_order_auto_approval.py -q` -> 5 passed
- `python -m compileall nerya tests/test_strategy_agent_task_chain.py -q` -> passed
- `python -m ruff check <changed python files>` -> passed
- `python -m pytest -q` -> 51 passed
- `python -m pytest -m "" -q` -> 51 passed
