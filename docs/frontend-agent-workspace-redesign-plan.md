# Nerya 前端对话与管理工作台彻底重构优化清单

更新时间：2026-04-28

本文给 Cursor / 前端重构使用，目标是把 Nerya dashboard 从“静态管理页面 + 简陋聊天框”重构成真正的 Agent 操作工作台：用户能在对话页面清楚看到任务进展、工具调用、审批、diff、错误、验证结果；非对话模块也能进行可编辑、可审批、可回滚、可追踪的管理操作。

---

## 1. 当前问题判断（基于代码证据）

### 1.1 Chat 页面无法清楚表达 Agent 进展

当前 `ChatView` 的模式是：用户发消息后，前端轮询 `/agent/stream/events`，把新事件追加到最后一条 assistant message 的 `live_events` 上。

证据：

- `Nerya/dashboard/components/chat/ChatView.tsx:81` — ChatView 本地维护 threads/activeId/input/sending。
- `Nerya/dashboard/components/chat/ChatView.tsx:206` — run turn 前只抓取 stream cursor。
- `Nerya/dashboard/components/chat/ChatView.tsx:234` — `pollOnce` 轮询 stream events。
- `Nerya/dashboard/components/chat/ChatView.tsx:250` — fresh events 追加到 assistant message 的 `live_events`。
- `Nerya/dashboard/components/chat/ChatView.tsx:288` — 仍然通过 `/agent/run_turn` 一次性获取 turn result。
- `Nerya/dashboard/components/chat/ChatView.tsx:297` — run_turn 返回后再 drain 一次尾部事件。
- `Nerya/dashboard/components/chat/LiveActivity.tsx:10` — LiveActivity 只是消费 `/agent/stream/events` 的事件展示。
- `Nerya/dashboard/components/chat/LiveActivity.tsx:19` — 按 tool lifecycle grouping，但数据来源依赖现有事件质量。
- `Nerya/dashboard/components/chat/LiveActivity.tsx:28` — 注释中列出 message.delta/tool.progress/approval.request/turn.step/turn.complete。

问题：

- 事件被挂在“最后一条消息”上，不是独立的任务时间线。
- 用户无法稳定看到“当前正在做什么、已完成什么、卡在哪、下一步是什么”。
- 没有强制展示 tool_use/tool_result 配对、diff、审批、验证结果。
- 轮询模型容易丢尾部/乱序/重复，体验不如 SSE/WebSocket。
- 失败时只显示 error string，缺少 recovery action。

### 1.2 Chat 不是 Agent 工作台，只是消息列表

证据：

- `Nerya/dashboard/components/chat/ChatInput.tsx` — 输入组件只负责提交文本。
- `Nerya/dashboard/components/chat/ChatSidebar.tsx` — 侧边栏只管本地 thread 列表。
- `Nerya/dashboard/lib/chat.ts` — chat thread/message/event 是前端本地结构。
- `Nerya/dashboard/lib/clientApi.ts:812` — `streamEvents` 注释明确是 polling bus。
- `Nerya/dashboard/lib/clientApi.ts:821` — streamEvents 仍然是 GET 轮询。

问题：

- thread 状态主要在浏览器 local state/localStorage，不是后端 session transcript 的真实视图。
- 缺少 session/turn/task 级导航：用户不能按任务、turn、工具、文件、审批筛选。
- 缺少“任务卡片”：目标、计划、TODO、当前步骤、风险、验证一屏展示。

### 1.3 非对话管理页功能割裂、编辑僵硬

证据：

- `Nerya/dashboard/app/agents/page.tsx:21` — agents 页分 sessions/recovery/run/explain tabs。
- `Nerya/dashboard/app/agents/page.tsx:296` — RunTurnPanel 让用户手写 kind/target/payload JSON。
- `Nerya/dashboard/app/agents/page.tsx:344` — payload 是裸 textarea JSON。
- `Nerya/dashboard/app/agents/page.tsx:378` — explain/trace 也靠手填 id 表单。
- `Nerya/dashboard/app/subagents/page.tsx:33` — subagents 页是独立 CRUD 表单。
- `Nerya/dashboard/app/scripts/page.tsx` — scripts 管理走 skill call，不是统一 artifact/proposal 工作流。
- `Nerya/dashboard/app/skills/page.tsx` — skills 页面偏列表/查看，缺少标准 skill playbook/script 管理。
- `Nerya/dashboard/app/strategies/page.tsx:458` — strategy config/limits/prompts 直接编辑 JSON textarea。
- `Nerya/dashboard/lib/clientApi.ts:317` — strategy 管理通过 `/skills/call`。
- `Nerya/dashboard/lib/clientApi.ts:409` — subagent 管理通过 `/skills/call`。
- `Nerya/dashboard/lib/clientApi.ts:462` — script 管理通过 `/skills/call`。

问题：

- 管理页普遍是“表单 + JSON textarea + 保存按钮”，缺少 schema-aware editor、diff preview、validation、approval、rollback。
- 资源对象没有统一模型：strategy/script/subagent/skill/memory/trigger/session 各自一套 UI。
- 编辑动作不能自然关联到 Agent 对话：用户不知道这次改动来自哪个任务、哪个审批、哪个 diff、哪个验证。
- 许多页面暴露底层 JSON，而不是操作员可理解的产品界面。

### 1.4 API 客户端仍强化 skill-call 管理方式

证据：

- `Nerya/dashboard/lib/clientApi.ts:294` — skills 列表只返回 id/version/permissions/actions。
- `Nerya/dashboard/lib/clientApi.ts:317` — strategy list 通过 `skill_id: strategy, action: list`。
- `Nerya/dashboard/lib/clientApi.ts:342` — strategy create 通过 `/skills/call`。
- `Nerya/dashboard/lib/clientApi.ts:409` — subagent list 通过 `/skills/call`。
- `Nerya/dashboard/lib/clientApi.ts:477` — script proposal 通过 `/skills/call`。
- `Nerya/dashboard/lib/clientApi.ts:791` — agent run turn 是独立 operator plane。
- `Nerya/dashboard/lib/clientApi.ts:807` — open turns 是独立接口。
- `Nerya/dashboard/lib/clientApi.ts:821` — stream events 又是另一套接口。

问题：

- 前端没有统一的 `WorkspaceResource API`。
- 所有管理功能都像“调用某个 skill action”，而不是“编辑一个可版本化资源”。
- 很难实现统一的权限、diff、审批、审计、回滚、验证。

### 1.5 信息架构失控：菜单数量多，但用户任务不清楚

当前 dashboard 侧边栏硬编码了 16 个一级入口，把运行态、开发态、调试态、底层资源和操作员任务混在一起。

证据：

- `Nerya/dashboard/lib/nav.ts:3` — `NAV` 是静态数组，不从能力、权限、模式生成。
- `Nerya/dashboard/lib/nav.ts:4` — 首页入口叫 `Command Center`。
- `Nerya/dashboard/lib/nav.ts:7` — `Automation` 单独暴露为一级菜单，对应 trigger/routes/schedules。
- `Nerya/dashboard/lib/nav.ts:13` — `Skills` 是一级菜单。
- `Nerya/dashboard/lib/nav.ts:14` — `Scripts` 是一级菜单。
- `Nerya/dashboard/lib/nav.ts:15` — `Memory` 是一级菜单。
- `Nerya/dashboard/lib/nav.ts:17` — `Messages` 是一级菜单。
- `Nerya/dashboard/lib/nav.ts:18` — `Security` 是一级菜单。
- `Nerya/dashboard/lib/nav.ts:19` — `Settings` 是一级菜单。

问题：

- 顶层菜单不是按用户目标组织，而是把内部模块逐个摊开。
- `Trigger` / 用户口中的 `Triggle` 本质是 SDK/自动化入口，是“某个策略或任务的触发条件”，不应该默认成为独立顶层产品页面。
- Skills、Scripts、Memory、Messages、Subagents、Evolution 多数是 Agent 工作台的辅助资源或高级调试面板，不应和 Chat/Portfolio/Strategy 处在同一主导航层级。
- 普通操作员进入页面后不知道应该先点什么，也不知道哪些页面是日常使用、哪些只是调试/开发/审计。
- 缺少 mode / role / capability gating：不接入交易、不启用 live、不启用 messaging、不启用 evolution 时，对应菜单仍然存在。

### 1.6 Trigger/Automation 页面产品语义错误

`Trigger` 在 Nerya 架构里是外部事件、cron、SDK、路由和策略 session 的连接层，不是用户每天单独“管理 Trigger 产品”的目的地。

证据：

- `Nerya/docs/trigger-sdk.md:1` — Trigger 被定义为 SDK。
- `Nerya/docs/runtime-ownership.md:14` — `TriggerEvent` 进入 `TriggerRouter`。
- `Nerya/dashboard/lib/clientApi.ts:717` — clientApi 把 triggers/schedules 标为 operator plane。
- `Nerya/dashboard/app/triggers/page.tsx:369` — 页面直接展示 `Trigger routes`。
- `Nerya/dashboard/app/triggers/page.tsx:411` — 添加/编辑 trigger route 需要手填 target / strategy_id / limit 字段。
- `Nerya/dashboard/app/triggers/page.tsx:420` — match 条件仍是裸 JSON textarea。
- `Nerya/dashboard/app/triggers/page.tsx:433` — dry run trigger 是调试动作，不是核心业务入口。

问题：

- Trigger 应该嵌入 Strategy / Workflow / Agent Task 详情里：例如“这个策略由哪些事件、计划任务、外部 webhook 触发”。
- 对开发者可以保留 `Automation Studio` 或 `Trigger Debugger`，但默认隐藏在 Advanced / Developer Tools。
- 顶层只应展示“自动化任务/工作流”的业务结果，不应展示 route/match/payload 这些 SDK 结构。
- Route、Schedule、Dry-run 应统一走 schema form + diff + validation + approval，而不是一页里混合表格、表单和裸 JSON。

### 1.7 首页重点错位：K 线不应压过资金曲线和任务状态

当前 Command Center 既抓 portfolio/equity，又抓 candle，但首屏重点仍偏“市场行情终端”。对 Nerya 这种 agent/trading workspace，首页最重要的是资金曲线、风险、持仓、活跃任务、待审批、异常和最近决策，而不是单个市场 K 线。

证据：

- `Nerya/dashboard/app/dashboard/page.tsx:25` — 首页引入 `CandleChart`。
- `Nerya/dashboard/app/dashboard/page.tsx:40` — 首页内置 K 线周期选项。
- `Nerya/dashboard/app/dashboard/page.tsx:61` — 首页也维护 equity curve state。
- `Nerya/dashboard/app/dashboard/page.tsx:90` — 首页请求 portfolio summary。
- `Nerya/dashboard/app/dashboard/page.tsx:93` — 首页请求 `portfolioEquityCurve(120)`。
- `Nerya/dashboard/app/dashboard/page.tsx:110` — 首页单独加载 candles。
- `Nerya/dashboard/app/dashboard/page.tsx:383` — 首页渲染 `CandleChart`。
- `Nerya/dashboard/app/dashboard/page.tsx:396` — 资金曲线只是后续卡片 `Equity Curve`。
- `Nerya/dashboard/app/dashboard/page.tsx:660` — Runtime footer 只展示 Skills/Routes/Proposals/Workspace 数字。

问题：

- K 线只能回答“某个市场现在怎样”，不能回答“我的 agent 有没有赚钱、有没有失控、哪里需要我处理”。
- 首页应该优先展示 `Equity Curve`、PnL attribution、drawdown、exposure、risk gate、open orders、recent decisions、active tasks、pending approvals、last error。
- K 线保留为“选中持仓/策略/订单时的上下文图”，不应作为默认首页核心模块。
- Runtime footer 的 Skills/Routes 数量对用户价值低，应替换成 agent health、队列延迟、失败任务、待审批、未验证变更、连接状态。

### 1.8 太多页面在展示“系统内部”，不是展示“用户下一步要做什么”

当前多个一级页的存在是技术上合理、产品上不合理：它们把内部能力暴露为菜单，而没有解释“我为什么要看它、什么时候要改它、改错会怎样”。

需要重新判定的页面：

- `Subagents`：应作为 Agent Workspace 的执行资源/团队面板；默认不需要独立一级菜单。
- `Skills`：应是 playbook/resource browser；默认在 Advanced，且按被当前任务调用时浮出。
- `Scripts`：应是 artifact/proposal/code-run 资源；从 Chat/Strategy 跳转，不应逼用户先理解脚本系统。
- `Memory`：应以搜索、引用、事实来源出现在 TaskInspector；编辑入口必须谨慎，不应成为日常顶层入口。
- `Evolution`：高风险自进化能力，默认必须隐藏到 Advanced/Approval，不应和日常操作并列。
- `Messages`：如果只是出站消息列表，应合并进 Notifications / Integrations；只有当用户在做多渠道运营时才显示一级入口。
- `Security`：应在 Settings 下作为安全/权限/审批分组，危险操作强提示。
- `Orders` / `Strategy History` / `Portfolio`：这些是交易核心，可以保留一级，但需要按“资金-风险-订单-策略表现”重新组织。

结论：重构不是“换 UI 皮肤”，而是先砍掉默认一级菜单，把入口改成：`Home`、`Agent Workspace`、`Portfolio`、`Strategies`、`Workflows`、`Action Inbox`、`Settings`，其余通过资源抽屉、命令面板、上下文链接、Advanced mode 进入。

### 1.9 交易核心页有数据，但缺少“下一步行动”

Portfolio、Orders、Strategy History 已经比纯调试页更接近用户任务，但仍然主要是表格、筛选、JSON inspector，不能主动告诉用户哪里危险、哪里要处理。

证据：

- `Nerya/dashboard/app/portfolio/page.tsx:82` — Portfolio 定位为 balances/live-paper/open positions/PnL/equity curve。
- `Nerya/dashboard/app/portfolio/page.tsx:90` — Portfolio 展示 Equity KPI。
- `Nerya/dashboard/app/portfolio/page.tsx:92` — Portfolio 展示 Realized PnL。
- `Nerya/dashboard/app/portfolio/page.tsx:129` — Equity curve 已存在，但缺少 drawdown、risk attribution、benchmark。
- `Nerya/dashboard/app/portfolio/page.tsx:154` — PnL 详情仍可退回 raw JSON。
- `Nerya/dashboard/app/portfolio/page.tsx:163` — Open positions 是扁平表格。
- `Nerya/dashboard/app/portfolio/page.tsx:202` — Account summary 仍展示 raw JSON。
- `Nerya/dashboard/app/orders/page.tsx:112` — Orders 页面按 strategy 查询。
- `Nerya/dashboard/app/orders/page.tsx:88` — Cancel 只用浏览器 confirm，没有风险预览或影响说明。
- `Nerya/dashboard/app/orders/page.tsx:242` — Orders inspector 展示 selected order / last action raw JSON。
- `Nerya/dashboard/app/strategy-history/page.tsx:121` — Strategy History 同时承担 review、attribution、divergence、scenario replay。
- `Nerya/dashboard/app/strategy-history/page.tsx:228` — Event row 仍截断展示 JSON。

问题：

- 用户看得到数字，但不知道“现在要不要处理”。
- Orders 的 cancel 应该展示订单影响、仓位影响、关联策略、是否会触发 recovery，而不是一个 confirm。
- Portfolio 应该是首页和交易核心的事实来源：资金曲线、回撤、暴露、风险门、持仓异常都应在这里被解释。
- Strategy History 不应只是事件表，应变成“策略表现/复盘/归因/异常”的分析页。

### 1.10 Agent/Developer 页混合了用户工作台和调试面

Agents、Subagents、Skills、Scripts 这些页面对开发有用，但默认暴露给普通用户会制造困惑。

证据：

- `Nerya/dashboard/app/agents/page.tsx:19` — Agents 页 tab 包含 `sessions`、`recovery`、`run`、`explain`。
- `Nerya/dashboard/app/agents/page.tsx:334` — `Run one turn` 是直接触发内部 turn 的调试面。
- `Nerya/dashboard/app/agents/page.tsx:344` — Payload 需要手写 JSON。
- `Nerya/dashboard/app/agents/page.tsx:367` — Run result 仍显示 JSON。
- `Nerya/dashboard/app/agents/page.tsx:412` — Explain/trace 需要手填 correlator。
- `Nerya/dashboard/app/subagents/page.tsx:203` — Subagents 页面管理 prompt files。
- `Nerya/dashboard/app/subagents/page.tsx:296` — test task 是自由文本 + strategy_id。
- `Nerya/dashboard/app/subagents/page.tsx:302` — test result 仍显示 JSON。
- `Nerya/dashboard/app/subagents/page.tsx:326` — system prompt 是大 textarea。
- `Nerya/dashboard/app/skills/page.tsx:65` — Skills 页面说明是通过 `/skills/call` 试 action。
- `Nerya/dashboard/app/skills/page.tsx:122` — skill payload 是 JSON textarea。
- `Nerya/dashboard/app/scripts/page.tsx:265` — Scripts 页面暴露 pending/approved 文件生命周期。
- `Nerya/dashboard/app/scripts/page.tsx:210` — Delete script 提示 irreversible。
- `Nerya/dashboard/app/scripts/page.tsx:547` — Run script args 仍是 JSON textarea。

问题：

- 普通用户不应该被要求理解 session、turn、payload、skill action、pending script、manifest。
- 这些能力应该被 Chat/Task/Strategy 串起来：用户提出目标，系统生成/测试/审批/运行；页面只作为产物详情和高级调试。
- Subagent 应该是“角色/团队成员/能力卡”，而不是裸 prompt 文件编辑器。
- Skills 应该是 `SKILL.md` playbook 浏览和启停/安装状态，不是 `/skills/call` 控制台。
- Scripts 应该是任务产物和可审计工具库，默认只展示用途、风险、输入表单、最近运行、审批状态。

### 1.11 Settings/Security 重复且偏 UI 偏好，不像控制台配置

Settings 页有 11 个 tab，包含本地 UI 偏好、K 线偏好、风险、通知、隐私、安全、集成、数据导入导出。Security 页又单独复用 IntegrationsPanel，导致“在哪里配置 provider/wallet/risk/approval”不清楚。

证据：

- `Nerya/dashboard/app/settings/page.tsx:9` — Settings 定义 11 个 tab。
- `Nerya/dashboard/app/settings/page.tsx:11` — `Trading Preferences` 是 tab。
- `Nerya/dashboard/app/settings/page.tsx:12` — `Risk Management` 是 tab。
- `Nerya/dashboard/app/settings/page.tsx:14` — `Alerts & Triggers` 是 tab。
- `Nerya/dashboard/app/settings/page.tsx:17` — `Privacy & Security` 是 tab。
- `Nerya/dashboard/app/settings/page.tsx:359` — chart type 仍是主要设置项。
- `Nerya/dashboard/app/settings/page.tsx:384` — number of candles 仍占设置入口。
- `Nerya/dashboard/app/settings/page.tsx:409` — Clear Cache 直接清 localStorage。
- `Nerya/dashboard/app/settings/page.tsx:457` — Reset All Settings 直接 patch 默认设置。
- `Nerya/dashboard/app/settings/page.tsx:466` — 设置主要持久化在浏览器 storage。
- `Nerya/dashboard/app/security/page.tsx:10` — Security & Integrations 也是 provider/wallet/exchange readiness。
- `Nerya/dashboard/app/security/page.tsx:14` — Security 页直接渲染 IntegrationsPanel。

问题：

- Settings 应拆成 `Runtime Config`、`Integrations`、`Risk & Approval`、`Notifications`、`UI Preferences`。
- UI preference 可以保留本地 storage，但 provider、wallet、risk、live mode、approval policy 不能看起来像普通前端偏好。
- Security 和 Settings 不应重复；Security 应是 Settings 下的安全分组，或 Settings 直接分组展示。
- Clear cache / reset settings 必须说明会影响哪些内容，不能误删 session/task 的感知状态。

### 1.12 Evolution/Messages/Memory 应从“菜单页”变成上下文能力

这些页面是有价值的，但价值来自特定上下文：某个任务产生了 proposal、某个策略产生了记忆、某个 channel 发出了告警。单独作为主菜单会让用户不知道为什么要进去。

证据：

- `Nerya/dashboard/app/evolution/page.tsx:125` — Evolution 管理 self-improvement proposals。
- `Nerya/dashboard/app/evolution/page.tsx:140` — Reflection and ranking 需要用户理解 proposal ranking。
- `Nerya/dashboard/app/evolution/page.tsx:192` — apply proposal 是高风险动作。
- `Nerya/dashboard/app/evolution/page.tsx:223` — ranking/evidence/action output 仍显示 JSON。
- `Nerya/dashboard/app/memory/page.tsx:176` — Memory remember 是自由文本 textarea。
- `Nerya/dashboard/app/messages/page.tsx:157` — Messages send 是自由文本 textarea。
- `Nerya/dashboard/app/messages/page.tsx:184` — Message record details 仍显示 JSON。

问题：

- Evolution 默认应隐藏在 `Approvals` / `Proposals` / `Advanced`，所有 apply/rollback 必须带 diff、风险、验证和回滚计划。
- Memory 应主要作为 Chat/Task 的引用来源和事实编辑 proposal，而不是普通记事本。
- Messages 应合并为 Notifications/Outbox，并从任务、策略、告警跳转；普通用户不该面对 raw record。

### 1.13 全局 Shell 像“装饰在线”，不是可操作状态栏

Sidebar / TopHeader 目前有在线灯、搜索、通知、设置按钮，但缺少真实跳转、真实通知列表和可处理事项。这样会让用户误以为系统健康，但其实不知道 provider、wallet、stream、任务队列哪里坏了。

证据：

- `Nerya/dashboard/components/Sidebar.tsx:24` — sidebar 折叠状态写 localStorage。
- `Nerya/dashboard/components/Sidebar.tsx:41` — nav 直接从静态 `NAV` 分组。
- `Nerya/dashboard/components/Sidebar.tsx:89` — agent profile 固定显示 `ONLINE`。
- `Nerya/dashboard/components/TopHeader.tsx:67` — TopHeader 只有一个 `online` boolean。
- `Nerya/dashboard/components/TopHeader.tsx:135` — Search 按钮只是 icon button。
- `Nerya/dashboard/components/TopHeader.tsx:138` — Notifications 按钮只是 icon button。
- `Nerya/dashboard/components/TopHeader.tsx:144` — Settings 按钮只是 icon button。
- `Nerya/dashboard/components/Page.tsx:114` — `Json` 是通用 raw JSON 展示组件，很多页面依赖它兜底。
- `Nerya/dashboard/components/Page.tsx:128` — `Empty` 只有一行 No data 式文案。
- `Nerya/dashboard/components/Page.tsx:134` — `ErrorBanner` 只显示错误字符串，没有 recovery action。

问题：

- 全局在线状态应拆成 API、stream、LLM provider、wallet/exchange、risk gate、queue、last event；不能只显示 ONLINE。
- Search 必须变成 command palette：搜索策略、任务、订单、脚本、skill、设置项，并支持执行动作。
- Notifications 必须展示 pending approvals、failed tasks、risk alerts、provider errors，而不是红点装饰。
- Empty/Error 状态必须给出“为什么为空/失败”和下一步行动：create strategy、connect provider、open logs、retry、go settings。
- Raw JSON 只能作为 Debug 展开项，不能成为默认用户信息架构。

### 1.14 Integrations/LLM 配置是关键，但缺少向导和安全预检

IntegrationsPanel 和 LlmOpsPanel 已经有 provider、vault、模型、routing 信息，但使用方式仍偏工程控制台：用户需要知道 vault、tier、routing、require_parameters、data_collection 等底层概念。

证据：

- `Nerya/dashboard/components/IntegrationsPanel.tsx:149` — Disable on-chain 直接调用 walletUse 空值。
- `Nerya/dashboard/components/IntegrationsPanel.tsx:193` — 删除 secret 仍用浏览器 confirm。
- `Nerya/dashboard/components/IntegrationsPanel.tsx:216` — On-chain Wallet Providers 需要用户理解 provider readiness。
- `Nerya/dashboard/components/IntegrationsPanel.tsx:313` — Encrypted Secrets Vault 暴露 secret 表单。
- `Nerya/dashboard/components/IntegrationsPanel.tsx:325` — secret value 是明文输入框。
- `Nerya/dashboard/components/LlmOpsPanel.tsx:169` — LLM Providers 展示 provider readiness。
- `Nerya/dashboard/components/LlmOpsPanel.tsx:221` — LLM Tiers 展示 tier -> provider/model。
- `Nerya/dashboard/components/LlmOpsPanel.tsx:266` — Model Catalog 需要手动 Refresh models。
- `Nerya/dashboard/components/LlmOpsPanel.tsx:316` — Provider Routing 暴露 OpenRouter-style routing。
- `Nerya/dashboard/components/LlmOpsPanel.tsx:346` — 用户直接切 `require_parameters`。
- `Nerya/dashboard/components/LlmOpsPanel.tsx:355` — 用户直接切 `data_collection = deny`。

问题：

- Integrations 应改成 setup wizard：目标是“让 agent 能安全运行”，不是让用户填 vault 表。
- LLM 配置应展示“可用/不可用原因/一键测试/推荐模型/成本风险/隐私策略”，而不是只展示 tier 表。
- Secret 删除、wallet disable、routing 改动都应有影响预览：哪些策略/任务/provider 会受影响。
- 明文 secret 输入框必须明确只用于写入、写入后立即清空、不可回显，并给出 vault ref 的使用位置。

### 1.15 最新后端能力已经变丰富，但前端没有吃进产品形态

最新代码里，后端已经不只是旧的 skills/triggers/portfolio 几个接口，而是补出了运行时能力矩阵、发现接口、审批接口、团队运行接口、路由权限矩阵、gateway 平台状态等更接近“控制台事实源”的能力。但当前前端仍然把这些能力散落到 Settings、Strategies、Agents、Security 等页面，没有形成用户能理解的一屏状态和下一步行动。

证据：

- `Nerya/nerya/api/local_server.py:37` — 本地 API 注册了 workspace、agent、skills、triggers、trading、LLM、history、scripts、messages、evolution、security、market、portfolio、wallet、exchanges、discovery、dev、capability、approvals、provider_auth、gateway、teams。
- `Nerya/nerya/api/routes_capability.py:331` — 已有 `/runtime/capability_matrix`、`/runtime/recipes`、`/runtime/dashboard_extensions`、`/runtime/operator_presets`、`/runtime/model_registry`、`/runtime/mcp_dynamic`。
- `Nerya/nerya/api/routes_discovery.py:134` — 已有 `/discovery`、accounts、wallets、venues、markets、lifecycle 枚举。
- `Nerya/nerya/api/routes_approvals.py:225` — 已有 pending approval、approval prompt、approval callback。
- `Nerya/nerya/api/routes_teams.py:112` — 已有 team templates、runs、run detail、start run。
- `Nerya/nerya/api/route_scopes.py:47` — 已有 read/write/trade/approve/gateway/admin scopes。
- `Nerya/dashboard/lib/clientApi.ts:330` — 前端只把 discovery 当成 strategy/settings 下拉数据源。
- `Nerya/dashboard/lib/clientApi.ts:289` — clientApi 没有 product-level 的 `overview`、`actionInbox`、`capabilityMatrix`、`teamRuns`、`approvalInbox` 聚合方法。

问题：

- 后端已经能回答“当前 runtime 有哪些能力、哪些权限、哪些 gateway、哪些 recipe 可用”，但前端菜单仍是静态 `NAV`，无法根据能力和权限隐藏噪音。
- 后端已有 approvals，但前端没有 Action Inbox 作为核心入口，pending approval 仍然只是各页面的零散概念。
- 后端已有 team runs，但前端仍把 Subagents 当 prompt 文件管理页，没有把 team run 表达成“谁在帮我做任务、结果是什么、还有什么阻塞”。
- 后端已有 route scopes，但按钮和危险动作没有根据 scope/readiness/risk gate 做显隐、禁用和解释。
- 后端已有 dashboard extensions 设计，但前端没有 extension slot，导致能力只能继续靠固定页面堆菜单。

结论：下一步不是继续加菜单，而是让前端消费 `/runtime/capability_matrix`、`/discovery`、`/approvals/*`、`/teams/*`、`/agent/*`，组合成用户导向的 Home、Action Inbox、Agent Workspace、Setup Wizard 和 Advanced Tools。

### 1.16 API 粒度仍偏内部模块，不是用户任务契约

当前 API 已经比之前更全，但路径和返回结构仍大多按内部模块切分：agent、skills、triggers、portfolio、messages、evolution、teams、security。前端为了展示一个首页或一个策略详情，需要自己拼很多端点和 JSON 结构，这会继续制造乱页面。

证据：

- `Nerya/dashboard/app/dashboard/page.tsx:84` — 首页并发拉 workspace、skills、trigger routes、proposals、messages、portfolio summary、strategy list、recent trades、equity curve、venues。
- `Nerya/nerya/api/routes_portfolio.py:147` — portfolio API 提供 summary/positions/pnl/equity_curve/strategy list/recent trades，但没有 drawdown、exposure、risk alerts、PnL attribution、attention items 聚合。
- `Nerya/nerya/api/routes_agent.py:536` — agent API 提供 run_turn、trace、explain、open_turns、sessions、stream、interrupt、tools，但没有一等 `AgentTask` / `TaskTimeline` / `TaskArtifacts`。
- `Nerya/nerya/api/routes_triggers.py:148` — triggers API 暴露 emit、dry_run、routes、schedules、stats、replay，更像 SDK/debugger，而不是用户要理解的 workflow 契约。
- `Nerya/nerya/api/routes_evolution.py:69` — evolution API 是 proposals/apply/rollback/reflect/rank/evidence，但没有和 approvals、diff、verification 合并成用户可处理的 proposal item。
- `Nerya/nerya/api/routes_messages.py:5` — messages API 是 send/list，不足以支撑 notification/action inbox 的 severity、source、requires_action、resolution 状态。
- `Nerya/dashboard/lib/api.ts:5` 和 `Nerya/dashboard/app/api/proxy/[...path]/route.ts:18` — dashboard 默认 API base 仍是 `127.0.0.1:8787`；README/service flow 又强调 `18317`，启动路径需要明确单一配置契约。

问题：

- 前端会继续变成“页面里拼十几个接口 + raw JSON inspector”，而不是稳定产品契约。
- API 缺少用户任务级聚合：`What needs attention`、`Portfolio health`、`Strategy workspace`、`Workflow detail`、`Action Inbox`。
- API 缺少“影响预览”契约：取消订单、删除 secret、disable wallet、改 routing、apply proposal、修改 schedule 前，前端不知道会影响哪些策略/任务。
- API 缺少“schema form”契约：strategy config、trigger match、script args、LLM routing、wallet config 都应由后端返回 schema/defaults/validation，而不是前端硬写 textarea。
- API 权限矩阵已经存在，但前端没有拿它驱动页面级可见性和按钮级 disabled reason。

结论：后端要补一个 operator-facing BFF/API 层，把内部模块接口包装成用户任务接口；原始模块接口保留给 Advanced/Debug。

### 1.17 前端路由和后端路由缺少统一生命周期

前端现在有页面，但后端没有对应的产品级路由；后端有产品事实，但前端没有对应页面。这是“看起来很多功能，其实用户不知道从哪下手”的核心原因。

必须统一的生命周期：

1. **Setup**：发现缺什么配置，告诉用户先做什么。
2. **Operate**：Home 展示资金、风险、任务、审批、错误。
3. **Investigate**：从资金/订单/策略/任务跳到对应 trace/evidence。
4. **Change**：所有配置变更先 preview diff/impact，再 approval。
5. **Verify**：测试、dry run、risk check、provider check 作为结果卡展示。
6. **Recover**：失败任务、断开的 provider、未完成 turn、风险告警必须有恢复动作。

这意味着：

- `/dashboard` 不应自己拼所有东西，应消费 `/operator/overview` 或 `/dashboard/summary`。
- `/chat` 不应只有 local thread，应消费 `/agent/tasks`、`/agent/tasks/{id}/timeline`、`/agent/tasks/{id}/artifacts`。
- `/portfolio` 不应只读 summary/positions，应消费 `/portfolio/health` 和 `/portfolio/explain_pnl`。
- `/strategies` 不应靠 `/skills/call` 管策略资源，应有 `/strategies/*` 一等 resource API。
- `/triggers` 不应默认暴露，应变成 `/workflows/*` 的实现层。
- `/messages`、`/evolution`、`/approvals` 应收敛为 `/inbox/items`。
- `/settings` 应消费 `/setup/readiness`、`/settings/impact_preview`、`/runtime/capability_matrix`。

---

## 2. 目标产品形态

### 2.1 Chat 页面升级为 Agent Workspace

新的对话页不再只是消息列表，而是四栏/三栏 Agent 工作台：

1. **左侧：会话与任务导航**
  - Sessions
  - Active turns
  - Pinned tasks
  - Recent artifacts
  - Failed / waiting approval
2. **中间：对话与任务时间线**
  - 用户消息
  - Assistant streaming text
  - Plan / Todo cards
  - Tool call groups
  - Approval cards
  - Diff cards
  - Test/verification cards
  - Final report
3. **右侧：上下文与产物面板**
  - 当前目标
  - TODO
  - Files read/modified
  - Commands run
  - Approvals
  - Errors/recovery
  - Skills/MCP used
4. **底部：增强输入区**
  - 普通提问
  - Plan mode toggle
  - Attach file/context
  - Mention resource: strategy/script/subagent/skill/session
  - Permission mode selector
  - Run/stop/resume controls

### 2.2 管理页升级为 Resource Studio

把非对话模块统一为 Resource Studio 模式：

- List：资源列表，可搜索、过滤、标签、状态。
- Detail：资源详情，可查看历史、依赖、使用位置。
- Edit：schema-aware 编辑器，不再裸 JSON。
- Diff：保存前展示 diff。
- Validate：保存前静态校验/运行轻量检查。
- Propose：高风险变更生成 proposal。
- Approve：审批后应用。
- Rollback：版本回滚。
- Link to chat：每次改动可回到触发它的对话/任务。

适用资源：

- Strategies
- Scripts
- Subagents
- Skills
- MCP servers/resources
- Memory
- Triggers/schedules
- Provider settings/secrets metadata
- Agent sessions/turns

### 2.3 新导航原则：按操作者目标，而不是按内部模块

建议默认一级导航只保留 7 个入口：

1. **Home**：资金曲线、PnL、风险、待审批、活跃任务、异常。
2. **Agent Workspace**：Chat、任务时间线、工具调用、文件/diff/验证、session/turn。
3. **Portfolio**：资金、持仓、订单、成交、风险暴露、账户连接。
4. **Strategies**：策略列表、配置、表现、版本、回测/测试、关联触发条件。
5. **Workflows**：面向用户的自动化任务；Trigger route / schedule 只作为详情里的实现层。
6. **Action Inbox**：待审批、失败任务、风险告警、provider error、proposal、notification。
7. **Settings**：Provider、钱包/交易所、权限、安全、通知、UI preference。

高级/开发入口默认折叠：

- Skills、Scripts、Subagents、Memory、Evolution、Messages、Raw API、Trigger Debugger。
- 只有当相关 capability 已启用、当前任务用到、或用户打开 Developer/Advanced mode 时显示。
- 所有隐藏入口仍可通过 command palette 搜索进入，避免完全不可达。

### 2.4 新首页原则：先回答“我的 agent 和钱安全吗”

首页首屏建议改成：

- **Equity / NAV Curve**：默认展示资金曲线、日/周/月收益、回撤、波动。
- **PnL Attribution**：按策略、市场、账户、时间段拆解收益。
- **Risk & Exposure**：净敞口、杠杆、未实现亏损、drawdown cap、risk gate 状态。
- **Active Agent Work**：正在运行的任务、等待审批、失败任务、最近验证。
- **Open Positions / Orders**：当前资金占用、订单状态、异常订单。
- **Recent Decisions**：最近 agent 为什么买/卖/跳过，链接到 timeline。

K 线处理原则：

- 首页默认不以 K 线为核心。
- K 线只在用户选中某个持仓、订单、策略、市场时作为上下文 drill-down。
- K 线旁必须展示该市场相关的 agent 决策、持仓成本、止盈止损、风险规则，否则只是噪音。

### 2.5 页面级重构矩阵


| 当前页面                  | 默认处理             | 面向用户的改法                                                                   |
| --------------------- | ---------------- | ------------------------------------------------------------------------- |
| Dashboard / Home      | 保留但重做            | 资金曲线、PnL、风险、任务、审批、异常优先；K 线降级为上下文图。                                        |
| Chat                  | 升级               | 改成 Agent Workspace：任务时间线、TODO、工具、diff、验证、审批一屏可见。                          |
| Portfolio             | 保留并增强            | 变成资金与风险中心：NAV、drawdown、exposure、仓位异常、账户健康、策略归因。                           |
| Orders                | 保留但并入交易工作流       | 按“待处理订单/风险订单/最近成交”组织；cancel 前展示影响、关联策略和回滚路径。                              |
| Strategies            | 保留为核心            | 策略详情中内嵌配置、表现、版本、触发条件、回测/测试、最近决策。                                          |
| Strategy History      | 合并/弱化入口          | 并入 Strategies 的 History/Review tab；保留全局搜索入口。                              |
| Triggers / Automation | 改为 Workflows 子能力 | 不再裸露 route/match/payload；在 Strategy/Workflow 里配置触发条件和 schedule。           |
| Agents                | 改名/拆分            | 用户侧叫 Tasks/Runs；run turn/explain/trace 放 Developer Tools。                 |
| Subagents             | 默认隐藏             | 在 Agent Workspace 作为 Team/Role cards；高级页才编辑 system prompt。                |
| Skills                | 默认隐藏             | 改成 Skill Playbook Browser，按当前任务调用时浮出；不作为 `/skills/call` 控制台。              |
| Scripts               | 默认隐藏/上下文化        | 作为 Task Artifact / Tool Library；展示用途、风险、输入表单、审批、最近运行。                     |
| Memory                | 上下文化             | 在 TaskInspector 展示引用事实；编辑必须走 proposal/diff。                               |
| Evolution             | 高风险隐藏            | 并入 Approvals/Proposals；apply/rollback 必须展示 diff、风险、验证和回滚。                 |
| Messages              | 合并               | 变成 Notifications / Outbox；从任务、策略、告警进入。                                    |
| Settings              | 保留但重组            | Runtime Config、Integrations、Risk & Approval、Notifications、UI Preferences。 |
| Security              | 合并进 Settings     | 作为 Security / Secrets / Permissions 分组，不再和 Integrations 重复。               |


### 2.5.1 当前左侧菜单逐项改法

当前 `Nerya/dashboard/lib/nav.ts` 的 16 个入口应按下面方式收敛。目标不是少菜单本身，而是让每个入口都有明确用户价值、明确下一步、明确风险边界。


| 当前菜单             | 建议处理                                             | 用户友好改法                                                                       |
| ---------------- | ------------------------------------------------ | ---------------------------------------------------------------------------- |
| `Command Center` | 改名 `Home`，保留一级                                   | 首屏只回答资产、风险、待处理事项、系统可用性；移除默认 K 线中心位。                                          |
| `Chat`           | 改名 `Agent Workspace`，保留一级                        | 从聊天框升级成任务工作台：plan/todo/tool/diff/test/approval/final + TaskInspector。        |
| `Strategies`     | 保留一级                                             | 变成策略运营中心：表现、风险、配置、触发条件、版本、测试、审批一体化。                                          |
| `Automation`     | 改名 `Workflows`，保留但重做                             | 默认展示业务自动化；trigger route/schedule/payload 只在 advanced implementation 展开。      |
| `Agent Runs`     | 改名 `Tasks / Runs`，移入 Agent Workspace 或保留二级       | 普通用户看任务状态和恢复动作；run turn / trace / raw state 进入 Developer Tools。              |
| `Subagents`      | 默认隐藏到 Advanced；在 Agent Workspace 以 Team Roles 浮出 | 展示角色能力、最近参与任务、测试结果；prompt 文件编辑只给高级用户。                                        |
| `Portfolio`      | 保留一级                                             | 改成资产与风险中心：NAV、drawdown、exposure、仓位异常、账户 readiness、PnL 解释。                    |
| `Orders`         | 保留二级或并入 Portfolio                                | 默认只展示需处理订单、异常订单、最近成交；取消订单前给影响预览和恢复路径。                                        |
| `History`        | 并入 Strategies 详情，保留全局搜索入口                        | 从事件流水改成复盘/归因/异常解释：为什么交易、收益来自哪、哪里偏离策略。                                        |
| `Skills`         | 默认隐藏到 Advanced                                   | 改成 Capabilities / Playbooks：浏览 `SKILL.md`、使用场景、示例、被哪些任务调用。                   |
| `Scripts`        | 默认隐藏；从任务/策略产物进入                                  | 改成 Tools / Artifacts：用途、输入表单、风险、审批、最近运行；raw source/debug 折叠。                 |
| `Memory`         | 默认隐藏；作为 TaskInspector evidence                   | 展示“本任务引用了哪些事实”；编辑必须 proposal/diff，不做普通记事本。                                   |
| `Evolution`      | 改名 `Proposals`，移入 Action Inbox / Advanced        | 用户处理变更提案：diff、证据、风险、测试、approve/reject/rollback。                              |
| `Messages`       | 改名 `Notifications / Outbox`，合并到 Action Inbox     | 按 severity、来源、是否需要处理展示；raw message record 只 debug。                           |
| `Security`       | 合并进 Settings > Security                          | Secrets、permissions、provider readiness 归入设置分组，不再和 Integrations 重复。           |
| `Settings`       | 保留一级但重组                                          | Runtime Config、Integrations、Risk & Approval、Notifications、UI Preferences 分层。 |


建议最终一级导航：

1. `Home`
2. `Agent Workspace`
3. `Portfolio`
4. `Strategies`
5. `Workflows`
6. `Action Inbox`
7. `Settings`

Advanced / Developer Tools 折叠入口：

- `Tasks / Runs Debugger`
- `Trigger Debugger`
- `Capabilities / Skills`
- `Scripts / Tool Library`
- `Team Roles / Subagents`
- `Memory Evidence`
- `Raw Events / Trace / JSON`

### 2.5.2 菜单可见性规则

菜单不应静态写死，而应根据 runtime capability、用户角色和当前任务上下文动态生成：

- **未完成 setup**：只显示 `Home`、`Setup`、`Agent Workspace`，其他入口以 checklist 的下一步出现。
- **没有交易账户**：隐藏或弱化 `Portfolio` / `Orders`，显示 `Connect account` CTA。
- **没有策略**：`Strategies` 显示创建向导，不显示空表格。
- **没有 messaging provider**：隐藏 `Notifications / Outbox` 的发送功能，只显示 setup 提示。
- **没有启用 evolution**：隐藏 `Proposals`，避免普通用户误解自进化能力。
- **Operator 模式**：只显示业务入口和 Action Inbox。
- **Developer 模式**：额外显示 Trigger Debugger、Skill console、raw trace、payload JSON。
- **当前任务用到某资源**：即使该资源默认隐藏，也在 TaskInspector 的 contextual resource 中浮出。

### 2.5.3 每个菜单入口的首屏必须有三件事

每个可见菜单打开后，第一屏必须统一包含：

1. **Status**：这个页面相关能力是否可用，有无风险/错误/待处理。
2. **Primary action**：用户下一步最可能要做的一件事。
3. **Context links**：关联的策略、任务、订单、审批、配置来源。

不满足这三点的入口，不应作为一级菜单。

### 2.5.4 当前菜单页实施卡片

以下是按当前菜单逐页落地的产品卡片，前端实现时优先按这些卡片改，不要只改视觉样式。

#### `Command Center` / Home

- **用户问题**：我现在安全吗？有没有赚钱？有什么事要处理？
- **首屏模块**：system health、NAV curve、PnL attribution、What needs attention、recent decisions。
- **主按钮**：`Review risk`。
- **隐藏/降级**：K 线降级到选中市场上下文；Skills/Routes 数量不要作为核心 KPI。
- **验收**：用户 5 秒内能知道资产、风险、待审批、失败任务、provider 错误。

#### `Chat` / Agent Workspace

- **用户问题**：我让 agent 做的事现在进展到哪了？改了什么？还缺我批准什么？
- **首屏模块**：task list、timeline、TaskInspector、input with mode/context/permission。
- **主按钮**：`Start task`；运行中变成 `Stop` / `Resume`。
- **隐藏/降级**：localStorage thread 只做缓存；raw stream events 进 Debug。
- **验收**：每个任务能看到 plan、todo、tool、diff、test、approval、final report。

#### `Strategies`

- **用户问题**：哪些策略在运行？表现怎样？怎么安全修改？
- **首屏模块**：strategy cards grouped by active/paper/needs approval/failed/disabled。
- **主按钮**：`Create strategy` 或 `Run validation`。
- **隐藏/降级**：config JSON、limits JSON、prompts JSON 进 Advanced；默认 schema form。
- **验收**：打开策略详情能看到 Overview、Config、Prompts、When it runs、History、Tests、Approvals。

#### `Automation` / Workflows

- **用户问题**：我有哪些自动化任务？什么时候触发？失败了怎么办？
- **首屏模块**：workflow list、next run、last result、failure alert、linked strategy/task。
- **主按钮**：`Create workflow`。
- **隐藏/降级**：route/match/payload/schedule raw JSON 进 Advanced implementation。
- **验收**：用户用自然语言/向导创建 schedule/webhook/risk alert，不手写 payload JSON。

#### `Agent Runs` / Tasks/Runs

- **用户问题**：哪些任务还在跑？哪些失败了？能不能恢复？
- **首屏模块**：active tasks、failed/recoverable tasks、pending approvals、recent completed。
- **主按钮**：`Resume selected task` 或 `Open task timeline`。
- **隐藏/降级**：Run one turn、trace/explain、turn state raw JSON 进 Developer Tools。
- **验收**：普通用户不需要知道 session_id/turn_id/payload，也能恢复失败任务。

#### `Subagents` / Team Roles

- **用户问题**：agent 有哪些专门角色？它们最近做了什么？是否靠谱？
- **首屏模块**：role cards、capabilities、recent tasks、success/error rate、test result。
- **主按钮**：`Test role` 或 `Create role from template`。
- **隐藏/降级**：system prompt textarea 进 Advanced editor。
- **验收**：用户按“市场分析师/风险审查员/执行检查员”理解，而不是按 prompt 文件理解。

#### `Portfolio`

- **用户问题**：钱在哪里？风险在哪里？收益从哪里来？
- **首屏模块**：NAV、cash、realized/unrealized PnL、drawdown、exposure、risky positions。
- **主按钮**：`Explain PnL`。
- **隐藏/降级**：advanced payload JSON 进 Debug。
- **验收**：资金曲线异常点能跳到策略、订单、agent decision。

#### `Orders`

- **用户问题**：有哪些订单需要处理？取消会有什么影响？
- **首屏模块**：open/cancelable orders、stuck orders、recent fills、strategy/account filters。
- **主按钮**：`Review open orders`。
- **隐藏/降级**：selected order raw JSON 进 Debug。
- **验收**：取消订单前展示影响预览：策略、仓位、资金占用、失败恢复。

#### `History` / Strategy Review

- **用户问题**：策略为什么交易？表现为什么变差？哪里偏离了？
- **首屏模块**：session picker、PnL attribution、decision timeline、divergence alerts、replay result。
- **主按钮**：`Review latest session`。
- **隐藏/降级**：event JSON list 进 Debug；默认展示解释和归因。
- **验收**：用户能从一次亏损直接看到触发、决策、订单、PnL、改进建议。

#### `Skills` / Capabilities

- **用户问题**：Nerya 会什么？什么时候会用？是否可用？
- **首屏模块**：capability cards、availability、required config、examples、recent usage。
- **主按钮**：`Open playbook`。
- **隐藏/降级**：`/skills/call` payload console 进 Developer Tools。
- **验收**：页面浏览 `SKILL.md` playbook，不让普通用户手写 action payload。

#### `Scripts` / Tools & Artifacts

- **用户问题**：有哪些脚本工具？谁生成的？能不能安全运行？
- **首屏模块**：approved tools、pending proposals、risk findings、recent runs、owner task。
- **主按钮**：`Create tool from task` 或 `Run approved tool`。
- **隐藏/降级**：source editor、manifest、args JSON 进 Advanced；默认 schema form。
- **验收**：脚本运行前能看到输入表单、权限、风险、审批状态和 dry-run 结果。

#### `Memory`

- **用户问题**：agent 这次依据了哪些事实？事实错了怎么改？
- **首屏模块**：referenced facts、source task、confidence/staleness、edit proposal。
- **主按钮**：`Search memory`。
- **隐藏/降级**：free-form remember textarea 不做默认入口。
- **验收**：Memory 作为 evidence surface 出现，修改事实必须走 proposal/diff。

#### `Evolution` / Proposals

- **用户问题**：有哪些改进提案要我批准？风险是什么？能回滚吗？
- **首屏模块**：pending proposals、diff、evidence、test result、risk、rollback plan。
- **主按钮**：`Review proposal`。
- **隐藏/降级**：ranking/evidence raw JSON 进 Debug。
- **验收**：apply/rollback 前必须看见 diff、验证、影响范围和恢复方案。

#### `Messages` / Notifications & Outbox

- **用户问题**：哪些通知需要我处理？哪些消息发送失败？
- **首屏模块**：action inbox、severity、source task/strategy、delivery state、retry。
- **主按钮**：`Open next action`。
- **隐藏/降级**：manual send message 和 raw record 进 Advanced。
- **验收**：按“需要处理”排序，而不是按 message record 时间堆列表。

#### `Security`

- **用户问题**：我的密钥、权限、provider、安全策略是否正常？
- **首屏模块**：security checklist、vault refs、missing secrets、permission risks、provider readiness。
- **主按钮**：`Fix security issue`。
- **隐藏/降级**：合并进 Settings，不再独立一级菜单。
- **验收**：删除 secret / disable wallet / 改 routing 前展示影响范围。

#### `Settings`

- **用户问题**：怎么配置 runtime、集成、风险、通知、UI？
- **首屏模块**：Runtime Config、Integrations、Risk & Approval、Notifications、UI Preferences。
- **主按钮**：`Run readiness check`。
- **隐藏/降级**：chart preferences 和 UI 小选项折叠到 UI Preferences。
- **验收**：runtime/security/live/risk 配置不再像普通浏览器偏好；危险配置走 preview/approval。

### 2.5.5 改后菜单结构草案

建议把 sidebar 改成两层：默认业务入口 + 折叠高级入口。

默认业务入口：

```text
Home
Agent Workspace
Portfolio
Strategies
Workflows
Action Inbox
Settings
```

二级/上下文入口：

```text
Portfolio
  ├─ Positions
  ├─ Orders
  ├─ Fills
  └─ Risk / Exposure

Strategies
  ├─ Overview
  ├─ Config
  ├─ Prompts
  ├─ When it runs
  ├─ History / Review
  ├─ Tests
  └─ Approvals

Settings
  ├─ Runtime Config
  ├─ Integrations
  ├─ Risk & Approval
  ├─ Notifications
  ├─ Security / Secrets
  └─ UI Preferences

Advanced / Developer Tools
  ├─ Tasks / Runs Debugger
  ├─ Trigger Debugger
  ├─ Capabilities / Skills
  ├─ Scripts / Tool Library
  ├─ Team Roles / Subagents
  ├─ Memory Evidence
  └─ Raw Events / Trace / JSON
```

关键点：

- `Orders` 不消失，而是作为 Portfolio 下的交易操作 tab；仍可通过 command palette 直达。
- `History` 不消失，而是作为 Strategies 下的 Review/History tab；仍可全局搜索。
- `Skills/Scripts/Subagents/Memory` 不消失，而是作为任务/策略的 contextual resource 浮出。
- `Evolution/Messages` 变成 Action Inbox 的两类 item：proposal 和 notification。
- `Security` 不消失，而是 Settings 下的高风险配置分组。

### 2.5.6 旧菜单到新菜单迁移映射


| 旧路由                 | 新位置                                                 | 迁移方式                                               |
| ------------------- | --------------------------------------------------- | -------------------------------------------------- |
| `/dashboard`        | `/home` 或保留 redirect                                | 保留兼容 redirect；页面内容重做为 Home。                        |
| `/chat`             | `/workspace` 或保留 `/chat` 别名                         | UI 改名 Agent Workspace，路由可先不改。                      |
| `/strategies`       | `/strategies`                                       | 保留一级，详情页吸收 History、Triggers、Tests。                 |
| `/triggers`         | `/workflows` + Advanced Trigger Debugger            | 用户默认进 Workflows；raw route editor 迁到 Advanced。      |
| `/agents`           | `/workspace/runs` + Advanced Turn Debugger          | 用户看 Tasks/Runs；run-turn/explain/trace 迁到 Advanced。 |
| `/subagents`        | `/advanced/team-roles` + workspace contextual panel | 默认隐藏；从任务详情查看参与角色。                                  |
| `/portfolio`        | `/portfolio`                                        | 保留一级，吸收 Orders 和 Risk/Exposure。                    |
| `/orders`           | `/portfolio?tab=orders`                             | 保留 redirect；sidebar 不再一级展示。                        |
| `/strategy-history` | `/strategies/:id?tab=history` + global search       | 保留 redirect/search；sidebar 不再一级展示。                 |
| `/skills`           | `/advanced/capabilities`                            | 默认隐藏；从 TaskInspector/Command palette 进入。           |
| `/scripts`          | `/advanced/tools`                                   | 默认隐藏；从 task artifacts/strategy tools 进入。           |
| `/memory`           | `/advanced/memory` + TaskInspector evidence         | 默认隐藏；任务中按引用事实浮出。                                   |
| `/evolution`        | `/inbox?type=proposal`                              | 作为 Action Inbox 的 proposal 类型。                     |
| `/messages`         | `/inbox?type=notification`                          | 作为 Action Inbox 的 notification/outbox 类型。          |
| `/security`         | `/settings?section=security`                        | 合并进 Settings；保留 redirect。                          |
| `/settings`         | `/settings`                                         | 保留一级，但内部重组。                                        |


### 2.5.7 菜单改造的分阶段落地

不要一次性大爆炸改路由，建议三步走：

1. **P0：先改显示层**
  - Sidebar 默认隐藏 Advanced 菜单。
  - 把 `Command Center` 文案改成 `Home`。
  - 把 `Chat` 文案改成 `Agent Workspace`。
  - 增加 `Action Inbox` 菜单占位。
  - Orders/History/Security 先保留路由，但从一级菜单移除或折叠。
2. **P1：再改页面归属**
  - Portfolio 页面增加 Orders tab。
  - Strategies detail 增加 History/Review tab。
  - Settings 合并 Security/Integrations。
  - Workflows 替代 Triggers 默认视图。
  - Agent Runs 拆出 user-facing Tasks 和 developer Debugger。
3. **P2：最后改数据模型**
  - NAV 从静态数组改成 capability-aware nav builder。
  - Action Inbox 接 approvals、failed tasks、risk alerts、provider errors。
  - Contextual resources 统一从 TaskInspector/ResourceRef 浮出。
  - 旧路由保留 redirect 和 command palette alias。

### 2.6 每个页面都必须回答的 5 个问题

重构后每个页面都必须显式回答：

1. **我为什么要看这个页面？** 入口标题和首屏必须说明任务目标。
2. **现在状态正常吗？** 有状态灯、健康检查、风险级别、最近错误。
3. **我下一步能做什么？** 主按钮不超过 1-2 个，危险操作必须解释影响。
4. **这个动作会改什么？** 保存、应用、取消、删除、运行前必须有 diff / preview / validation。
5. **出事怎么恢复？** 有 rollback、retry、open logs、view task timeline、contact provider 等 recovery action。

### 2.7 全局交互标准

所有页面共用以下交互标准：

- **Primary action**：每页只保留一个主动作；其他动作放 secondary / kebab / command palette。
- **State first**：首屏必须展示健康、风险、待处理事项，不先展示底层列表。
- **Explain empty**：空状态必须解释原因和下一步，不允许只有 `No data`。
- **Recover errors**：错误必须带 retry、open logs、go settings、view task、copy diagnostic。
- **Preview dangerous actions**：删除、禁用、应用、取消订单、改 live/risk/provider 前必须展示影响对象。
- **Debug last**：raw JSON、payload、manifest、trace 默认折叠到 Debug，不作为主视图。
- **Context links**：每个资源都能跳到来源 task、关联 strategy、最近 decision、相关 approval。
- **Capability-aware**：没有配置 provider/wallet/messaging/evolution 时，相关入口隐藏或显示 setup CTA。

### 2.8 Setup Wizard 目标形态

新用户第一次进入时，不应先看到 16 个菜单，而应进入配置向导：

1. 选择模式：paper / live readiness / developer only。
2. 配置 LLM provider：key 是否存在、模型是否可用、成本/隐私策略。
3. 配置账户/钱包/交易所：paper 账户、vault ref、provider readiness、链/RPC。
4. 配置风险：max drawdown、order confirmation、live trading gate、approval policy。
5. 配置策略或导入示例：创建第一个策略、绑定触发条件、跑一次 dry run。
6. 完成后进入 Home，展示“已可用/还缺什么/下一步”。

### 2.9 具体用户旅程蓝图

后续实现必须围绕这些真实用户旅程，而不是围绕路由文件名：

#### 旅程 A：第一次打开 Nerya

入口：`/` 或 `/dashboard`。

用户应该看到：

1. Runtime 是否在线：API、stream、LLM、wallet/exchange、risk gate。
2. 当前模式：paper / live readiness / live，是否需要审批。
3. 还缺什么：LLM key、账户、策略、风险配置、消息通道。
4. 下一步主按钮：`Start setup` / `Create paper strategy` / `Open Agent Workspace`。
5. 不显示：Skills、Scripts、Subagents、Trigger routes、raw JSON。

#### 旅程 B：我想知道现在赚没赚钱、有没有风险

入口：Home 或 Portfolio。

用户应该看到：

1. NAV / Equity curve、realized/unrealized PnL、drawdown、资金利用率。
2. 按策略/市场/账户拆分的 PnL attribution。
3. 风险解释：哪些仓位超限、哪些订单卡住、哪些策略最近亏损。
4. 主动作：`Review risk`、`Open positions`、`Ask agent to explain`。
5. 每个异常都能跳到对应 strategy、order、task timeline。

#### 旅程 C：我想创建或修改一个策略

入口：Strategies 或 Chat。

用户应该看到：

1. 策略卡片：目标、市场、账户、状态、风险、近期表现。
2. 配置分区：universe、position sizing、risk limits、prompts、triggers、tests。
3. 修改前 preview：diff、影响范围、需要审批的原因。
4. 保存后自动生成 task：validation、dry run、paper run、approval。
5. Trigger 不再是单独菜单，而是策略详情里的 `When this strategy runs`。

#### 旅程 D：我想让 agent 自动做一件事

入口：Workflows。

用户应该看到：

1. 业务语言的 workflow：`Every 5 min review BTC risk`、`On drawdown alert send Telegram`。
2. Trigger route / schedule 是展开后的 implementation，不是默认视图。
3. 创建向导：事件来源、条件、目标动作、频率、限流、审批、dry run。
4. 预览：下一次什么时候触发、会调用哪个策略/agent、失败怎么告警。
5. 不要求用户手写 match JSON / payload JSON。

#### 旅程 E：我想处理待审批/失败任务

入口：Action Inbox / Notifications。

用户应该看到：

1. Pending approvals：变更摘要、风险、diff、测试结果、approve/reject。
2. Failed tasks：失败步骤、错误原因、retry/open logs/open task。
3. Risk alerts：关联资金/订单/策略，严重级别和建议动作。
4. Provider errors：哪个 key/模型/钱包/交易所失效，修复入口。
5. 所有条目都必须能跳回 task timeline。

#### 旅程 F：我是高级用户，要调试 runtime

入口：Advanced / Developer Tools / Command Palette。

用户才应该看到：

1. Turn Debugger、raw events、trace/explain、payload JSON。
2. Skill call console、script manifest、subagent prompt editor。
3. Trigger Debugger、route/match/payload raw editor。
4. Raw JSON、provider routing document、model catalog details。
5. 这些页面必须默认折叠，并明确标注“高级/危险/调试”。

### 2.10 页面命名和文案原则

当前很多页面名是内部名，重构时建议改名：


| 当前名                   | 建议名                      | 原因                                  |
| --------------------- | ------------------------ | ----------------------------------- |
| Command Center        | Home                     | 更直观，不暗示功能很多但不知从哪开始。                 |
| Chat                  | Agent Workspace          | 不只是聊天，而是任务、工具、diff、审批工作台。           |
| Automation / Triggers | Workflows                | 用户关心自动化任务，不关心 SDK trigger route。    |
| Agents                | Tasks / Runs             | 用户关心任务运行，不关心 agent 内部 session/turn。 |
| Subagents             | Team Roles               | 用户理解“角色/团队成员”比 prompt file 更容易。     |
| Skills                | Capabilities / Playbooks | 重点是能力说明和使用时机，不是 action list。        |
| Scripts               | Tools / Artifacts        | 脚本是任务产物和可审计工具，不是主业务入口。              |
| Evolution             | Proposals                | 用户处理的是变更提案，不是抽象自进化。                 |
| Messages              | Notifications / Outbox   | 用户关心通知和发送状态，不是 message record。      |
| Security              | Settings > Security      | 避免和 Integrations 重复。                |


### 2.11 核心页面首屏蓝图

#### Home 首屏

布局：

1. 顶部健康条：API、stream、LLM、wallet/exchange、risk gate、live/paper mode。
2. 左上主图：NAV / Equity curve，默认不是 K 线。
3. 右上 `What needs attention`：pending approvals、failed tasks、risk alerts、provider errors。
4. 中部：PnL attribution by strategy / market / account。
5. 下部：Open positions / orders 异常列表和 recent agent decisions。

主动作：`Review risk`、`Open Action Inbox`、`Ask agent to explain PnL`。

#### Agent Workspace 首屏

布局：

1. 左侧：sessions/tasks，而不是 localStorage thread 列表。
2. 中间：用户消息 + plan/todo/tool/diff/test/final timeline。
3. 右侧：TaskInspector，包括目标、状态、artifacts、approvals、errors、linked resources。
4. 底部：输入框带 mode、context attach、permission mode、stop/resume。

主动作：`Start task`、`Stop/Resume`、`Approve pending change`。

#### Portfolio 首屏

布局：

1. 总资产、现金、已实现/未实现 PnL、drawdown、exposure。
2. 资金曲线异常点标注：点击跳到对应 strategy/order/decision。
3. 持仓按风险排序，不按原始账户顺序。
4. 账户 readiness：paper/live、provider、最近同步、异常。

主动作：`Explain PnL`、`Review exposure`、`Open risky positions`。

#### Strategies 首屏

布局：

1. 策略卡片按状态分组：active、paper、needs approval、failed、disabled。
2. 每张卡显示收益、回撤、最近交易、触发条件、风险状态。
3. 策略详情 tabs：Overview、Config、Prompts、When it runs、History、Tests、Approvals。
4. Config 用 schema form，Prompts 用编辑器，保存前 diff。

主动作：`Create strategy`、`Run validation`、`Ask agent to improve`。

#### Workflows 首屏

布局：

1. 业务级自动化列表：目的、触发源、目标动作、下次运行、最近结果。
2. 创建向导：schedule/webhook/event/risk alert 四类入口。
3. route/match/payload 只作为 advanced implementation 展开。
4. dry run 结果用 timeline card 展示，不用 raw JSON。

主动作：`Create workflow`、`Dry run`、`Pause/resume`。

#### Settings / Setup 首屏

布局：

1. Readiness checklist：LLM、account、wallet/exchange、risk policy、notifications。
2. 每个缺失项都有 `Fix now`，跳到对应配置表单。
3. UI Preferences 单独折叠，不和 runtime/security 配置混在一起。
4. 高风险配置保存前显示影响范围。

主动作：`Run readiness check`、`Fix missing provider`、`Test connection`。

### 2.12 组件级重构规范

- `PageHeader` 必须支持 `status`、`primaryAction`、`secondaryActions`、`helpLink`，不能只放 title/description。
- `Card` 必须能表达 `status`、`severity`、`emptyAction`、`loadingSkeleton`，减少每页重复写空状态。
- `Kpi` 必须支持 `explain`、`source`、`threshold`、`clickTarget`，例如点击 drawdown 跳到风险解释。
- `Empty` 必须改成 `EmptyState`：包含原因、下一步按钮、相关文档/设置链接。
- `ErrorBanner` 必须改成 `ErrorState`：包含 retry、open logs、copy diagnostic、go settings。
- `Json` 改为 `DebugJson`，只能在 Advanced/Debug 展开；默认用户视图不能依赖它。
- `CandleChart` 应改为 `MarketContextChart`：只有选中市场/订单/策略时出现，并叠加决策、成本、止盈止损、订单点。
- `ChatInput` 的 cancel 必须是真后端 cancel/interrupt，不是“client-side only”。
- `TurnBlocks` / `LiveActivity` 要从 tool/event 技术流升级为 task timeline：plan、todo、tool、approval、diff、test、final。

### 2.13 基于最新代码的目标前端页面

最新代码已经有足够后端基础，前端不应再把 16 个模块摊成菜单。目标页面按“用户要完成什么”组织：

| 新页面 | 默认展示 | 后端事实源 | 不再默认展示 |
| --- | --- | --- | --- |
| `Home` | NAV/equity、PnL、drawdown、risk、待处理、失败任务、provider 状态 | `/operator/overview` 聚合 existing portfolio/approvals/messages/runtime/teams | 单市场 K 线、skills/routes 数字 |
| `Agent Workspace` | session/task 列表、timeline、plan/todo、tool、diff、approval、artifact | `/agent/tasks`、`/agent/session/*`、`/agent/stream/events`、`/teams/*` | 裸 run_turn payload、裸 trace 表单 |
| `Portfolio` | 资金曲线、回撤、暴露、持仓异常、订单影响、PnL 解释 | `/portfolio/health`、existing `/portfolio/*`、`/trading/*`、`/strategy/attribution` | raw account JSON 作为主视图 |
| `Strategies` | 策略状态、配置、触发、风险护栏、回测/复盘、提案 | `/strategies/*` resource API，复用 strategy skill 和 history APIs | `/skills/call` 控制台 |
| `Workflows` | “什么时候自动运行什么”的业务配置、自然语言 schedule、webhook/condition | `/workflows/*` BFF，底层复用 `/triggers/*` | route/match/payload 编辑器 |
| `Action Inbox` | approvals、proposals、failed tasks、risk alerts、provider errors、notifications | `/inbox/items` 聚合 approvals/evolution/messages/open_turns/provider status | Evolution/Messages 分散菜单 |
| `Settings` | Setup readiness、integrations、LLM、wallet/exchange、risk approval、UI prefs | `/setup/readiness`、`/runtime/capability_matrix`、security/provider/wallet/llm APIs | 11 个混杂 tab |
| `Advanced Tools` | raw trace、trigger debugger、skill call、scripts、subagents、memory、route scopes | 现有底层 API 原样保留 | 普通用户默认不可见 |

设计原则：

- 每个页面首屏都必须有 `Status`、`Primary action`、`Needs attention`、`Source links`。
- 每个危险动作都必须先走 `impact preview`，再执行 apply/callback。
- 每个 raw JSON 都必须有用户摘要；JSON 只能在 Advanced/Debug 展开。
- 每个“为什么失败/为什么不可用”都必须指向一个修复入口，而不是只显示 error string。

### 2.14 后端 API 重构目标：增加 Operator BFF，不替换 runtime 内核

不要把现有 routes 全推翻。现有 `agent`、`portfolio`、`triggers`、`skills`、`security`、`llm`、`teams`、`gateway` routes 是 runtime 能力层；前端需要的是一个更薄的 operator-facing 聚合层。

建议新增这些产品 API：

```text
GET  /operator/nav
GET  /operator/overview
GET  /setup/readiness

GET  /inbox/items?type=&severity=&status=
POST /inbox/items/:id/resolve

GET  /agent/tasks
GET  /agent/tasks/:task_id
GET  /agent/tasks/:task_id/timeline
GET  /agent/tasks/:task_id/artifacts
POST /agent/tasks/:task_id/resume
POST /agent/tasks/:task_id/cancel

GET  /portfolio/health
POST /portfolio/explain_pnl
POST /portfolio/orders/:order_id/impact_preview
POST /portfolio/orders/:order_id/cancel

GET  /strategies
GET  /strategies/:strategy_id
POST /strategies/:strategy_id/preview_change
POST /strategies/:strategy_id/apply_change
GET  /strategies/:strategy_id/history
GET  /strategies/:strategy_id/workflows

GET  /workflows
GET  /workflows/:workflow_id
POST /workflows/preview
POST /workflows/apply
POST /workflows/:workflow_id/run_now

POST /settings/impact_preview
POST /settings/apply
POST /integrations/test
```

这些 API 的职责：

- **聚合**：Home 不再由前端并发拼 10 个端点，而是后端聚合成 `overview`。
- **翻译**：Workflows 把 trigger route/schedule 翻译成用户语言。
- **影响预览**：变更前返回 affected strategies/tasks/orders/provider、risk、rollback。
- **schema 化**：需要用户填写的配置返回 JSON Schema/defaults/enum/source。
- **权限表达**：返回 `allowed`、`required_scope`、`disabled_reason`，让前端按钮显隐有依据。
- **来源可追溯**：每个卡片返回 `source_refs`，能跳到 trace/session/history/debug。
- **保留 debug**：底层 `/skills/call`、`/triggers/*`、`/agent/trace` 不删除，只放 Advanced。

### 2.15 产品 API 到现有代码的复用映射

| 新 API | 复用现有代码 | 需要补的逻辑 |
| --- | --- | --- |
| `/operator/nav` | `/runtime/capability_matrix`、`/discovery`、`route_scopes` | 根据 capability、setup、role、scope 生成菜单和隐藏原因 |
| `/operator/overview` | `/workspace`、`/portfolio/*`、`/approvals/pending`、`/messages/list`、`/agent/open_turns`、`/runtime/capability_matrix` | attention items、health score、risk summary、primary action |
| `/setup/readiness` | discovery、wallet、exchanges、LLM providers、provider_auth、risk config | first-run checklist、blocking reason、修复动作 |
| `/inbox/items` | approvals、evolution proposals、messages、open turns、provider status、risk alerts | 统一 item schema、severity、requires_action、source、resolution |
| `/agent/tasks/*` | sessions、open_turns、stream events、trace/explain、teams runs | task_id 归一、timeline reducer、artifact index、resume/cancel 状态 |
| `/portfolio/health` | summary、positions、pnl、equity_curve、recent_trades、risk gate | drawdown、exposure、异常仓位、资金变化归因 |
| `/portfolio/explain_pnl` | strategy_history attribution/review/scenario replay | 把资金曲线点映射到策略、订单、agent decision |
| `/strategies/*` | strategy skill、strategy_history、discovery、trigger schedules/routes | resource API、schema form、diff preview、approval/rollback |
| `/workflows/*` | trigger routes/schedules/dry_run/stats/replay | 用户语言 workflow model、NL schedule、route debug 折叠 |
| `/settings/*` | security、provider_auth、wallet、exchanges、llm、capability_matrix | 影响预览、连接测试、readiness、危险操作审批 |

### 2.16 后端返回结构标准

所有 operator-facing API 返回结构必须稳定，不能让前端猜字段：

```json
{
  "ok": true,
  "status": "ok | warn | error | blocked",
  "severity": "info | warn | danger",
  "summary": "human readable summary",
  "primary_action": {
    "id": "fix_provider",
    "label": "Fix provider",
    "href": "/settings?section=integrations"
  },
  "next_actions": [],
  "source_refs": [],
  "debug_refs": [],
  "data": {}
}
```

规则：

- `status/severity/summary` 给用户读；`data` 给组件渲染；`debug_refs` 给 Advanced。
- mutation 不直接执行：先 `preview`，再 `apply`，高风险再进入 approval。
- read-only 用 GET；有副作用用 POST/PATCH/DELETE；现有 POST-read 路由可保留 legacy，但新 BFF 不继续扩大这个模式。
- 所有 secret 字段只返回 redacted/vault ref，绝不返回 plaintext。
- 所有危险动作返回 `rollback_action` 或明确说明不可回滚。
- 所有列表支持 `limit/cursor/filter`，避免 Home/Inbox 一次拉爆 journal。
- 所有错误返回 `recovery_actions`，不是裸 error string。

---

## 3. 完整优化 TODO（按实现顺序）

### Phase 0 — 信息架构和状态模型先统一

- 重新定义默认一级导航：`Home`、`Agent Workspace`、`Portfolio`、`Strategies`、`Workflows`、`Action Inbox`、`Settings`。
- 将 Skills/Scripts/Subagents/Memory/Evolution/Messages/Trigger Debugger 移入 Advanced 或上下文资源抽屉。
- 增加 capability gating：未启用交易、消息、自进化、触发器、provider 时隐藏对应入口。
- 增加 role/mode gating：Operator、Developer、Admin、Debug 模式看到不同入口。
- 定义 `WorkspaceSession`：后端 session、前端 thread、agent turn 的统一视图。
- 定义 `AgentTask`：一个用户目标对应的执行单元，包含 turns、todos、artifacts、approvals、errors。
- 定义 `TimelineEvent`：message、tool_call、tool_result、approval、diff、test、compact、error、final。
- 定义 `Artifact`：file_read、file_modified、command、script、strategy、subagent、skill、mcp_resource。
- 定义 `ResourceRef`：所有页面统一引用资源，如 `strategy:xxx`、`script:yyy`、`session:zzz`。
- 前端状态从“ChatThread 本地模型”迁移到“后端 session/task transcript 模型”。

验收：

- 用户刷新页面后仍能看到同一任务的真实进度和产物。
- Chat thread 不再只是 localStorage 消息列表。

### Phase 1 — 新事件协议：从 polling event dump 到 timeline stream

- 新增后端 `/agent/timeline/stream` SSE 或 WebSocket。
- 每个事件必须包含 `task_id`、`turn_id`、`session_id`、`seq`、`event_id`、`parent_id`、`timestamp`。
- 支持事件类型：`message.delta`、`tool.started`、`tool.progress`、`tool.result`、`approval.requested`、`approval.resolved`、`diff.created`、`test.started`、`test.result`、`todo.updated`、`plan.updated`、`error.recoverable`、`turn.completed`。
- 前端用 event reducer 合并事件，不再直接把 raw events append 到最后一条消息。
- 支持断线重连：`after_seq` / `Last-Event-ID`。
- 支持事件去重和乱序修复。

验收：

- 工具调用进度能实时更新，不需要刷新。
- 断线重连后不会重复展示或丢事件。

### Phase 2 — Chat timeline 重构

- 重写 `ChatView` 为 `AgentWorkspaceView`。
- 拆分组件：`TimelineMessage`、`ToolCallCard`、`ApprovalCard`、`DiffCard`、`TodoCard`、`PlanCard`、`ErrorRecoveryCard`、`VerificationCard`、`FinalReportCard`。
- tool call 按 `tool_use_id` 聚合 start/progress/result。
- tool result 支持 collapsed/expanded 两种展示。
- 对 read/grep/glob/shell/edit/write 采用不同图标和摘要。
- 错误卡片必须给出 recovery action：retry、re-read、approve、open logs、view diff。
- 最终回答固定展示：改了什么、验证了什么、风险、下一步。

验收：

- 用户能一眼看到任务当前阶段、已完成工具、失败工具和最终结果。
- 长任务不会变成一堆不可读 JSON。

### Phase 3 — 任务进度面板

- 右侧新增 `TaskInspector`。
- 展示当前目标、Plan、Todo、active tool、blocked reason。
- 展示 files read / files modified / commands run / tests run。
- 展示 approvals pending / approved / rejected。
- 展示 skill/MCP/subagent 使用记录。
- 支持点击 artifact 跳转到对应详情或 diff。

验收：

- 用户无需翻聊天记录就知道 Agent 进展。
- pending approval、失败测试、未验证风险能突出显示。

### Phase 4 — 输入区增强

- 输入框支持 mode：Chat、Plan、Edit、Review、Run。
- 支持 mention：`@strategy`、`@script`、`@subagent`、`@skill`、`@file`、`@session`。
- 支持 attach context：选择文件、策略、历史 turn、日志。
- 支持 permission mode：ask、auto-readonly、plan-only、danger-confirm。
- 支持 stop / interrupt / resume。
- 支持“把当前任务保存为计划文档”。

验收：

- 用户能明确告诉 Agent 当前要规划、执行、审查还是编辑。
- 中断后可以继续同一任务，而不是开新 thread 丢上下文。

### Phase 5 — Approvals 工作流产品化

- 在 Chat timeline 内直接展示审批卡片。
- 审批卡片包含：操作类型、风险原因、diff/command、影响资源、过期时间。
- 支持 approve once、approve session、deny、request changes。
- 审批结果回填 timeline。
- 全局 approvals 页面展示所有 pending approvals。
- 高风险资源编辑统一走 proposal + approval。

验收：

- 用户不需要去别的页面找审批。
- 拒绝后 Agent 能看到拒绝原因并改方案。

### Phase 6 — Diff / 文件产物查看器

- 新增 `ArtifactDrawer`。
- 支持 unified diff 展示。
- 支持文件 read preview。
- 支持 side-by-side diff。
- 支持 copy path、open resource、rollback。
- 支持关联 tool call 和 approval。

验收：

- 用户能清楚看到 Agent 改了哪些文件，而不是只看文字总结。

### Phase 7 — Shell / 测试结果展示

- Shell tool result 以命令卡片展示。
- 展示 command、cwd、duration、exit code、stdout/stderr tabs。
- 长输出自动折叠，支持搜索。
- 测试命令识别为 `VerificationCard`。
- 失败测试展示失败摘要和 retry 按钮。

验收：

- 用户能直接判断验证是否真实执行。
- 失败原因不用打开 raw JSON。

### Phase 8 — Resource Studio 基础框架

- 新增通用 `ResourceListPage`。
- 新增通用 `ResourceDetailLayout`。
- 新增通用 `SchemaForm`。
- 新增通用 `JsonEditorWithValidation`。
- 新增通用 `DiffPreview`。
- 新增通用 `ResourceHistory`。
- 新增通用 `ProposalBar`。

验收：

- strategies/scripts/subagents/skills/memory/triggers 页面不再各写一套僵硬表单。

### Phase 9 — Strategies 页面重构

- 把 `config JSON` / `limits JSON` 拆成 schema-aware form。
- prompts 使用多 tab prompt editor，而不是一个 JSON textarea。
- 保存前展示 diff。
- 支持 validate strategy。
- 支持 run test turn 并把结果写入 task timeline。
- 支持版本历史和 rollback。

证据入口：

- `Nerya/dashboard/app/strategies/page.tsx:458`
- `Nerya/dashboard/app/strategies/page.tsx:476`

### Phase 10 — Scripts 页面重构

- Script 列表展示 state、risk、last run、approval status。
- Script editor 支持 code editor、static analyze、run dry-run。
- Proposal diff 和 approval 内嵌。
- Run result 用 shell/test card 展示。
- 支持从 Chat 生成脚本后跳到 script proposal。

证据入口：

- `Nerya/dashboard/lib/clientApi.ts:462`
- `Nerya/dashboard/lib/clientApi.ts:477`

### Phase 11 — Subagents 页面重构

- Subagent editor 支持 prompt、description、tools、permission mode、test task。
- test run 结果作为 timeline task 展示。
- 支持 duplicate/rename/delete 的 diff/approval。
- 支持 subagent capability preview。
- 支持关联最近调用记录。

证据入口：

- `Nerya/dashboard/app/subagents/page.tsx:33`
- `Nerya/dashboard/lib/clientApi.ts:409`

### Phase 12 — Skills 页面重构

- Skills 页面从 action list 改成 `SKILL.md` playbook browser。
- 展示 frontmatter、body 摘要、linked files、scripts、examples。
- 支持 view referenced file。
- 支持 install/disable/update/remove。
- 支持 skill runbook preview，但不要把 actions 当主要 UI。
- 支持 skill 与 Chat task 的 invoked record 关联。

证据入口：

- `Nerya/dashboard/lib/clientApi.ts:294`

### Phase 13 — Agent Sessions / Turns 页面重构

- Agents 页不再要求用户手写 payload JSON 运行 turn。
- Open turns 展示为 task recovery board。
- Turn detail 展示 timeline，而不是 raw JSON。
- 支持 resume / interrupt / branch / export。
- Explain/Trace 支持从任意 timeline event 打开。

证据入口：

- `Nerya/dashboard/app/agents/page.tsx:296`
- `Nerya/dashboard/app/agents/page.tsx:344`
- `Nerya/dashboard/app/agents/page.tsx:378`

### Phase 14 — Memory / Messages / Settings 重构

- Memory 不再作为普通记事本；改成 task/strategy 的 evidence source 和 editable fact proposal。
- Messages 合并为 Notifications / Outbox，并支持从 task、strategy、alert 跳转来源。
- Settings 重组为 Runtime Config、Integrations、Risk & Approval、Notifications、UI Preferences。
- Security 合并进 Settings 的 Secrets / Permissions / Provider readiness 分组。
- Provider settings 支持连接测试、状态诊断、缺失配置解释和修复入口。
- Clear cache / reset settings 显示影响范围，不允许误导用户以为清掉了后端状态。
- 所有危险设置变更走 proposal/approval。

### Phase 15 — Navigation 与全局状态

- 删除硬编码“内部模块铺满侧边栏”的默认体验。
- Sidebar 默认只展示主工作流入口；Advanced 折叠展示底层资源。
- Trigger/Automation 不再默认作为 SDK route 编辑页；改成 Workflows 的详情配置区。
- 全局顶部展示 backend status、active turn count、pending approvals、last error、risk mode。
- 全局 toast 改成 event-aware notification。
- 支持 command palette：跳转资源、运行动作、打开任务、打开高级调试页。

### Phase 15.5 — Home / Command Center 重构

- 首页首屏改为 equity/NAV curve、drawdown、PnL attribution、exposure、risk gate。
- 增加 active tasks、pending approvals、failed/recoverable tasks、last verification。
- 增加 recent agent decisions，并能跳转到对应 timeline event。
- Open positions/orders 与资金曲线联动，展示资金占用和异常状态。
- K 线从默认主模块降级为选中市场/策略/订单后的上下文模块。
- Runtime footer 从 Skills/Routes 数量改成 operator health：API、provider、wallet/exchange、stream、queue、last error。

### Phase 16 — API 层同步重构

- 新增 operator-facing BFF：`/operator/nav`、`/operator/overview`、`/setup/readiness`。
- 新增 Action Inbox：`/inbox/items` 聚合 approvals、proposals、messages、failed/open turns、risk/provider alerts。
- 新增 task API：`/agent/tasks`、`/agent/tasks/:id/timeline`、`/agent/tasks/:id/artifacts`、resume/cancel。
- 新增 portfolio health：`/portfolio/health`、`/portfolio/explain_pnl`、order impact preview。
- 新增 strategy resource API：`/strategies/*`，把 strategy skill action 包成 schema/diff/approval/rollback 流。
- 新增 workflow API：`/workflows/*`，底层复用 triggers，但用户不再看到 route/match/payload。
- 新增 settings impact API：provider/wallet/LLM/risk/security 变更先 preview，再 apply。
- `/skills/call`、`/triggers/*`、`/agent/trace` 保留为 Advanced/Debug，不作为产品主路径。
- 新 API 必须返回 `status`、`severity`、`primary_action`、`next_actions`、`source_refs`、`debug_refs`。
- 端口/API base 契约统一：dashboard proxy、CLI service、README、start-local 不能让用户在 `8787` 和 `18317` 之间猜。

证据入口：

- `Nerya/dashboard/lib/clientApi.ts:317`
- `Nerya/dashboard/lib/clientApi.ts:409`
- `Nerya/dashboard/lib/clientApi.ts:462`
- `Nerya/nerya/api/routes_capability.py:331`
- `Nerya/nerya/api/routes_discovery.py:134`
- `Nerya/nerya/api/routes_approvals.py:225`
- `Nerya/nerya/api/routes_teams.py:112`
- `Nerya/nerya/api/route_scopes.py:47`

### Phase 17 — 前端工程化

- 引入 data fetching 层：React Query / SWR 二选一。
- 引入 schema form 和 code editor。
- 把 clientApi 拆成 domain clients：agentClient、resourceClient、approvalClient、settingsClient。
- 引入前端 event reducer 单元测试。
- 为 Chat timeline 增加 Storybook 或 fixture-driven preview。
- 增加 Playwright e2e：长任务、审批、diff、失败测试、resume。

### Phase 18 — 页面级可用性收敛

- Portfolio 增加 drawdown、exposure、risk gate、异常仓位、策略归因，不再只展示账户/持仓表。
- Orders cancel 改成影响预览：订单、仓位、策略、资金占用、失败恢复，而不是浏览器 confirm。
- Strategy History 并入 Strategies detail 的 Review/Attribution tab，保留全局搜索。
- Agents 拆成用户可见 `Tasks/Runs` 和开发者 `Turn Debugger`。
- Subagents 改成 team/role cards；prompt 编辑放 Advanced。
- Skills 改成 SKILL.md playbook browser；隐藏 `/skills/call` raw payload。
- Scripts 改成 task artifact/tool library；run args 用 schema form，raw JSON 只在 debug 展开。
- Evolution 改成 Proposals/Approvals 高风险流，apply/rollback 必须有 diff、验证、回滚计划。
- Messages 改成 Notifications / Outbox，默认展示 severity、来源、是否需要处理。

### Phase 19 — 全局 Shell 与配置向导

- Sidebar ONLINE 改成真实系统健康摘要：API、stream、LLM、wallet/exchange、risk gate、queue。
- TopHeader Search 改成 command palette，可搜资源、跳页面、执行安全动作。
- TopHeader Notifications 改成 action inbox：approval、failed task、risk alert、provider error。
- Empty/Error 组件增加 `reason`、`nextActions`、`diagnostic`、`retry` 标准接口。
- `Json` 组件改名为 `DebugJson`，默认只在 Debug/Advanced 展开。
- Integrations 改成 setup wizard + readiness checklist，不再只暴露 provider/vault 表。
- LLM Ops 增加一键测试、推荐默认、成本/隐私风险解释、模型不可用原因。
- Secret / wallet / routing 高风险操作全部改成影响预览 + approval/recovery。

### Phase 20 — 用户旅程页面蓝图

- 新增 first-run setup 状态：缺 LLM、缺账户、缺策略、缺风险配置时进入 setup flow。
- Home 实现 `What needs attention` 区块：审批、失败任务、风险、provider error。
- Portfolio 实现 `Explain PnL` 动作：把资金曲线异常点链接到策略/订单/agent decision。
- Strategies detail 增加 `When it runs`、`Why it traded`、`How it is guarded` 三个用户语言区块。
- Workflows 用向导创建 schedule/webhook/condition，不再以 route table 为主。
- Action Inbox 替代静态 notification icon，承载审批、失败、风险、provider 修复。
- Advanced Tools 独立开关，集中 raw JSON、trace、payload、skill call、trigger debugger。
- 页面重命名落地：Agents→Tasks/Runs、Evolution→Proposals、Messages→Notifications/Outbox。

### Phase 21 — 组件级体验重构

- `PageHeader` 增加状态、主动作、帮助链接，所有页面首屏统一表达“状态 + 下一步”。
- `Empty` / `ErrorBanner` 替换为可行动的 EmptyState / ErrorState。
- `Json` 改成 `DebugJson` 并默认折叠到 Advanced。
- `Kpi` 支持 explain/source/clickTarget，资金和风险指标可追溯。
- `CandleChart` 降级为 MarketContextChart，并叠加交易决策和持仓成本。
- `ChatInput` 接入真实 cancel/interrupt/resume，不再只是前端 abort。
- `TurnBlocks` / `LiveActivity` 以 task timeline 为主，tool raw detail 为 debug。
- 所有表格行支持 drill-down 到 task/strategy/order/resource，而不是只打开 raw inspector。

### Phase 22 — 菜单信息架构落地

- 将 `NAV` 从静态数组改成 capability-aware nav builder。
- 一级导航收敛为 Home、Agent Workspace、Portfolio、Strategies、Workflows、Action Inbox、Settings。
- Advanced / Developer Tools 做成折叠分组，不默认展示内部工具页。
- Orders 并入 Portfolio 的 Orders tab，同时保留 command palette 直达。
- History 并入 Strategy detail 的 Review/History tab，同时保留全局搜索。
- Security 合并进 Settings > Security，删除重复入口。
- Messages 合并进 Action Inbox / Notifications，默认按“是否需要处理”排序。
- Skills/Scripts/Subagents/Memory/Evolution 改成 contextual resource，从任务/策略/审批入口浮出。
- 每个菜单入口实现 Status + Primary action + Context links 三件套。

### Phase 23 — 菜单页实施卡片落地

- Home 实现 What needs attention + Explain PnL。
- Agent Workspace 接后端 task/session，不再只靠 localStorage thread。
- Strategies detail 增加 When it runs / Why it traded / How guarded。
- Workflows 替代 Automation route table 作为默认视图。
- Tasks/Runs 替代 Agent Runs，并把 raw turn debugger 移入 Developer Tools。
- Portfolio/Orders/History 串成资金、订单、复盘一条链路。
- Capabilities/Tools/Team Roles/Memory 作为 contextual resources，不默认铺在 sidebar。
- Proposals/Notifications 合并进 Action Inbox，默认展示下一步处理动作。
- Settings 接管 Security，并把 UI preferences 与 runtime risk/security 分开。

### Phase 24 — 菜单迁移兼容层

- 旧路由保留 redirect 或 alias，避免用户书签和内部链接失效。
- `/orders` redirect 到 `/portfolio?tab=orders`。
- `/strategy-history` 支持跳到 `/strategies/:id?tab=history`。
- `/security` redirect 到 `/settings?section=security`。
- `/evolution` redirect 到 `/inbox?type=proposal`。
- `/messages` redirect 到 `/inbox?type=notification`。
- Command palette 支持旧页面名搜索，但打开新位置。
- 在迁移期给旧页面显示 “Moved to …” banner 和原因说明。

### Phase 25 — Operator BFF / 聚合 API 落地

- 新建 `nerya/api/routes_operator.py`，注册 `/operator/nav`、`/operator/overview`、`/setup/readiness`。
- `/operator/nav` 从 `/runtime/capability_matrix`、`/discovery`、route scopes、workspace flags 生成菜单。
- `/operator/overview` 聚合 workspace、portfolio、approvals、messages、open turns、LLM/wallet/exchange readiness、recent trades。
- `/setup/readiness` 输出 first-run checklist：LLM、账户、wallet/exchange、risk/approval、paper dry run、strategy。
- 每个 overview card 返回 `status/severity/summary/primary_action/source_refs/debug_refs`。
- dashboard Home 只消费 `/operator/overview`，不再在页面里并发拼十几个底层 API。
- TopHeader/Sidebar 消费 `/operator/nav` 和 overview 的 health summary。

验收：

- 关闭 wallet 或缺 LLM key 时，Home 和 Sidebar 都能显示 blocked reason 和 fix action。
- 未启用 evolution/messaging/team 时，菜单不展示无意义入口，但 Advanced 可搜索到 debug 页面。

### Phase 26 — Action Inbox API 和页面落地

- 新建 `/inbox/items`，合并 approvals、evolution proposals、messages、failed/open turns、provider errors、risk alerts。
- 每个 item 统一字段：`id`、`type`、`severity`、`status`、`title`、`summary`、`requires_action`、`source_refs`、`actions`。
- approvals 复用 `/approvals/pending`、`/approvals/prompt`、`/approvals/callback`。
- proposals 复用 `/evolution/proposals`，但 item 必须带 diff/evidence/test/rollback 摘要。
- failed tasks 复用 `/agent/open_turns`、`/agent/turn_state`、`/agent/explain`。
- provider errors 复用 LLM/wallet/exchange/provider_auth 状态。
- `/messages` 只作为 notification/outbox 的底层来源，不再做默认一级菜单。
- 新建 `/inbox` 页面，替代 Evolution/Messages/假的 notification icon。

验收：

- 用户打开 Action Inbox 能完成 approve/reject、open failed task、fix provider、view proposal diff。
- 如果一个 item 没有下一步动作，它不应出现在 `requires_action=true` 列表里。

### Phase 27 — Agent Task API 和 Workspace 落地

- 在后端定义 `AgentTask`：`task_id`、goal、session_id、turn_ids、status、blocked_reason、artifacts、approvals、errors。
- 用现有 session store、open turns、stream events、trace/explain、teams runs 组装 `/agent/tasks`。
- `/agent/tasks/:id/timeline` 把 stream events、turn blocks、tool trace、approval、diff/test artifacts 规整成 timeline。
- `/agent/tasks/:id/artifacts` 返回文件、脚本、策略、team report、trace、verification。
- `/agent/tasks/:id/cancel` 复用 `/agent/interrupt`，但返回用户可读状态。
- `/agent/tasks/:id/resume` 明确哪些 stopped_reason 可恢复，不能恢复时给出原因。
- 前端 `ChatView` 改名 `AgentWorkspaceView`，状态来自 task/session，不再把任务事实放 localStorage。
- Team runs 不单独做一级菜单；在 TaskInspector 中展示参与成员、任务、blackboard、final report。

验收：

- 刷新 Agent Workspace 后，正在运行/等待审批/失败的任务仍然可见。
- 一个 team run 能在同一个 task timeline 中看到成员贡献和 synthesis 结果。

### Phase 28 — Portfolio / Strategy / Workflow 产品 API 落地

- `/portfolio/health` 聚合 equity、PnL、drawdown、exposure、positions、orders、risk gate、recent decisions。
- `/portfolio/explain_pnl` 把资金曲线异常点映射到 strategy history、fills、agent decision、trace。
- `/portfolio/orders/:id/impact_preview` 返回取消订单对仓位、资金占用、策略状态、后续 recovery 的影响。
- `/strategies` 和 `/strategies/:id` 替代前端直调 strategy `/skills/call` 的主路径。
- `/strategies/:id/preview_change` 返回 schema validation、diff、affected workflows、risk/approval requirement。
- `/strategies/:id/apply_change` 高风险时只创建 approval/proposal，不直接落地。
- `/workflows` 把 schedules/routes 显示成“自动化任务”，字段包括目标、触发条件、下次运行、最近结果、失败原因。
- `/workflows/preview` 把自然语言 schedule/webhook/condition 翻译成 trigger route/schedule diff。
- `/workflows/apply` 走 approval/rollback，raw trigger route editor 移入 Advanced。

验收：

- 用户创建“每天 9 点检查 BTC 风险”的自动化，不需要看到 route match JSON。
- 用户修改策略 prompt 前能看到 diff、会影响哪些 workflow、是否需要审批和如何回滚。

### Phase 29 — Settings / Integrations / 权限逻辑落地

- Settings 只保留五类：Setup、Integrations、Risk & Approval、Notifications、UI Preferences。
- Security 合并进 Settings，不再单独一级菜单。
- `/settings/impact_preview` 覆盖 secret delete、wallet disable、LLM routing change、live mode/risk change。
- `/integrations/test` 支持 LLM provider、wallet provider、exchange provider、gateway platform 的一键测试。
- 前端所有危险按钮读取 route scope / required permission / live mode / risk gate，显示 disabled reason。
- LLM Ops 改成“可用性、推荐配置、成本、隐私、测试结果”，routing 细节默认折叠。
- Secret 输入写入后立即清空，界面只显示 vault ref 和引用位置。
- Clear cache / reset UI settings 明确只影响浏览器偏好，不影响后端 session/task/runtime。

验收：

- 没权限或缺配置时，按钮不是消失或报错，而是明确显示“为什么不能做”和“去哪修”。
- 用户不会把 browser localStorage 设置误认为 runtime/live/risk 配置。

---

## 4. P0 / P1 / P2 优先级

### P0：先让对话页可用

- 砍掉默认一级菜单噪音，确定 Home / Agent Workspace / Portfolio / Strategies / Workflows / Action Inbox / Settings 信息架构。
- 先落 `/operator/nav`、`/operator/overview`、`/setup/readiness`，否则 Home/Sidebar/Setup 仍会继续前端拼接口。
- 先落 `/inbox/items`，否则 approval/proposal/message/failed task 仍会分散在旧页面。
- 先定义 `/agent/tasks/*`，否则 Agent Workspace 仍会被本地 ChatThread 和 raw stream events 限制。
- 首页从 K 线中心改成资金曲线、风险、任务、审批中心。
- 新用户 first-run 不进入复杂菜单，先进入 setup wizard。
- TopHeader notification 从假红点改成 Action Inbox。
- Timeline event model。
- Chat timeline 重构。
- ToolCallCard / ApprovalCard / DiffCard / ErrorRecoveryCard。
- TaskInspector。
- stop / resume / interrupt。
- final report card。

### P1：让管理页不僵硬

- Trigger route/schedule 从一级菜单改成 Strategy/Workflow 下的可配置触发条件。
- Portfolio/Orders/Strategy History 改成“风险和下一步行动”导向，而不是表格 + JSON inspector。
- Agents 拆成用户任务页和开发调试页。
- Skills/Scripts/Subagents/Memory/Evolution/Messages 改成 Advanced/contextual resources。
- Settings/Security 去重并重组成 runtime/integration/risk/notification/UI 五类。
- Strategy、Workflow、Portfolio health 用产品 API 包底层 runtime routes，不再让页面直连 `/skills/call` 和 trigger route editor。
- 所有危险 mutation 先做 impact preview：订单取消、secret 删除、wallet disable、routing/live/risk 变更。
- 菜单从静态 `NAV` 改成 capability-aware，按 setup/role/mode/context 动态显示。
- Sidebar/TopHeader 从装饰性按钮改成真实 command palette、action inbox、system health。
- Integrations/LLM Ops 改成 setup wizard 和 readiness checklist。
- Resource Studio 基础组件。
- Strategies schema-aware editor。
- Scripts code editor + analyze/run result。
- Subagents prompt editor + test run。
- Agent sessions timeline viewer。
- 全局 approvals 页面。

### P2：形成真正 Agent 产品

- Skill playbook browser。
- MCP/resource browser。
- Artifact graph。
- Command palette。
- Cross-page task/resource linking。
- Full e2e regression suite。

---

## 5. 不要继续做的错误方向

- 不要继续把 Chat 做成本地消息列表。
- 不要继续把 raw stream events 直接挂到最后一条消息。
- 不要继续用 JSON textarea 作为主要编辑体验。
- 不要让管理页继续直接暴露 `/skills/call` 思维。
- 不要让 Home/Sidebar/Inbox 在前端各自拼底层端点；应由 operator-facing BFF 输出稳定产品状态。
- 不要新增一个后端 route 就新增一个一级菜单；只有能回答用户任务的问题才进默认导航。
- 不要把 approval、diff、test result 分散在不同页面。
- 不要只做 UI 美化；必须重构状态模型、事件协议和资源 API。
- 不要继续把 SDK/内部模块当作一级产品菜单。
- 不要让 Trigger/Triggle 作为用户默认入口；它是策略/工作流的触发配置和 SDK 调试面。
- 不要把 K 线当首页核心指标；首页核心是用户资金、风险、任务和待处理事项。
- 不要展示用户不知道干什么的页面；没有明确 action、risk、status、next step 的页面默认隐藏。
- 不要让普通用户手写 payload JSON、run args JSON、match JSON；这些都应变成 schema form 或高级调试折叠项。
- 不要用浏览器 confirm 承载高风险交易/删除/应用操作；必须有影响预览和 recovery action。
- 不要让 Settings 同时承担 UI 偏好、交易风险、provider 集成、安全配置而不分级。
- 不要显示假的 ONLINE、假搜索、假通知；全局 shell 必须连接真实状态和真实 action inbox。
- 不要让用户先理解 vault/tier/routing/provider readiness；应该用 setup wizard 告诉用户缺什么、怎么修。

---

## 6. 最小验收场景

### 6.1 Agent Workspace 验收

用户在 Chat 中发起任务：修改一个策略 prompt 并运行测试。

前端必须展示：

1. 用户目标卡片。
2. Agent plan。
3. Todo 进度。
4. 读取了哪些文件。
5. 修改 diff。
6. 审批卡片。
7. 测试命令和结果。
8. 最终总结。
9. 右侧 TaskInspector 中同步展示 artifacts、approvals、errors。
10. 刷新页面后进度和产物仍存在。

如果只能看到“用户消息 + assistant 文本 + 一串 raw event/JSON”，则重构不算完成。

### 6.2 Home / Portfolio 验收

用户打开 Home 后，不需要点任何菜单就能知道：

1. 当前 NAV / Equity、PnL、drawdown。
2. 有没有 pending approval、failed task、provider error、risk alert。
3. 哪个策略/市场/订单造成主要 PnL 变化。
4. 钱包/交易所/LLM/provider 是否可用。
5. 下一步主动作是什么：review risk、open approval、fix provider、create strategy。

如果首页主要展示 K 线、技能数、routes 数、workspace 名，则重构不算完成。

### 6.3 Setup / Integrations 验收

新用户没有配置 key/account/strategy 时，必须看到 setup checklist：

1. LLM provider 是否 ready。
2. Paper account 是否 ready。
3. Risk / approval policy 是否设置。
4. 是否已有可运行策略。
5. 一键 dry run 是否成功。

如果用户必须自己理解 vault、tier、provider routing、trigger route 才能开始，则重构不算完成。

### 6.4 Advanced / Debug 验收

普通用户默认不应看到 raw payload JSON、skill call console、trigger match JSON、turn trace。只有打开 Advanced mode 后才显示，并且每个高级页面必须：

1. 标注用途和风险。
2. 支持复制诊断信息。
3. 提供返回用户任务页的链接。
4. 不影响普通导航的信息架构。

### 6.5 Operator BFF / Action Inbox 验收

用户打开 Home 和 Action Inbox，不需要知道后端有哪些 routes，也能处理当前系统事项。

前后端必须满足：

1. `GET /operator/overview` 能返回资金、风险、任务、审批、provider、primary action。
2. `GET /operator/nav` 能根据 capability/setup/role/scope 返回默认菜单和隐藏原因。
3. `GET /inbox/items` 能统一展示 approval、proposal、failed task、provider error、risk alert、notification。
4. 每个 inbox item 都有 `requires_action`、`source_refs`、`actions`。
5. approve/reject/fix/open/retry 之后 item 状态会变化，不只是前端本地消失。

如果 Home 仍然在页面组件里并发拼十几个底层 endpoint，或者 Action Inbox 只是 Messages/Evolution 的换皮，则重构不算完成。

### 6.6 Workflow / Strategy / Portfolio API 验收

用户不需要理解 trigger SDK、skill action、raw payload，也能完成日常操作。

前后端必须满足：

1. Workflows 能用自然语言或 schema 表单创建 schedule/webhook/condition。
2. Workflow detail 默认展示目标、触发条件、下次运行、最近结果、失败原因。
3. Strategy 修改必须先 preview diff/impact，再 apply 或进入 approval。
4. Portfolio 能解释某段 PnL/NAV 变化来自哪些策略、订单、agent decision。
5. 取消订单、disable wallet、删除 secret、改 LLM routing 都有影响预览。
6. 所有 raw JSON / route match / skill payload 只在 Advanced 展开。

如果用户还需要手写 match JSON、payload JSON、script args JSON 才能完成核心操作，则重构不算完成。
