# Notes: Prompt Playwright Case Rerun

## 本地规则与入口
- 已检查 `Nerya/AGENTS.md`：不得泄露明文 secret；外部能力通过 runtime/tools/skills；dashboard 通过 API proxy 访问 runtime；E2E 在 `dashboard/tests/e2e`。
- 已检查 `dashboard/playwright.config.ts` 与 `dashboard/tests/e2e/csv-runner.spec.ts`。
- 最终入口：`playwright test csv-runner --workers=1`。
- 最终 CSV：`dashboard/tests/e2e/cases.timeout20m.csv`。
- 结果路径：
  - `dashboard/test-results/summary.csv`
  - `dashboard/test-results/logs/<case>.jsonl`
  - `dashboard/test-results/logs/<case>.reply.txt`
  - `dashboard/test-results/screenshots/<case>.png`
  - Playwright report/trace/video under `dashboard/test-results/`

## 最终运行配置
- CWD：`C:\Users\Ricky\Documents\Project\NeryaProject\Nerya`
- Runtime：`http://127.0.0.1:18438`
- Dashboard：`http://127.0.0.1:3028`
- Workspace：`dashboard/.nerya-local-config-20m-workspace`
- CSV 行数：160
- Timeout 检查：160 行全部为 `timeout_ms=1200000`，没有短 timeout 行。
- 本轮使用 `C:\Users\Ricky\.nerya\nerya.yml` 的 LLM tier/provider/model 路由；没有把用户提供的 API key 写入报告。
- 本地 LLM 配置摘要已脱敏：
  - `default_tier=medium`, `intent_tier=light`
  - `light`: provider `agnes`, model `agnes-2.0-flash`
  - `medium`: provider `mimo`, model `mimo-v2.5-pro`
  - `high`: provider `clipproxy`, model `gpt-5.5`
  - `intent`: provider `agnes`, model `agnes-2.0-flash`

## 最终结果
- 总数：160
- 通过：136
- 失败：24
- Playwright 总耗时：约 3.3 小时
- 长用例正常跑完，说明未被短 timeout 截断：
  - `GX1`: 571397 ms
  - `GX5`: 464355 ms
  - `E6`: 396871 ms
  - `C-AT6`: 381049 ms
  - `K2`: 196755 ms

## 代码证据点
- Reply quality gate：`dashboard/tests/e2e/csv-runner.spec.ts:506-548`
- Proposal/API checks：`dashboard/tests/e2e/csv-runner.spec.ts:792-837`
- Financial Datasets readiness check：`dashboard/tests/e2e/csv-runner.spec.ts:1056-1072`
- Fresh chat open behavior：`dashboard/tests/e2e/fixtures.ts:76-101`
- Required artifact forcing：`nerya/agent/loop.py:2003-2129`, `nerya/agent/loop.py:7873-7930`, `nerya/agent/loop.py:9740-9859`
- Strategy backtest finalizer：`nerya/agent/loop.py:10720-10823`
- Cancel endpoint/UI：`nerya/api/routes_agent.py:1535-1550`, `dashboard/components/chat/ChatView.tsx:1505-1541`
- Strategy news hook：`nerya/evolution/strategy_code_generator.py:308-346`, `nerya/evolution/strategy_code_generator.py:1342-1361`
- Script task id validation：`nerya/skills/builtin/tasks/scripts/create_task.py:82-96`, `nerya/skills/builtin/tasks/scripts/create_task.py:227-241`
- Account paper upsert support：`nerya/tools/native/accounts.py:36-83`, `nerya/tools/native/accounts.py:193-217`
- Market/data routing hints：`nerya/tools/native/bootstrap.py:1888-1959`, `nerya/tools/native/connectors.py:123-167`, `nerya/tools/native/connectors.py:429-441`
- Wallet provider metadata includes Bitget Wallet：`nerya/wallet/registry.py:281-327`
- Financial Datasets status derives from vault/env keys：`nerya/api/routes_data_sources.py:116-139`

## 失败 Case 判定

### 产品/实现缺口或输出收束缺口
| Case | 判定 | 主要证据 |
|---|---|---|
| `A10` | 取消 UI contract 缺口 | `A10.png` 只显示 error card；reply 为空；`cancel_inflight.done` 已记录。 |
| `C3` | Polymarket 不可回测答复收束失败 | 调了 connector/data/web/market 工具，但页面暴露 thinking/tool/Raw JSON。 |
| `C5` | 钱包/链上 smart-money 策略的 backtest finalization 缺口 | proposal 和 `strategy_backtest` 都运行，但 transition 是 `no_more_tools`，API 要求 finalized。 |
| `C7` | AgentTeam 策略 required artifact 未执行 | 只说会启动团队，未调用 `team_run`/`strategy_generate_proposal`/`strategy_backtest`。 |
| `C-AT6` | `news_social` 生成/挂钩缺口 | proposal/backtest 成功，但 `main.py` 没有 required `news_social`。 |
| `C-AT9` | 链上巨鲸策略 required artifact/tool routing 缺口 | 没有 proposal/backtest；日志显示工具收窄后未进入策略工具。 |
| `D5` | script schedule 参数归一化缺口 | fixture 中 approved script 存在，但模型传入路径型 `script_id`，工具只接受裸 id。 |
| `E10` | 团队分析到策略生成联动缺口 | 要 TSLA proposal/backtest，但没有任何策略工具调用。 |
| `F4` | Skill proposal routing + 输出收束缺口 | 没有调用 `evolve_skill_proposal`，页面暴露工具 dump。 |
| `G1` | paper account 创建行为缺口 | `account_upsert` 支持 paper，但模型停在要求 Kraken API 凭证，没有创建 `kraken:paper`。 |
| `G9` | Polymarket event odds 路由/收束缺口 | Polymarket connector 存在，工具调了但无正式赔率答复，页面暴露 dump。 |
| `GX1` | Deribit option chain 数据整形/收束缺口 | Deribit API 数据已抓取，最后没有按 strike 输出期权链，页面显示 Raw JSON 和 `(no reply returned)`。 |
| `GX4` | Gamma exposure 图/报告缺口 | 找到 CryptoGamma/Deribit 来源，但没有生成图或正式报告，只显示工具与 reasoning。 |
| `GX6` | Aster provider proposal 强制执行缺口 | evidence contract 要 `evolve_provider_proposal`，模型只说要创建 proposal，未调用工具。 |
| `GX11` | dYdX 公共资金费率路由/收束缺口 | `market_data` 对 public funding 返回 credential missing，fallback web 后仍无正式答复。 |
| `H6` | Evidence markers 泄漏 | NVDA 财报查询本身完成，但尾部把 `web_fetch`/`web_search_fetch` JSON 作为用户答复显示。 |
| `I3` | Wallet/provider 语义路由缺口 | “Bitget Wallet” 被当成 Bitget 交易所账户，未走 wallet provider。 |
| `I4` | Coinbase wallet route 收束/执行缺口 | `wallet.capability_catalog` 返回 `COINBASE_WALLET ready=True`，但没有创建/说明 wallet，页面显示 data_api dump。 |

### 非产品实现缺口，需调整测试前提或哨兵
| Case | 判定 | 主要证据 |
|---|---|---|
| `D9` | harness/session reuse 问题 | `openChat` 总是 `page.goto("/chat")`，即使 `reset_before=0` 也没有显式恢复 D8 thread。 |
| `E2` | brittle assertion | `team_run` 成功且中文答复有辩论内容，只因 `must_contain=team` 英文哨兵失败。 |
| `E3` | brittle assertion | `team_run`、proposal、backtest 都成功，只因 `must_contain=team` 英文哨兵失败。 |
| `E6` | 环境前提冲突 | 本地配置让 Financial Datasets ready=true，但 case 预期 key 缺失降级。 |
| `F6` | fixture/上下文依赖问题 | 当前 isolated workspace 没有 “C1 策略”；模型合理要求澄清。 |
| `H9` | 环境前提冲突 | 本地配置让 Financial Datasets ready=true，但 case 的 `financial_datasets_status=false` 仍按缺 key 断言。 |

## 截图抽查
- `A10.png`: 可见 `Turn failed · Error` 和取消提示，但没有正常 assistant reply。
- `C3.png`: Polymarket 页面显示 `market_data` structured/raw JSON。
- `GX1.png`: Deribit API JSON 和写文件结果直接显示，底部 `(no reply returned)`。
- `GX4.png`: Gamma 页面停在 fetch/reasoning，未生成图或报告。
- `GX6.png`: 只显示“然后创建 provider proposal”，实际没有 proposal 工具调用。
- `H6.png`: 正文后直接显示 Evidence markers 的 JSON。
- `I3.png`: Bitget Wallet 被回答成 Bitget 交易所 API 凭证接入。
- `I4.png`: Coinbase wallet readiness/capability JSON 直接显示，底部 `(no reply returned)`。
