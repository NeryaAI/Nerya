# Prompt Playwright 架构修复报告

## 结论

最终完整回归已通过。

- 运行范围：`C5,C-AT6,C-AT9,E2,E3,E6,E10,G9,GX4,GX6,GX11,H9,I4`
- 权限模式：`NERYA_PERMISSION_MODE=yolo`
- 单 case timeout：`1200000ms`
- 配置来源：从 `C:\Users\Ricky\.nerya\nerya.yml` 派生到隔离 workspace
- 最终结果：`13 passed (14.7m)`
- 未实现 / 未通过 case：无

## 大白话失败原因

### E2

以前的问题：团队任务有时没有把多角色结果整理成一份干净报告，而是把内部 fallback JSON、工具观察、状态字段直接露给用户。

大白话：本来应该给用户一份会议纪要，结果把后台草稿纸和调试日志贴出来了。

修复：把 `tool_observation_fallback` 这类内部 payload 只留在日志/事件里，最终答复必须走干净的用户文本合成；如果合成不完整，就生成有边界的 evidence report，而不是泄漏原始 JSON。

### GX6

以前的问题：测试要求必须创建 `provider_proposal`，但 runtime 只是提示模型应该调用 `evolve_provider_proposal`。模型如果只用文字说“我会创建”，系统也可能结束。

大白话：任务要求“把申请单提交出来”，但模型只说“我准备提交申请单”，系统就差点算它完成。

修复：把 `required_artifacts` 当作硬约束。只要 contract 要求某个 artifact 且对应工具可用，就必须看到真实工具成功结果；对 provider proposal 使用 contract 里的结构化信息恢复工具调用，不按 case id 或 prompt 文案硬编码。

### E3

以前的问题：团队和策略 proposal 都成功了，`strategy_backtest` 也被调用了，但回测内部失败：`MockCtx object has no attribute get_ohlcv`。

大白话：策略代码用的是常见 SDK 说法 `ctx.get_ohlcv(...)`，但回测环境只认识 `ctx.ohlcv(...)`。真实环境和回测环境说的不是同一套“方言”。

修复：让 live `StrategyContext` 和 backtest `MockCtx` 暴露一致的 market-data alias：`get_ohlcv`、`get_candles`、`klines` 都转发到已有 candle/ohlcv 实现。这样以后模型生成等价 SDK 写法也能在回测里跑通。

## 修复文件

- `nerya/agent/loop.py`
- `nerya/agent/kernel.py`
- `dashboard/tests/e2e/csv-runner.spec.ts`
- `nerya/skills/builtin/backtest/scripts/mock_ctx.py`
- `nerya/strategies/context.py`
- `tests/test_agent_loop_final_summary.py`
- `tests/test_backtest_skill.py`

## 不是硬编码

- 没有新增 case id 分支。
- 没有按具体 prompt 文案写判断。
- 没有 mock LLM 成功路径。
- 没有添加 committee 专用的标记式修复。
- Provider proposal 修复依赖机器可读 contract 和工具可用性。
- E3 修复是公共 SDK 兼容层，不是 SOL 策略特例。

## 验证

- `python -m pytest tests/test_backtest_skill.py -q` -> `36 passed`
- `python -m pytest tests/test_agent_loop_final_summary.py -k "strategy_backtest_runtime_error or distinct_backtest_runtime_errors or strategy_backtest_success_finalizes or strategy_backtest_data_gap or required_team_strategy_backtest" -q` -> `5 passed`
- `python -m pytest tests/test_agent_loop_final_summary.py tests/test_strategy_code_generator.py tests/test_team_streaming_events.py tests/test_backtest_skill.py -q` -> `374 passed`
- Focused `E3` Playwright -> `1 passed`
- Final 13-case Playwright -> `13 passed (14.7m)`

## 日志与截图检查

- `dashboard/test-results/summary.csv`：13 行全部 `pass`，`api_check_pass=yes`
- `dashboard/test-results/logs/*.reply.txt`：扫描内部泄漏关键词无命中
- `dashboard/test-results/screenshots/*.png`：13 张截图均生成
- `E3` 最新证据：`transition_reason=strategy_backtest_finalized`，`successful_tool_names` 包含 `team_run`、`strategy_generate_proposal`、`strategy_backtest`
- `GX6` 最新证据：`successful_tool_names` 包含 `evolve_provider_proposal`，API check 显示 `metadata contains: aster ok`
