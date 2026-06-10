# Prompt Playwright Case 修复方案（中文）

## 结论
本轮 160 个 prompt Playwright case 已完整跑完，结果为 136 pass / 24 fail。

24 个失败中：
- 18 个是产品实现、路由、最终答复收束或 UI contract 需要修复。
- 6 个不是核心产品未实现，主要是测试前提、harness 会话复用、环境状态或英文哨兵断言问题。

## 未实现 / 需要产品修复的 Case
| Case | 问题类型 | 修复归属 |
|---|---|---|
| `A10` | 取消任务后可见回复为空，只显示 error card | Dashboard cancel UI + agent interrupt final state |
| `C3` | Polymarket 不可回测场景暴露工具/JSON dump | Agent finalizer/output sanitizer |
| `C5` | 链上 smart-money meme proposal/backtest 后未 finalized | Strategy backtest finalizer + custom replay/data-gap contract |
| `C7` | AgentTeam 投研策略未生成 proposal/backtest | Required artifact enforcement |
| `C-AT6` | 技术信号+新闻策略 `main.py` 缺 `news_social` | Strategy code generator |
| `C-AT9` | 链上巨鲸策略未调用 proposal/backtest | Required artifact + wallet/onchain strategy routing |
| `D5` | 纯脚本 schedule 不接受路径型 `script_id` | Task script id normalization |
| `E10` | TSLA 团队分析未联动策略生成 | Required artifact enforcement |
| `F4` | 自动新增 Skill 未创建 skill proposal，且输出 dump | Skill proposal routing + finalizer |
| `G1` | Kraken paper account 仍要求真实 API 凭证 | Account setup routing / paper account policy |
| `G9` | Polymarket event odds 未输出正式赔率 | Polymarket market_data/data_api route + finalizer |
| `GX1` | Deribit option chain 已抓数据但未整形答复 | Option-chain data route + finalizer |
| `GX4` | Gamma exposure 未生成图/报告 | Options analytics/chart artifact route + finalizer |
| `GX6` | Aster provider proposal 没有调用 `evolve_provider_proposal` | Required provider proposal enforcement |
| `GX11` | dYdX public funding rate 被凭证门挡住并最终 dump | Public funding data route + finalizer |
| `H6` | 财报答复尾部泄漏 Evidence markers JSON | Output sanitizer |
| `I3` | Bitget Wallet 被误判为 Bitget exchange connector | Wallet/provider intent routing |
| `I4` | Coinbase CDP wallet ready 后未执行/收束 | Wallet capability action + finalizer |

## 非产品缺口但需要处理的失败
| Case | 判定 | 修复方向 |
|---|---|---|
| `D9` | harness 没有复用上一 case 的 chat session | 修改 CSV runner/fixture 支持 `reset_before=0` 的 thread reuse 或显式 `depends_on` |
| `E2` | 中文团队辩论已完成，只因 `must_contain=team` 失败 | 改成 API evidence 检查 `team_run`，或接受中文团队哨兵 |
| `E3` | team/proposal/backtest 都成功，只因 `must_contain=team` 失败 | 同上 |
| `E6` | 本地配置已有 Financial Datasets key，和“缺 key”预期冲突 | 给该 case 使用 scrubbed negative workspace，或让断言按环境自适应 |
| `F6` | isolated workspace 没有 “C1 策略” 前置 fixture | 为 case seed C1 策略，或让 CSV 显式依赖/保留 C1 |
| `H9` | 本地配置已有 Financial Datasets key，和 `financial_datasets_status=false` 冲突 | 同 E6 |

## 修复计划

### 1. 先修 Agent 最终答复收束与内部 dump 泄漏
覆盖 case：`C3`, `F4`, `G9`, `GX1`, `GX4`, `GX11`, `H6`, `I4`，并降低 `D5/GX6` 这类“有思考无最终回复”的 UI 泄漏概率。

涉及文件：
- `nerya/agent/loop.py`
- `nerya/evals/transcript_backend.py`
- `dashboard/components/chat/ChatView.tsx`
- `dashboard/tests/e2e/csv-runner.spec.ts`

修复动作：
1. 在 agent loop 进入 `no_more_tools` 或 `model_done` 前，如果本 turn 有成功工具结果但最终 assistant text 为空或只包含工具调试块，强制追加一次 text-only final synthesis。
2. synthesis 输入只允许 compact evidence，不允许原始 `ToolResult` JSON、`thinking`、`tool_batch_summary`、`Raw JSON`、`Evidence markers` 进入用户正文。
3. 对已经完成但带内部 marker 的最终文本加 sanitizer：剥离 `web_fetch ok: {...}`、`"status": ...`、`tool=...` 这类测试明确判定为内部 dump 的片段，改成自然语言来源说明。
4. Dashboard 中继续保留开发调试块，但不要把调试块混入 assistant reply 的主文本区域；必要时给工具块加稳定 `data-testid="tool-debug"`，测试只读取 assistant final text。
5. 增加 focused E2E/单元测试：模拟工具成功但模型没有 final answer，期望 UI 仍有一段用户可读总结，且不含 Raw JSON。

验证：
- 单跑 `C3`, `F4`, `G9`, `GX1`, `GX4`, `GX11`, `H6`, `I4`。
- 检查 `replyQualityFailures` 不再报 `assistant reply exposes internal team/schema dump`。

### 2. 修 required artifact 强制执行
覆盖 case：`C7`, `C-AT9`, `E10`, `GX6`，也包括 `F4` 的 skill proposal。

涉及文件：
- `dashboard/tests/e2e/csv-runner.spec.ts`
- `nerya/agent/loop.py`
- `nerya/tools/native/bootstrap.py`
- `nerya/tools/native/evolve.py`

修复动作：
1. 检查 `buildRequiredArtifacts` 对 `proposal_kind=provider_proposal`、`strategy_proposal_kind`、`requires_strategy_backtest`、skill proposal 的映射，确保 E2E contract 总能传入 `AgentConfig.required_artifacts`。
2. 在 `nerya/agent/loop.py` 中，如果 required artifact 还缺失，不允许 `model_done` 直接结束；即使没有工具调用，也要注入 `_required_artifact_retry_prompt` 并强制 tool_choice。
3. 对 required action 工具收窄时，不要把必要的前置发现工具全部屏蔽到模型无法构造参数；允许一次 bounded discovery，然后强制目标工具。
4. 对 `evolve_provider_proposal` 和 `evolve_skill_proposal` 添加最小参数 fallback：当用户已给 docs URL/base URL/signing model 或 skill domain 时，工具 schema 可以由 prompt 事实直接成案，不必再要求模型展开长 discovery。
5. 对 strategy cases 增加 scaffold fallback：如果 prompt/API contract 要 proposal/backtest，而模型只说“我会创建”，loop 应自动触发 `strategy_generate_proposal` 的最小可审阅草案。

验证：
- 单跑 `C7`, `C-AT9`, `E10`, `GX6`, `F4`。
- API 检查应看到对应 proposal id 和 required tool 成功记录。

### 3. 修策略 backtest 与 `news_social` 生成
覆盖 case：`C5`, `C-AT6`。

涉及文件：
- `nerya/tools/native/strategy_runtime.py`
- `nerya/evolution/strategy_code_generator.py`
- `nerya/research/datasets/router.py`
- `nerya/skills/builtin/backtest/references/custom_replay_template.md`
- `dashboard/tests/e2e/csv-runner.spec.ts`

修复动作：
1. `C5`：钱包/链上/meme strategy 不应走普通 OHLCV 后停在 `no_more_tools`。`strategy_backtest` 返回 data-gap/custom-replay-required 时，loop 要用 `strategy_backtest_data_gap_finalized` 或等价 transition 完成，而不是让 API check 看到 `no_more_tools`。
2. 为 flexible meme / wallet-flow 策略定义“可接受的诚实 finalized 状态”：包括 proposal id、缺失的数据源、下一步 custom replay 模板路径、未 promote/apply 的声明。
3. `C-AT6`：确保 prompt 中的新闻/社交信号能进入 `StrategyGenerationRequest.news_sources`，并且 `_ensure_news_social_main_hook` 在 `main.py` 中无条件留下可审计的 `news_social` 标记。
4. 如果用户覆盖了 `main.py`，hook 应在 override 后再次执行；当前 `_build_files` 已有该顺序，需要补齐 req.news_sources 解析或 API schema 入参。
5. 增加 contract test：带 news_sources 的 proposal，`strategy.yml`、`strategy.md`、`main.py` 均包含 `news_social` 或具体来源。

验证：
- 单跑 `C5`, `C-AT6`。
- 检查 proposal after tree 中 `main.py` 包含 `news_social`，backtest transition 是 finalized。

### 4. 修任务调度、会话恢复和跨 case fixture
覆盖 case：`D5`, `D9`, `F6`。

涉及文件：
- `nerya/skills/builtin/tasks/scripts/create_task.py`
- `dashboard/tests/e2e/fixtures.ts`
- `dashboard/tests/e2e/csv-runner.spec.ts`
- `dashboard/tests/e2e/cases.timeout20m.csv`
- `tools/prepare_isolated_test_workspace.py`

修复动作：
1. `D5`：新增 `normalize_approved_script_id(value)`，接受以下输入并归一成裸 id：
   - `eth_btc_ratio_chart`
   - `script:eth_btc_ratio_chart`
   - `scripts/approved/eth_btc_ratio_chart/eth_btc_ratio_chart.py`
   - `scripts\approved\eth_btc_ratio_chart\eth_btc_ratio_chart.py`
2. `D5`：如果路径能解析到 approved script 目录，不要报 `approved_script_not_found`；最终 schedule 必须写入 `session_kind=script`。
3. `D9`：CSV runner 不应在 `reset_before=0` 时无条件新开 `/chat`。新增 `lastSessionId`，当下一行要求 continuation 时导航到 `/chat/<lastSessionId>` 或通过 UI 选择上一 thread。
4. `F6`：给 isolated workspace seed 一个稳定的 `C1` strategy fixture，或在 CSV 中显式声明 `depends_on=C1` 并保留对应 workspace state。

验证：
- 单跑 `D5`, `D8`, `D9`, `F6`。
- D9 reply 应包含继续/上下文/BTC/宏观等恢复线索。

### 5. 修 account / wallet / provider intent 路由
覆盖 case：`G1`, `I3`, `I4`。

涉及文件：
- `nerya/tools/native/accounts.py`
- `nerya/tools/native/bootstrap.py`
- `nerya/tools/native/connectors.py`
- `nerya/data_api/builtins.py`
- `nerya/wallet/registry.py`
- `nerya/api/routes_wallet.py`

修复动作：
1. `G1`：当用户说 “paper account / 已支持 venue 添加” 且 connector 已存在时，agent 应调用 `account_upsert(mode='paper')`，不得要求真实 API key。`account_upsert` 已限制为非 live paper，工具说明要更明确。
2. 在 loop 中增加 account setup required-action heuristic：用户请求 “add account / paper account / 接入已支持交易所” 且 `account_upsert` 可用时，若模型只列凭证要求，应 nudge/force `account_upsert`。
3. `I3`：当 prompt 命中 `Wallet`、`Agentic Wallet`、`Bitget Wallet`、`Coinbase CDP SDK` 等，优先走 `data_api(provider='wallet', action='capability_catalog', args.preferred_provider=...)`，不要先走 exchange connector。
4. `I3`：Bitget Wallet 已在 `wallet/registry.py` 中声明默认动作不需要用户 API key；答复应说明 wallet provider/skill 路径，而不是 Bitget exchange API 凭证。
5. `I4`：Coinbase CDP ready 后，要么调用明确的钱包创建/打开 route，要么诚实说明已 ready、下一步的安全动作；不能停在 capability JSON。
6. 如果当前 data_api 只有 read-only catalog，没有 create wallet action，需要新增受权限保护的 `wallet_create` 或 `wallet.open` native action，默认走 paper/sandbox 或要求 approval。

验证：
- 单跑 `G1`, `I3`, `I4`。
- `G1` API check 应找到 `venue=kraken mode=paper`。
- `I3/I4` 回复必须包含 wallet/provider，且不要求无关 exchange API key。

### 6. 补 Deribit、Polymarket、dYdX、Gamma 的一等数据路由
覆盖 case：`G9`, `GX1`, `GX4`, `GX11`。

涉及文件：
- `nerya/tools/native/connectors.py`
- `nerya/tools/native/bootstrap.py`
- `nerya/connectors/polymarket.py`
- `nerya/connectors/ccxt_adapter.py`
- `nerya/data/funding.py`
- `nerya/charting/from_rows.py`
- `nerya/charting/composer.py`

修复动作：
1. 给 `market_data` 或 `data_api` 增加明确 read-only action：
   - `get_option_chain`
   - `get_event_odds`
   - `get_funding_rate`
   - `gamma_exposure`
2. `GX1`：Deribit option chain 应直接调用 Deribit/CCXT public route，按 expiry 和 strike 整形成表，不再让模型反复 web_fetch 原始 JSON。
3. `G9/C3`：Polymarket 应支持 event slug/关键词检索、事件赔率摘要、不可回测原因说明；没有 market 时返回 candidate list，而不是只返回 raw error。
4. `GX11`：dYdX public funding rate 不应要求私钥/助记词；为 `dydx_v4` public funding route 设置 credential-free path。
5. `GX4`：Gamma exposure 若依赖第三方 snapshot，应输出图表 artifact 或至少返回可渲染 chart data；没有完整数据时输出诚实报告，不显示内部 fetch dump。

验证：
- 单跑 `C3`, `G9`, `GX1`, `GX4`, `GX11`。
- 截图应显示表格/图表/自然语言报告，不能显示 Raw JSON。

### 7. 修测试前提和 brittle assertions
覆盖 case：`E2`, `E3`, `E6`, `H9`，以及辅助 `D9/F6`。

涉及文件：
- `dashboard/tests/e2e/csv-runner.spec.ts`
- `dashboard/tests/e2e/cases.timeout20m.csv`
- `tools/prepare_isolated_test_workspace.py`
- `dashboard/tests/e2e/fixtures.ts`

修复动作：
1. `E2/E3`：不要用 `must_contain=team` 判断团队能力。改成 API evidence：`tool_names` 包含 `team_run`，或 turn transition 为 `team_result_*`。
2. `E2/E3`：如果仍保留文本断言，允许 `团队|辩论|多角色|committee|team`。
3. `E6/H9`：本轮按用户要求使用真实 `~/.nerya/nerya.yml`，Financial Datasets ready=true 是合法环境状态。缺 key case 应使用 scrubbed workspace/env：
   - 不继承 `NERYA_FINANCIAL_DATASETS_KEYS`
   - 不拷贝 `vault://financial_datasets.keys`
   - 或在 CSV 加 `requires_fd_missing=1`，global setup 专门隔离。
4. `financial_datasets_status=false` 只应在 negative workspace 中断言；在真实本地配置 run 中应标记为 environment-precondition mismatch。

验证：
- 单跑 `E2`, `E3`, `E6`, `H9`。
- 分别验证真实本地配置和 scrubbed negative workspace 两种模式。

## 推荐执行顺序
1. 先修最终答复收束和 sanitizer，因为它影响 8 个失败 case，且可显著降低 Raw JSON 截图失败。
2. 再修 required artifact enforcement，因为它影响策略、provider、skill proposal 的核心成功率。
3. 接着修 strategy backtest/news hook 与 account/wallet routing。
4. 最后修 harness/test-precondition，避免把真实环境状态误判为产品失败。

## 回归命令
先单跑失败集：

```powershell
$env:NERYA_CASES_CSV="dashboard/tests/e2e/cases.timeout20m.csv"
$env:NERYA_WORKSPACE="dashboard/.nerya-local-config-20m-workspace"
$env:NERYA_API="http://127.0.0.1:18438"
$env:NERYA_E2E_SKIP_LLM_PROBE="1"
playwright test csv-runner --workers=1 --grep "A10|C3|C5|C7|C-AT6|C-AT9|D5|D9|E2|E3|E6|E10|F4|F6|G1|G9|GX1|GX4|GX6|GX11|H6|H9|I3|I4"
```

再跑全量：

```powershell
$env:NERYA_CASES_CSV="dashboard/tests/e2e/cases.timeout20m.csv"
$env:NERYA_WORKSPACE="dashboard/.nerya-local-config-20m-workspace"
$env:NERYA_API="http://127.0.0.1:18438"
$env:NERYA_E2E_SKIP_LLM_PROBE="1"
playwright test csv-runner --workers=1
```

验收标准：
- 全量 160 case 完成。
- 产品缺口 18 个 case 至少转为 pass 或明确、可接受的 finalized data-gap 状态。
- Raw JSON / Evidence markers 不再出现在用户可见最终答复。
- 环境前提类 case 在真实本地配置和 scrubbed negative workspace 下分别给出稳定、可解释结果。
