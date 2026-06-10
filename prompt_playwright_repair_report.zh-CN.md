# Prompt Playwright 修复报告（中文）

> 本文件记录本轮实际修复内容、验证结果和剩余工作。

## 已修复
- `A10`：取消进行中的任务现在会在页面中生成正常 assistant 回复文本，而不是 error-only 卡片；CSV runner 会把取消回复写入 reply 文件并参与质量断言。
- `D5`：`task_create` 会把 `scripts/approved/<id>/<id>.py` 这类 path-like approved script 引用归一化为裸 `script_id`，并持久化为 `target: script:<id>`。
- 最终摘要兜底：确定性 fallback 不再输出 `tool errors:` 这种会触发 reply-quality 内部 dump 检测的字段。
- Required artifact 兜底：如果调用方明确要求 proposal/backtest/provider/skill/task 等结构化产物，但对应工具仍没有成功结果，agent loop 会返回明确缺失说明并标记 `required_artifact_missing_finalized`，不再静默接受纯文本完成。

## 已验证
- Python 聚焦测试：`18 passed`。
- Dashboard 类型检查：`npx tsc --noEmit` 通过。
- Playwright 聚焦回归：`A10,D5` 使用 `cases.timeout20m.csv` 定向执行，`2 passed`。
- A10 截图：页面显示“已取消当前任务。后端中断请求已发送…”，无 error card、无空回复。
- D5 日志/截图：`task_create ok`，API check 通过；测试 workspace 的 `triggers/schedules.yml` 中为 `target: script:eth_btc_ratio_chart`、`payload.script_id: eth_btc_ratio_chart`。
- Playwright yolo 回归：使用 `NERYA_PERMISSION_MODE=yolo` 和 `NEXT_PUBLIC_NERYA_PERMISSION_MODE=yolo` 跑历史失败集合 22 个 case，结果 `9 passed / 13 failed`。通过项：`C3`, `C7`, `D9`, `F4`, `F6`, `G1`, `GX1`, `H6`, `I3`。

## 剩余风险
- 本轮只修复并回归了第一批高置信问题。完整 160 case 尚未重跑。
- yolo 后仍需修复的产品/输出类 case：`C5`, `C-AT6`, `C-AT9`, `E3`, `E10`, `G9`, `GX4`, `GX6`, `GX11`, `I4`。
- yolo 后仍需单独处理的 harness/env/brittle assertion 或 workspace 预置问题：`E2`, `E6`, `H9`。
- Playwright global setup 的 live LLM probe 仍可能路由到 `sensenova-6.7-flash-lite` 并因空文本 `max_tokens` 失败；本次聚焦 case 使用 `NERYA_E2E_SKIP_LLM_PROBE=1` 才进入实际 case。
- 已补 dashboard `final_text` fallback 并通过 typecheck，但 `G9/GX4/GX11/I4` 复跑仍失败；说明这些 case 后端没有生成干净 final_text，需要修 agent loop 的 `no_more_tools` finalizer。

## 下一批详细修复方案
- `C5`, `E3`, `E10`, `GX6`：最高优先级，都是 required artifact 逃逸。当前兜底已能暴露缺失，但还没有把模型强制带到 `strategy_generate_proposal`/`strategy_backtest`/`evolve_provider_proposal`。下一步检查 provider tool surface、`required_artifacts.defer_initial_tool_choice`、`_next_required_artifact_tool_names()` 和最终 retry 是否在初始无工具回合前生效。
- `G9`, `GX4`, `GX11`, `I4`：第二优先级，都是最终回复污染。最终 answer 抽取到了 thinking/tool/Raw JSON trace，需要修复 `topLevelDecisionText`/dashboard reply fallback 或后端 final_text 生成，保证 `[data-turn-section="reply"]` 只包含自然语言最终答复。
- `C-AT6`：策略产物内容缺项。proposal/backtest 已成功，但 `main.py` 缺 `news_social`，应修 `strategy_author`/proposal prompt 或模板，让“新闻叠加”需求变成代码依赖/输入，而不是只体现在说明文本。
- `C-AT9`：回测工具运行但 contract finalizer 语义不一致。`strategy_backtest` 返回 `ok=false/no_historical_data` 时，API check 仍要求 backtest finalized；需要定义“诚实不可回测”的 transition 是否算 finalized，并让 finalizer/API check 口径一致。
- `E2`：`team_run` 已成功，失败只因回复不含 literal `team`。应在 team bounded fallback 或 case 断言中统一术语，建议产品侧 final text 保留 `team` 的用户可读短标识。
- `E6`：workspace 报告 Financial Datasets ready=true，但 case 预期未配置。需要确认 isolated workspace 是否从本地 vault 复制了 FD key；这是环境预置和断言预期冲突。
- `H9`：provider proposal 已创建，但 final text 没有包含 key/vault/status 关键字。修 finalizer 的 provider proposal 摘要字段即可。
