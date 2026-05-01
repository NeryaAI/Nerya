# Nerya Agent Harness 完整对比与重构 TODO

更新时间：2026-04-28

本文补充 `agent-intelligence-gap-and-cursor-refactor-plan.md`，专门覆盖 Agent Harness：工具编排、工具执行、权限审批、Shell 风险、hooks、中断、错误恢复、进度展示、compact、session restore、subagent/task、MCP、eval 回归。

核心结论：Nerya 现在有 `ToolRunner`、`StreamingEventBus`、`context_budget.py`、`transcript_compact.py`、operator file tools、ACP tool API 等部件，但这些部件没有形成 Claude Code 那种围绕 provider-native `tool_use/tool_result` 的统一 harness。Nerya 的主路径仍是 skill action runner，所以继续修 action schema、mock decision、operator skill handler，并不会让 Coding Agent 变聪明。

---

## 1. Claude Code Agent Harness 参考点（绝对路径）

### 1.1 主 loop 与 tool result 回填

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\query.ts:365` — 每轮从 messages 取 compact boundary 后的 transcript。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\query.ts:659` — 调模型时传入 `messages/systemPrompt/tools/signal/options`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\query.ts:829` — assistant message 中解析 `tool_use` blocks。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\query.ts:1380` — tool blocks 交给 streaming executor 或 `runTools`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\query.ts:1395` — tool result normalize 成 API messages。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\query.ts:1716` — 下一轮 messages = query messages + assistant messages + tool results。

### 1.2 Tool orchestration：串行/并行/context modifier

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:19` — `runTools` 是统一工具编排入口。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:26` — 先按 concurrency safety 分批。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:35` — read-only/concurrency-safe batch 并发执行。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:65` — 非 read-only batch 串行执行。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:91` — 根据 tool schema 和 `isConcurrencySafe` 分批。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:126` — 串行工具执行时维护 in-progress tool ids。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:152` — 并发工具执行入口。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolOrchestration.ts:179` — tool 完成后移除 in-progress id。

### 1.3 Tool execution：校验、权限、错误、tool_result

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:337` — `runToolUse` 是单个 provider `tool_use` 的执行入口。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:400` — unknown tool 返回 `tool_result` 且 `is_error=true`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:415` — abort signal 中止 tool use。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:599` — `checkPermissionsAndCallTool` 汇合 schema 校验、权限、调用。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:615` — tool input schema 校验。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:669` — input validation error 转成 `tool_result`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:683` — tool 自身 `validateInput` 二次校验。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:742` — Bash command 进入特殊权限/分类路径。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:797` — permission/hook 结果参与最终判断。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:1207` — 执行真实 `tool.call`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:1381` — 记录 `tool_result` telemetry。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:1400` — tool result 可携带 context modifier。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolExecution.ts:1483` — post-tool hooks 在 tool 执行后运行。

### 1.4 Streaming executor 与 hooks

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\StreamingToolExecutor.ts:1` — streaming tool executor 支持模型 streaming 时启动/管理工具。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\StreamingToolExecutor.ts:121` — executor 管理剩余 tool result 与状态。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolHooks.ts:39` — post-tool hook 统一入口。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolHooks.ts:56` — 执行 `executePostToolHooks`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolHooks.ts:105` — hook blocking error 变成 attachment message。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolHooks.ts:117` — hook 可阻止 continuation。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolHooks.ts:132` — hook 可追加上下文。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\tools\toolHooks.ts:145` — hook 可更新 MCP output。

### 1.5 Bash / Shell 风险控制

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\BashTool.tsx:227` — Bash tool input schema 定义 `command/timeout/description/background/sandbox override`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\BashTool.tsx:251` — sandbox override 不能让模型绕过权限检查。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\BashTool.tsx:345` — 用户配置命令支持 permission rule pattern。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\bashPermissions.ts:13` — Bash 权限解析走 AST / security semantics。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\bashPermissions.ts:37` — Bash classifier 参与 allow/ask/deny。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\bashPermissions.ts:55` — 权限请求 message 由 permission utility 创建。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\bashPermissions.ts:67` — sandbox manager 参与 Bash 权限判断。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\bashPermissions.ts:95` — 复杂 compound command 有安全检查上限。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\BashTool\bashPermissions.ts:266` — exact command / prefix rule 建议逻辑。

### 1.6 File / Grep / Glob primitives

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileReadTool\FileReadTool.ts:337` — FileRead 是 first-class buildTool。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileReadTool\FileReadTool.ts:542` — read dedup 依赖 `readFileState`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileReadTool\FileReadTool.ts:575` — 读文件时发现 skill dirs。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileReadTool\FileReadTool.ts:589` — 读文件路径可激活 conditional skills。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileEditTool\FileEditTool.ts:275` — edit 前必须读过文件。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileEditTool\FileEditTool.ts:291` — edit 前检查读后是否被改。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\FileEditTool\FileEditTool.ts:329` — 多匹配时要求更具体上下文。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GlobTool\GlobTool.ts:57` — Glob 是 first-class buildTool。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GlobTool\GlobTool.ts:76` — Glob 声明 concurrency safe。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GlobTool\GlobTool.ts:135` — Glob 走 read permission 检查。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GrepTool\GrepTool.ts:160` — Grep 是 first-class buildTool。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GrepTool\GrepTool.ts:183` — Grep 声明 concurrency safe。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GrepTool\GrepTool.ts:233` — Grep 走 read permission 检查。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\GrepTool\GrepTool.ts:437` — ripgrep timeout/abort 行为被明确处理。

### 1.7 Todo / Plan / Session restore

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\TodoWriteTool\TodoWriteTool.ts:31` — TodoWrite 是 first-class buildTool。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\TodoWriteTool\TodoWriteTool.ts:58` — TodoWrite 无需权限审批。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\TodoWriteTool\TodoWriteTool.ts:65` — TodoWrite 更新 app state。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\TodoWriteTool\TodoWriteTool.ts:72` — 完成较多任务时 nudging verification agent。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\TodoWriteTool\prompt.ts:151` — todo item 需要 `content` 与 `activeForm`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\TodoWriteTool\prompt.ts:156` — todo 状态必须实时更新。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\EnterPlanModeTool\prompt.ts:1` — Plan Mode 是计划/研究状态。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\ExitPlanModeTool\prompt.ts:1` — ExitPlanMode 用于提交计划并请求批准。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\utils\sessionRestore.ts:73` — 从 transcript 的 TodoWrite tool_use 恢复 todos。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\utils\sessionRestore.ts:138` — 恢复后的 todos 注入 app state。

### 1.8 Subagent / Task harness

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\AgentTool.tsx:85` — AgentTool schema 支持 `subagent_type`。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\AgentTool.tsx:96` — spawned teammate 可携带 permission mode。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\AgentTool.tsx:148` — async agent output file 进入 schema/result。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\AgentTool.tsx:196` — AgentTool 是 first-class buildTool。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\AgentTool.tsx:217` — agent list 按 MCP requirements 与 permission rules 过滤。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\AgentTool.tsx:250` — AgentTool call 入口拿到 toolUseContext/canUseTool/assistantMessage/progress callback。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\prompt.ts:91` — prompt 明确不要读取 fork 输出文件污染上下文。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\prompt.ts:103` — fresh agent prompt 要提供充分上下文。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\tools\AgentTool\prompt.ts:112` — 不要把理解工作外包给 subagent。

### 1.9 Compact / MCP / progress summary

- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\microCompact.ts:40` — microcompact 只 compact 指定工具结果。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\compact.ts:1399` — compact 后恢复最近访问文件 attachments。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\compact.ts:1468` — compact 后保留 plan attachment。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\compact.ts:1489` — compact 后保留 invoked skills。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\compact.ts:1542` — compact 后保留 plan mode。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\compact.ts:1563` — compact 后保留 async agent。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\compact\grouping.ts:22` — 按 API round 对 messages 分组。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\mcp\client.ts:2171` — MCP 同时发现 tools、commands、skills、resources。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\mcp\client.ts:2720` — MCP result 转换为 tool result。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\mcp\client.ts:3220` — MCP session expired / connection closed 有恢复路径。
- `C:\Users\Ricky\Documents\Project\ClaudeCode\anthropic-ai-claude-code-2.1.88-expanded\src\services\toolUseSummary\toolUseSummaryGenerator.ts:45` — `generateToolUseSummary` 入口。

## 2. Nerya 当前 Harness 证据（绝对路径）

- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\harness\tool_runner.py:3` — ToolRunner 明确是 skill call chokepoint。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\harness\tool_runner.py:162` — `call` 执行单个 skill action。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\harness\tool_runner.py:216` — runtime.call 在线程 timeout 内执行。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\harness\tool_runner.py:281` — query-only 判断来自 manifest。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\harness\tool_runner.py:300` — coding metadata 来自 manifest flags。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\harness\tool_runner.py:350` — `call_parallel` 只处理 read-only skill actions。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\kernel.py:1169` — build_context 注入 action_catalog。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\kernel.py:1270` — 只追加最近 5 条 observations。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\kernel.py:1317` — 调用 `LLMGateway.call(prompt=ctx)`。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\kernel.py:1321` — 解析 strict JSON decision。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\kernel.py:1541` — 通过 `runner.call` 执行。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\skills\builtin\operator_skill\scripts\handlers.py:229` — `read_file` 藏在 operator skill。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\skills\builtin\operator_skill\scripts\handlers.py:645` — `edit_file` 藏在 operator skill。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\skills\builtin\operator_skill\scripts\handlers.py:1014` — terminal shell 藏在 operator skill。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\skills\builtin\operator_skill\scripts\handlers.py:1286` — todo list 藏在 operator skill。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\streaming.py:99` — 有 event bus publish，但不是 provider streaming tool executor。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\context_budget.py:1` — compact 模块存在。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\agent\transcript_compact.py:118` — transcript validator 存在。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\mcp\dynamic_tools.py:241` — MCP tool 由 SkillManifest/ActionSpec 构造。
- `C:\Users\Ricky\Documents\Project\NeryaProject\Nerya\nerya\mcp\dynamic_tools.py:405` — allow/deny 仍按 skill/action policy。

---

## 3. 差距矩阵


| Harness 维度 | Claude Code                             | Nerya 当前                          | Cursor 应改成                        |
| ---------- | --------------------------------------- | --------------------------------- | --------------------------------- |
| 主循环        | messages/tools/tool_use/tool_result     | prompt + strict JSON action       | provider-native native loop       |
| 工具注册       | first-class `buildTool`                 | skill manifest actions            | 独立 `ToolRegistry`                 |
| 工具执行       | `runToolUse` 统一 schema/权限/hooks/result  | `ToolRunner.call` 执行 skill action | `NativeToolExecutor`              |
| 并发         | tool `isConcurrencySafe` 分批             | manifest `agent_query_only`       | descriptor-driven partition       |
| 错误         | error 回填 `tool_result`                  | error_kind 写 observation/journal  | transcript error result           |
| 权限         | `canUseTool` + mode + hook              | ACP/manifest gate 分散              | `PermissionEngine`                |
| Shell      | AST/classifier/sandbox/prefix rules     | terminal + 简单 destructive pattern | BashTool 风险模型                     |
| 文件编辑       | first-class read state/diff/stale guard | operator skill handler            | native file tools                 |
| hooks      | post hook 可 block/append/update output  | kernel phase hooks                | tool lifecycle hooks              |
| streaming  | streaming tool executor                 | event bus 未驱动 tool execution      | streaming executor                |
| compact    | 主 loop 内 budget/micro/autocompact       | 模块存在但未接主路径                        | transcript compact                |
| skill      | SKILL.md 按需注入并保留                        | SKILL.md actions 变 schema         | SkillIndex/SkillView              |
| MCP        | tools/resources/skills 分开发现             | ActionSpec 投影 tools               | MCP -> ToolRegistry/ResourceIndex |
| Todo/Plan  | Todo progress 与 Plan approval 分离        | todo 在 operator skill             | native tools + restore            |
| Subagent   | AgentTool first-class                   | subagent/team 另一套 dispatch        | task harness                      |
| Eval       | transcript/tool/result 级别               | mock JSON action 主导               | native harness evals              |


---

## 4. 完整重构 TODO（按实现顺序）

### Phase 0 — Harness 入口冻结与测试分层

- 冻结 legacy JSON action loop，命名为 `legacy_skill_action_loop`。
- 所有新增 coding/harness 能力不得继续走 `SKILL.md actions`。
- 旧测试分为 `legacy_action_tests` 与 `native_harness_tests`。
- 新增 architecture guard：禁止新增 coding 类 `agent_action/input_schema`。
- 建立 harness trace ID：每轮、每个 tool_use、每个 tool_result 都有稳定 ID。

### Phase 1 — Tool descriptor 与 ToolRegistry

- 新增 `nerya/tools/types.py`：`ToolDescriptor`、`ToolCall`、`ToolResult`、`ToolError`。
- 新增 `nerya/tools/registry.py`：注册 native、MCP、legacy adapter tools。
- descriptor 包含 `name/namespace/description/input_schema/read_only/is_concurrency_safe/risk_level/permission_scope/result_kind/max_result_tokens`。
- 从 skill manifest 迁移 `agent_query_only/agent_concurrency_safe/result_kind` 到 tool descriptor。
- legacy skill action 只通过 `LegacySkillToolAdapter` 接入。

### Phase 2 — Provider-native ToolExecutor

- 新增 `nerya/agent/native_tool_executor.py`。
- 输入 provider `tool_use` block，输出 provider-compatible `tool_result` block。
- unknown tool 必须生成 `is_error=true` 的 tool_result。
- input schema validation error 必须生成可恢复 tool_result。
- tool.validateInput 二次校验失败也必须回填 tool_result。
- 每次 tool call 记录 trace，但 transcript 是 source of truth。

### Phase 3 — Tool orchestration：并发/串行/上下文修改

- 新增 `nerya/agent/tool_orchestrator.py`。
- 对一轮 assistant 返回的多个 tool_use 进行 partition。
- read-only + concurrency-safe 连续 batch 并发执行。
- mutating / shell / permission-required tool 串行执行。
- 并发执行先收集 context modifiers，再按 tool_use 顺序 apply。
- in-progress tool ids 进入 streaming state 和 UI。

### Phase 4 — PermissionEngine 与 approval transcript

- 新增 `nerya/permissions/engine.py`。
- 输入 tool descriptor + input + context，输出 allow/ask/deny。
- 权限结果必须进入 transcript，而不仅是 ACP pending queue。
- 支持 session 临时批准、永久规则、deny rule、mode override。
- permission denial 回填 tool_result，模型可继续换方案。
- ACP `tool.approve` 与 dashboard approval 写回同一 permission result。

### Phase 5 — BashTool 级 shell 安全

- 从 operator `terminal` 迁移到 native `bash` / `run_shell` tool。
- 支持 command、description、timeout、background、cwd、sandbox policy。
- 实现 shell parser / AST 或最小命令语义解析。
- 实现 command prefix permission rule。
- 实现 destructive classifier：rm/mv/chmod/chown/git clean/schema migration/secrets/network 等。
- 支持 sandbox 与危险命令审批。
- 支持 background task 的 start/status/output/stop。
- 输出 stdout/stderr 截断、exit code、duration、timeout 标记。

### Phase 6 — Native file/search/edit tools

- 从 `operator_skill/scripts/handlers.py` 抽出 `file_state.py`、`file_tools.py`、`search_tools.py`。
- `read_file`、`glob`、`grep`、`edit_file`、`write_file` 注册为 native tools。
- read state 与 edit/write freshness 由 shared context 管理。
- edit 多匹配时返回可恢复错误，要求更具体 old_string。
- 每次 mutation 返回 diff，并产生 UI diff event。
- 读文件时触发 skill discovery / conditional skill activation。

### Phase 7 — Tool lifecycle hooks

- 新增 pre-tool hooks、post-tool hooks、permission-denied hooks。
- hook 可以 block、append context、update MCP output、stop continuation。
- hook 输出必须进入 transcript attachment/event。
- hook error 不应让整个 loop 崩溃；应变成 hook error attachment。
- dashboard 展示 hook timing 与 blocking reason。

### Phase 8 — StreamingToolExecutor 与进度展示

- 模型 streaming 期间识别完整 tool_use 后即可启动工具。
- in-progress tool ids 通过 `StreamingEventBus` 发布。
- tool progress、partial output、approval request、diff event 都进入统一 event stream。
- streaming executor 结束时输出剩余 tool results。
- dashboard/TUI 根据 tool_use_id 合并展示 progress/result/error。

### Phase 9 — Compact 与 artifact restore 接入主路径

- tool-result budget 在每次模型调用前执行。
- microcompact 对 read/grep/glob/shell/web/tool result 做 token 裁剪。
- autocompact 按 API round compact，而不是按自然 user turn 粗切。
- compact 后恢复最近读过的文件 attachment。
- compact 后恢复 plan、plan mode、todo、invoked skills、async agents。
- compact 不能破坏 tool_use/tool_result pairing。

### Phase 10 — Todo / Plan / Verification agent

- `todo_write` 从 operator skill 迁出为 native tool。
- todo state 写入 app/session state，并可从 transcript restore。
- Plan Mode 与 TodoWrite 严格分离。
- `exit_plan_mode` 发起 approval，不是普通 final answer。
- 完成 3+ todo 且无验证步骤时，提示或触发 verifier agent。

### Phase 11 — Subagent / Task harness

- 建立 native `agent` tool，支持 subagent type、permission mode、prompt、description。
- 支持 background async agent，产出 output file，但主 agent 不自动读取全文。
- 支持 progress notification 与 completion notification。
- 支持 task list/get/output/stop/update 工具。
- coordinator 必须负责集成，不把理解和决策外包给子 agent。

### Phase 12 — MCP harness 重构

- MCP tools 注册到 `ToolRegistry`，不是从 `ActionSpec` 反推。
- MCP resources 注册到 `ResourceIndex`。
- MCP skills 注册到 `SkillIndex`。
- MCP result 统一转换为 provider-compatible tool_result。
- MCP session expired / connection closed 支持清缓存、重连、可恢复错误。
- post-tool hook 可以修改 MCP output。

### Phase 13 — Error taxonomy 与 retry/recovery

- 统一错误类型：schema_validation、permission_denied、tool_not_found、timeout、aborted、rate_limit、sandbox_denied、stale_file、diff_conflict、provider_error、mcp_session_expired。
- 每类错误定义：是否 retry、是否 ask user、是否 re-read、是否 stop loop。
- 错误进入 transcript tool_result，而不是只写 `errors` journal。
- LLM provider error 与 tool error 分开处理。
- max output / partial tool_use 要有 repair 或 interrupted result。

### Phase 14 — Progress summary / final report

- 每个 tool batch 生成短 summary label，用于 UI 和日志。
- final report 自动读取 artifact index：modified files、created files、commands、tests、errors、unverified risks。
- UI 展示：tool started、tool progress、approval pending、diff ready、tool error、compact event。
- final answer 不从模型记忆猜测，而从 artifact index 汇总。

### Phase 15 — Eval / regression

- 新增 transcript-level mock provider，可返回多轮 tool_use。
- Eval 1：读文件 -> grep -> edit -> run shell -> final。
- Eval 2：tool input schema 错误 -> tool_result error -> 模型修正参数。
- Eval 3：permission ask -> approve -> tool 执行 -> result 回填。
- Eval 4：permission deny -> 模型走替代路径。
- Eval 5：shell dangerous command 被拦截。
- Eval 6：compact 后继续编辑，仍知道已读/已改文件。
- Eval 7：skill_index -> skill_view -> script.inspect -> script.run。
- Eval 8：MCP tool session expired -> reconnect/retry。
- Eval 9：subagent async completion 不污染主上下文。
- Eval 10：interrupt during tool_use 产生 interrupted tool_result，resume 后 transcript 有效。

---

## 5. Cursor 实施优先级

### P0：必须先做

- `ToolRegistry`
- provider-native `ToolExecutor`
- native file/search/edit tools
- transcript tool_result 回填
- schema validation / unknown tool error result
- minimal PermissionEngine
- native loop feature flag

### P1：形成可用 coding agent

- BashTool 风险控制
- Tool orchestration concurrency partition
- TodoWrite native tool
- Plan/ExitPlanMode native tools
- compact 接入主 loop
- streaming progress events
- UI diff / tool progress

### P2：成为强 Agent 产品

- SkillIndex / SkillView / ScriptRunner
- MCP ToolRegistry / ResourceIndex / SkillIndex
- AgentTool / async task harness
- hook system
- verifier agent / eval framework
- full session restore / branch / interrupt repair

---

## 6. 不要再做的错误方向

- 不要继续把 `operator_skill/SKILL.md` 变成越来越大的工具清单。
- 不要用 manifest `agent_query_only` 代替 tool descriptor 的 `read_only/isConcurrencySafe`。
- 不要用 `ToolRunner.call_parallel` 代替 native tool orchestration。
- 不要让 shell 只靠 `_is_destructive` 字符串规则。
- 不要把 permission 只做成 ACP approve/reject，而不进入 transcript。
- 不要把 compact 做成 observation summary；必须保留 tool_use/tool_result 结构。
- 不要让 TODO 继续藏在 operator skill。
- 不要让 MCP 继续从 `ActionSpec.input_schema` 生成。
- 不要通过 mock JSON action 测试证明新 Agent harness。

---

## 7. 最小 MVP 验收场景

Cursor 完成第一阶段后，必须能跑通这个 transcript 场景：

1. user：请读取 `README.md`，找到测试命令，修改一个小 bug 并运行测试。
2. assistant：返回 `tool_use(read_file)`。
3. harness：执行 native `read_file`，回填 `tool_result`。
4. assistant：返回 `tool_use(grep)`。
5. harness：执行 native `grep`，回填 `tool_result`。
6. assistant：返回 `tool_use(edit_file)`。
7. harness：检查 fresh read，写入文件，回填 diff tool_result。
8. assistant：返回 `tool_use(run_shell)`。
9. harness：权限检查，通过后运行，回填 stdout/stderr/exit code。
10. assistant：final answer，总结改动、验证、风险。

这个场景里不允许出现：

- strict JSON action decision；
- operator skill allowlist 阻断 read/edit；
- tool result 只进入 journal 不进入 transcript；
- compact 后丢失 read/edit/test artifact；
- final answer 只靠模型记忆猜测。