# Notes: Strategy Trigger Agent Chain

## 新语义

- trigger 用于触发 Agent 执行任务。
- 策略脚本负责获取数据、计算指标/因子、添加自定义字段，并组装最终 prompt 字符串。
- prompt 可以是 CSV、Markdown、JSON block 或混合文本；runtime 不应自动猜测字段语义。
- prompt 放入稳定 Agent Session，由该 Session 的 Agent 复用下单规则、工具权限和历史上下文执行任务。
- Agent 可以直接调用受控交易工具提交订单意图，但不能绕过 RiskGate / ApprovalGate。

## 当前代码事实

- `AgentKernel.run_turn()` 的 user text 来源已经支持 `trigger.payload.text` / `message` / `prompt`，适合直接投递策略脚本组装好的字符串 prompt。
- 传入同一个 `session_id` 时，AgentKernel 会加载同 session prior messages；`SessionStore` 会持久化 `turn_ids`、`invoked_skills`、`skill_state` 等信息。
- `ScheduledSessionRunner` 当前为 scheduled agent 生成 `sched:<schedule_id>:<ts>`，适合一次性任务，不适合高频策略复用规则。
- `TriggerRuntime.emit()` 当前只 route，不执行 Agent task。
- `_strategy_triggered_order_turn()` 已能为 strategy/schedule/price/news/onchain 等来源的策略 turn 授予 `trade_intent_submit` 工具权限基础。
- `nerya/trading/submit.py` 中交易提交路径仍会经过 `RiskGate` / `ApprovalGate` / `ExecutionEngine`。

## 推荐实现方向

1. 新增 `StrategyAgentTask` 输出契约。
2. 新增 `skill:strategy.agent_task` target executor。
3. 策略脚本新增 `build_agent_task(ctx)`，返回最终 prompt。
4. runtime 解析稳定策略 Agent Session ID。
5. session profile 注入长期下单规则和允许工具。
6. Agent turn 收到 `payload.text=prompt` 后直接执行任务。
7. `trade_intent_submit` 加 strategy/session guard，固定 `strategy_id`、`session_id`、`trigger_event_id`、`source=strategy_agent`。
8. 增加 agent task history、prompt artifact 和 dashboard 回放。

## 已实现事实

- `nerya/strategies/agent_task.py` 提供 `StrategyAgentTask.dispatch/skip/error`。
- `nerya/strategies/prompt_io.py` 提供 CSV、Markdown table、JSON block、artifact helper。
- `build_strategy_context()` 已注入 `ctx.trigger`、`ctx.prompt` 和 `ctx.mode`。
- `nerya/triggers/strategy_agent_task_executor.py` 已实现 `skill:strategy.agent_task` 执行层：加载策略、运行 prompt builder、解析稳定 session、写 prompt artifact、调用 `AgentKernel.run_turn()`。
- `TriggerRuntime.emit()` / `replay()` 已在 route 成功且 target 为 `skill:strategy.agent_task` 时同步执行 Agent task。
- `nerya/agent/session_profile.py` 已持久化策略 Agent Session profile，并由 `AgentKernel._build_system_prompt()` 注入系统提示。
- `trade_intent_submit` native wrapper 已在策略触发 Agent turn 中强制绑定 `strategy_id`、`source=strategy_agent`、`trigger_event_id`、`agent_session_id`，并按 profile 校验 allowed tools、account、market、confidence、max_single_order_usd。
- `StrategyAPI.history()` 已包含 `agent_tasks` ledger；新增 `agent_tasks()` / `agent_task()` 读取 prompt artifact 和 session profile。
- HTTP runtime routes 已新增 `GET /strategies/runtime/agent_tasks` 与 `GET /strategies/runtime/agent_task`。

## 重要边界

- K line / 指标 / 因子由策略脚本负责获取和组装，不由 trigger payload 或 runtime 自动传递。
- Runtime 可以提供 CSV / artifact helper，但不能替策略决定字段含义。
- Session profile 放长期规则；prompt 放本轮数据。
- Agent 直接下单等于直接调用受控 native trading tool，不等于绕过风控。

## 已跑验收

- `python -m pytest tests/test_strategy_agent_task_chain.py -q` -> 4 passed
- `python -m pytest tests/test_strategy_order_auto_approval.py -q` -> 5 passed
- `python -m compileall nerya tests/test_strategy_agent_task_chain.py -q` -> passed
- `python -m ruff check <changed python files>` -> passed
- `python -m pytest -q` -> 51 passed
- `python -m pytest -m "" -q` -> 51 passed
