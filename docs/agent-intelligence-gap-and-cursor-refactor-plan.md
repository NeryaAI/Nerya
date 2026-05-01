# Nerya Agent 智能度差距诊断与 Cursor 重构指南

更新时间：2026-04-28

本文目标不是继续给 Nerya 增加更多“动作包装”，而是指导 Cursor 把 Nerya 从“静态技能动作执行器”重构成更接近 Claude Code / Hermes 的“workspace-native coding agent”。

结论先行：Nerya 当前最大问题不是少几个读文件或编辑文件工具，而是主 Agent loop、上下文、工具定义、skill 加载、MCP 导出、测试用例共同把模型锁进了 `SKILL.md actions -> strict JSON decision -> ToolRunner.call` 的窄通道。这个通道能跑通单元测试，但会显著降低真实编码智能体的探索能力、纠错能力、上下文连续性和工具选择灵活度。

---

## 1. 本次调查范围

### Nerya 重点文件

- `Nerya/nerya/agent/kernel.py:510` — 从 skill manifest 的 `agent_action` 构造主 Agent 动作映射。
- `Nerya/nerya/agent/kernel.py:547` — 从 skill manifest 构造 LLM 可见 action catalog。
- `Nerya/nerya/agent/kernel.py:1169` — 构造主循环上下文并注入 action catalog。
- `Nerya/nerya/agent/kernel.py:1260` — 主循环迭代入口。
- `Nerya/nerya/agent/kernel.py:1270` — 只把最近 5 条 observation 追加回字符串上下文。
- `Nerya/nerya/agent/kernel.py:1317` — 通过 `LLMGateway.call(prompt=ctx)` 调模型。
- `Nerya/nerya/agent/kernel.py:1321` — 用 `parse_decision` 解析模型输出，而不是消费 provider-native tool calls。
- `Nerya/nerya/agent/kernel.py:1426` — 从 `_action_map` 查 action 名称。
- `Nerya/nerya/agent/kernel.py:1456` — 静态 selected skill allowlist 会阻断模型想调用的工具。
- `Nerya/nerya/agent/kernel.py:1541` — action 最终通过 `ToolRunner.call` 执行。
- `Nerya/nerya/agent/context_builder.py:100` — 渲染 manifest-sourced action catalog。
- `Nerya/nerya/agent/context_builder.py:119` — prompt 明确打印 “skills available this turn”。
- `Nerya/nerya/agent/context_builder.py:124` — prompt 强制模型输出 STRICT JSON。
- `Nerya/nerya/agent/context_builder.py:291` — 主 agent 指令再次强制 STRICT JSON object only。
- `Nerya/nerya/agent/context_builder.py:313` — 只允许调用 catalog 里的 actions。
- `Nerya/nerya/agent/output_parser.py:9` — `parse_decision` 只把原始文本/JSON 解析成 action dict。
- `Nerya/nerya/agent/planner.py:149` — `plan_turn` 先按 route 表选择 skills/tier/subagents。
- `Nerya/nerya/agent/skill_selector.py:86` — 根据 plan skills、安装状态、策略 allowlist 筛选技能。
- `Nerya/nerya/harness/tool_runner.py:162` — `ToolRunner.call` 执行单个 skill action。
- `Nerya/nerya/harness/tool_runner.py:282` — query-only 由 manifest 的 `agent_query_only` 决定。
- `Nerya/nerya/harness/tool_runner.py:300` — coding metadata 也来自 manifest flags。
- `Nerya/nerya/llm/gateway.py:84` — LLM gateway 入口只接收单个 `prompt` 字符串。
- `Nerya/nerya/llm/gateway.py:167` — router dispatch 仍然是 `prompt=clean_prompt`。
- `Nerya/nerya/llm/gateway.py:218` — capability matrix 知道模型能力，但主 loop 没有用 messages/tools 驱动。
- `Nerya/nerya/llm/streaming.py:58` — 已有 `tool_use_start` / `tool_use_input` 事件抽象。
- `Nerya/nerya/agent/transcript_compact.py:1` — 已有 tool_use/tool_result transcript-aware compaction 模块。
- `Nerya/nerya/agent/context_budget.py:1` — 已有 context budget / microcompact / autocompact 目标模块。
- `Nerya/nerya/agent/session.py:5` — session 文件是 compact indexed summary，不是完整 provider-shaped transcript。
- `Nerya/nerya/skills/manifest.py:14` — `ActionSpec` 把 skill 变成 typed action schema。
- `Nerya/nerya/skills/manifest.py:258` — 从 `actions_raw` 解析 action 列表。
- `Nerya/nerya/skills/registry.py:63` — 加载 builtin skills。
- `Nerya/nerya/skills/registry.py:221` — procedural `SKILL.md` 被注册成 synthetic `run` handler。
- `Nerya/nerya/skills/registry.py:246` — 当前脚本布局仍服务于 action dispatch。
- `Nerya/nerya/mcp/dynamic_tools.py:22` — MCP dynamic tools 直接使用 `ActionSpec.input_schema`。
- `Nerya/nerya/mcp/dynamic_tools.py:405` — MCP 工具过滤仍围绕 manifest action policy。
- `Nerya/nerya/skills/builtin/operator_skill/SKILL.md:40` — operator skill 把读写文件声明成 skill actions。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:229` — `read_file` 在 skill handler 内实现。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:645` — `edit_file` 在 skill handler 内实现。
- `Nerya/tests/test_agent_actions.py:3` — 测试通过 `<<MOCK_DECISION:{...}>>` 驱动 strict JSON action。
- `Nerya/tests/test_mcp_dynamic_tools.py:11` — MCP 测试验证 manifest `input_schema` 到 dynamic tool 的映射。
- `Nerya/tests/test_architecture_audit.py:177` — 测试要求每个 skill action 有 `name/permissions/input_schema`。

### Claude Code 对照文件

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:365` — 每轮从 message transcript 构造 `messagesForQuery`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:379` — 先做 tool-result budget。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:414` — 调用 microcompact。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:454` — 调用 autocompact。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:659` — `callModel` 接收 `messages/systemPrompt/tools/signal/options`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:829` — 从 assistant message 中收集 provider-native `tool_use` block。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:1380` — 执行 streaming 或 normal tools。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:1395` — tool result 被 normalize 成 API messages。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:1716` — 下一轮 message array = 原 messages + assistant messages + tool results。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/constants/prompts.ts:444` — `getSystemPrompt` 组装系统提示。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/context.ts:116` — system context 被缓存并注入会话。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/context.ts:155` — user context 负责项目规则等动态上下文。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:337` — FileRead 是 first-class tool。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:496` — FileRead 的 tool call 入口。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:542` — read dedup 通过 `readFileState` 判断。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:575` — 读文件时触发 skill directory discovery。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:589` — 按文件路径激活 conditional skills。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:1032` — 读后更新 `readFileState`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:275` — 编辑前要求已读文件。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:291` — 编辑前检查文件是否被外部修改。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:316` — 用实际字符串匹配提高编辑精度。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:329` — 多匹配时要求更具体上下文。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:516` — 编辑后通知 diff view。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:520` — 编辑后更新 `readFileState`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileWriteTool/FileWriteTool.ts:198` — 写文件前要求已读文件。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileWriteTool/FileWriteTool.ts:328` — 写后通知 diff view。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/skills/loadSkillsDir.ts:403` — skill 目录只支持 `skill-name/SKILL.md`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/skills/loadSkillsDir.ts:447` — 解析 frontmatter 和 markdown body。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/SkillTool/SkillTool.ts:1065` — 注入 skill 前剥离 YAML frontmatter。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/SkillTool/SkillTool.ts:1088` — 记录 invoked skill，供 compaction preservation 使用。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:2171` — MCP client 同时发现 tools、commands、skills、resources。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:2344` — reconnect/list 阶段重新发现 MCP tools / skills / resources。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:2720` — MCP result 被统一转换为 tool result。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:3220` — MCP session expired / connection closed 有恢复路径。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/EnterPlanModeTool/prompt.ts:1` — Plan Mode 是计划/研究模式，不是执行进度 TODO。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/ExitPlanModeTool/prompt.ts:1` — ExitPlanMode 用于提交计划并请求批准。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/TodoWriteTool/prompt.ts:3` — TodoWrite 是编码会话里的结构化任务清单工具。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/TodoWriteTool/prompt.ts:151` — TODO 要有 `content` 和 `activeForm`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/TodoWriteTool/prompt.ts:156` — TODO 要实时更新，且只有一个 in_progress。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/utils/sessionRestore.ts:73` — resume 时从 transcript 的 TodoWrite tool_use 恢复 todos。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/utils/sessionRestore.ts:138` — session restore 实际注入恢复后的 todo state。

---

## 2. 根因判断：Nerya 为什么显得“不聪明”

### 2.1 主循环是“动作 JSON 解释器”，不是 tool-call agent loop

Nerya 现在的主循环本质是：

1. `build_context` 拼出一个大字符串 prompt。
2. prompt 要求模型只输出 STRICT JSON。
3. `parse_decision` 把 JSON 解析成 `{action: ...}`。
4. `_action_map` 把 action 名字映射到某个 skill/action。
5. `ToolRunner.call` 执行 action。
6. 把最近 5 条 observation summary 拼回 prompt 字符串。

对应证据：

- `Nerya/nerya/agent/context_builder.py:291`
- `Nerya/nerya/agent/output_parser.py:9`
- `Nerya/nerya/agent/kernel.py:1270`
- `Nerya/nerya/agent/kernel.py:1317`
- `Nerya/nerya/agent/kernel.py:1321`
- `Nerya/nerya/agent/kernel.py:1426`
- `Nerya/nerya/agent/kernel.py:1541`

Claude Code 的 loop 则是 provider-shaped transcript loop：

1. 从历史 messages 构造 `messagesForQuery`。
2. 调模型时传入 `messages/systemPrompt/tools/signal/options`。
3. assistant 返回 `tool_use` blocks。
4. tool executor 执行 tool。
5. tool results normalize 成 user/tool result messages。
6. 下一轮继续用完整 message array。

对应证据：

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:365`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:659`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:829`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:1380`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:1395`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:1716`

影响：

- Nerya 的模型不能自然决定多个 provider-native tool call。
- 工具调用结果不是一等 transcript event，而是被压扁成 summary。
- 工具调用和模型下一轮推理之间缺少 `tool_use_id/tool_result` 不变量。
- 错误恢复依赖 JSON 决策重试，不是 provider-native tool-result recovery。
- 单元测试容易 mock 一段 JSON 就通过，但无法证明真实模型能进行长链编码。

### 2.2 Skill 被错误地当成工具 schema 容器

Nerya 的 repo 规则已经写得很清楚：

- `Nerya/AGENTS.md:23` — 外部调用由 Skill 和 approved scripts mediated。
- `Nerya/AGENTS.md:25` — Skill 是 `SKILL.md` 声明的 model/operator-facing playbook。
- `Nerya/AGENTS.md:26` — executable logic 属于 `scripts/`。
- `Nerya/AGENTS.md:100` — 不要把 skill instructions、action catalogs、大 schema 编进 YAML/manifests。
- `Nerya/AGENTS.md:103` — 脚本是 executable helpers，不是 skill definition。

但当前实现仍然在反方向走：

- `Nerya/nerya/skills/manifest.py:14` 定义 `ActionSpec`。
- `Nerya/nerya/skills/manifest.py:258` 从 `actions_raw` 解析 actions。
- `Nerya/nerya/agent/kernel.py:510` 把 `agent_action` 合并进 `_action_map`。
- `Nerya/nerya/agent/kernel.py:547` 把 manifest actions 变成 prompt action catalog。
- `Nerya/nerya/mcp/dynamic_tools.py:22` 把 `ActionSpec.input_schema` 变成 MCP tool schema。

这就是用户直觉里“为了跑通用例，把脚本入参塞进 skill.md/actions 再塞进 tool call 上下文”的根源。它把 `SKILL.md` 从“模型可读的操作指南”扭曲成“工具注册表 + schema + action catalog”。

Claude Code 的 skill 更接近正确模型：

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/skills/loadSkillsDir.ts:403` — skill 目录是 `skill-name/SKILL.md`。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/skills/loadSkillsDir.ts:447` — frontmatter 和 markdown body 分离解析。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/SkillTool/SkillTool.ts:1065` — 注入 skill 时剥离 frontmatter。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/SkillTool/SkillTool.ts:1088` — invoked skill 被记录供 compact 保留。

正确方向：skill 是按需加载的说明书；脚本是 skill 说明书提到的可执行辅助文件；工具是独立的一等能力，不应该从 skill actions 反推出来。

### 2.3 Coding primitives 被藏在 operator skill 里，模型缺少 workspace-native 自由度

Nerya 已经实现了一些不错的文件工具语义：

- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:91` — workspace 内路径解析和逃逸拒绝。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:229` — `read_file`。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:430` — edit 前检查 fresh cache。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:645` — `edit_file`。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:680` — edit 前 fresh read 校验。
- `Nerya/nerya/skills/builtin/operator_skill/scripts/handlers.py:876` — `grep`。

但这些能力被声明在 `operator_skill/SKILL.md` 的 actions 里：

- `Nerya/nerya/skills/builtin/operator_skill/SKILL.md:40`
- `Nerya/nerya/skills/builtin/operator_skill/SKILL.md:51`
- `Nerya/nerya/skills/builtin/operator_skill/SKILL.md:152`
- `Nerya/nerya/skills/builtin/operator_skill/SKILL.md:191`

这会造成几个真实问题：

- 文件读写是否可用取决于 planner/selector 是否允许 operator skill。
- 编码工具要和交易/策略/domain skills 一样走 manifest action catalog。
- 模型看到的是“技能动作列表”，不是熟悉的 `Read/Edit/Write/Bash/Grep/Glob` coding primitives。
- 未来加一个更强模型，它仍然被旧 schema 限死。

Claude Code 的做法相反：

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileReadTool/FileReadTool.ts:337` — FileRead 是 first-class tool。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileEditTool/FileEditTool.ts:275` — FileEdit 是 first-class tool，且有读后编辑约束。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/FileWriteTool/FileWriteTool.ts:198` — FileWrite 也是 first-class tool。

Nerya 应该把 coding primitives 从 skill action 里提升出来，成为主 loop 默认工具集的一部分。

### 2.4 上下文管理已经写了模块，但主 loop 没真正用起来

Nerya 有看起来接近 Claude Code 的模块：

- `Nerya/nerya/agent/context_budget.py:1`
- `Nerya/nerya/agent/transcript_compact.py:1`
- `Nerya/nerya/llm/streaming.py:58`

但当前主 loop 实际用的是：

- `Nerya/nerya/agent/kernel.py:1270` — append `accumulated_obs[-5:]` 到字符串 prompt。
- `Nerya/nerya/agent/session.py:5` — session 是 compact indexed summary，不是完整 transcript。
- `Nerya/nerya/llm/gateway.py:84` — call 入口只接收 `prompt`。

这说明 Nerya 可能出现了“文档/模块已补齐，但主路径没有换”的问题。Cursor 如果只让 `context_budget.py` 或 `transcript_compact.py` 的单测通过，却不把它们接入 `AgentKernel._run_turn_body` 和 LLM adapter，就不会提升真实 Agent 智能度。

Claude Code 是在主 loop 里先后应用：

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:379` — tool-result budget。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:414` — microcompact。
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/query.ts:454` — autocompact。

所以 Nerya 的重构验收标准必须是“主 loop 真实走 transcript compaction”，不是“模块存在”。

### 2.5 Planner/skill selector 过早替模型做决定

Nerya 的 planner 在模型调用前就根据 route table 决定 skill 子集：

- `Nerya/nerya/agent/planner.py:149`
- `Nerya/nerya/agent/planner.py:157`
- `Nerya/nerya/agent/planner.py:170`
- `Nerya/nerya/agent/skill_selector.py:86`
- `Nerya/nerya/agent/kernel.py:1456`

这对交易机器人可能有安全价值，但对 coding agent 会让模型缺少临场探索能力。例如用户说“深入挖掘工具加载机制”，模型应能自由 `grep`、`read_file`、查看 docs、再决定是否调用 skill/MCP；而不是先被 route 表限制在某几个 skills 内。

目标不是删除安全边界，而是分层：

- Coding primitives 默认可见，但 mutating tools 有审批/风险等级。
- Domain skills 按需 discover/load。
- 策略交易类 actions 仍可保留 route/allowlist。
- coding 模式不要经过 domain planner 的静态技能白名单。

### 2.6 MCP 现在是 manifest action 的投影，不是独立 tool registry

当前 MCP dynamic tool 的核心来源仍是 `ActionSpec.input_schema`：

- `Nerya/nerya/mcp/dynamic_tools.py:22`
- `Nerya/nerya/mcp/dynamic_tools.py:76`
- `Nerya/nerya/mcp/dynamic_tools.py:405`

这会导致 MCP 也继承 skill/action/schema 绑死的问题。Claude Code 的 MCP client 则同时发现 tools、commands、skills、resources，并且结果统一进入 tool result 通道：

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:2171`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:2344`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:2720`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/services/mcp/client.ts:3220`

Nerya 应改成：MCP tools 来自 `ToolRegistry`；MCP skills/resources 进入 `SkillIndex` 或 resource index；不要再从 `SKILL.md actions` 生成 MCP tools。

### 2.7 测试体系正在强化错误架构

这些测试说明当前单测更关注“mock JSON action 是否能跑通”，而不是“真实 agent transcript 是否能循环”：

- `Nerya/tests/test_agent_actions.py:3`
- `Nerya/tests/test_e2e_scenarios_offline.py:3`
- `Nerya/tests/test_local_api_server.py:129`
- `Nerya/tests/test_action_registry.py:3`
- `Nerya/tests/test_mcp_dynamic_tools.py:11`
- `Nerya/tests/test_architecture_audit.py:177`

这正是用户担心的“为了跑通单元测试，功能和文档不对齐”。Cursor 下一步不能只更新旧测试断言，而要新增能证明真实架构的 contract/eval：

- provider-native `messages + tools` 被调用。
- assistant `tool_use` block 被执行。
- tool result 带 `tool_use_id` 回填。
- compact 后 transcript 仍满足 tool_use/tool_result 配对。
- skill body 是按需注入，不是 action catalog 每轮塞满。
- coding primitives 默认可见，且 edit/write 需要 fresh read。

---

## 3. Nerya 目标架构

### 3.1 新核心：`WorkspaceNativeAgentLoop`

新增或重写主 loop，使它以 message transcript 为中心：

- 输入：`systemPrompt[]`、`systemContext`、`userContext`、`messages[]`、`tools[]`、`toolPermissionContext`、`abortSignal`。
- 模型输出：assistant content blocks，包括 text/thinking/tool_use。
- 执行：`ToolExecutor` 根据 tool name 调用一等工具。
- 回填：每个 tool result 以 provider 兼容 block/message 回到 transcript。
- 递归：下一轮 messages = compacted prior messages + assistant messages + tool result messages。
- 结束：模型无 tool_use、达到 stop reason、用户中断、权限拒绝、max turns、预算耗尽。

不要让新 loop 输出 `{"action": ...}`，也不要要求模型“JSON only”。

### 3.2 新工具层：`ToolRegistry` 独立于 `SkillRegistry`

工具分三层：

1. **Always-on coding primitives**
  - `read_file`
  - `list_dir`
  - `grep`
  - `glob`
  - `edit_file`
  - `write_file`
  - `run_shell`
  - `apply_patch` 或 patch proposal tool
  - `todo_write`
  - `enter_plan_mode` / `exit_plan_mode`
  - `skill_index` / `skill_view`
2. **Dynamic MCP tools**
  - 从 MCP server `tools/list` 来。
  - 进入同一个 `ToolRegistry`。
  - 有 server namespace、permission policy、result adapter、reconnect/error recovery。
3. **Domain tools / legacy skill actions**
  - 只作为迁移期兼容层。
  - 不再作为 coding agent 的主要能力来源。
  - 不允许新建 `agent_action/input_schema` 来扩展 coding 能力。

### 3.3 新 skill 层：`SkillIndex + SkillView + ScriptRunner`

重构后 skill 应该这样工作：

- `SkillIndex` 扫描 `SKILL.md`，只读 frontmatter + very short summary。
- `SkillView` 在模型需要时返回完整 markdown body，剥离 frontmatter。
- `SkillView` 提供 linked files 列表：`references/`、`templates/`、`scripts/`、`assets/`。
- `ScriptRunner` 是通用脚本执行工具，不为每个脚本提前生成 LLM tool schema。
- 模型需要脚本参数时，通过 `scripts/README.md`、`--help`、或 `script.inspect` 获取。
- skill 被调用后记录 `invoked_skill`，compact 时必须保留 skill summary 或重新注入入口。

这和 `Nerya/AGENTS.md:97` 到 `Nerya/AGENTS.md:112` 一致。

### 3.4 新上下文层：`TranscriptStore + ContextBudget + ArtifactIndex`

必须把上下文拆成四类：

1. **Static prompt cache boundary**
  - 系统身份、工具规则、安全规则、输出风格。
  - 变化少，适合缓存。
2. **Project context**
  - CWD、git status、repo rules、AGENTS/NERYA/README 摘要。
  - 按路径 scope 加载，不要全量塞。
3. **Transcript context**
  - user/assistant/tool_use/tool_result messages。
  - 保持 provider-shaped，不要压成字符串 observations。
4. **Artifact index**
  - read files、modified files、created files、diffs、commands、test results、pending risks。
  - 这是 coding agent 长任务不失忆的关键，不能只依赖自然语言 summary。

### 3.5 Plan 和 Todo 要分开

Claude Code 的对照很明确：

- Plan Mode 是研究/计划/审批状态。
- TodoWrite 是执行进度状态。
- TodoWrite 每个任务有 `content` 和 `activeForm`。
- Todo 状态可以从 transcript restore。

证据：

- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/EnterPlanModeTool/prompt.ts:1`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/ExitPlanModeTool/prompt.ts:1`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/TodoWriteTool/prompt.ts:3`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/TodoWriteTool/prompt.ts:151`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/tools/TodoWriteTool/prompt.ts:156`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/utils/sessionRestore.ts:73`
- `C:/Users/Ricky/Documents/Project/ClaudeCode/anthropic-ai-claude-code-2.1.88-expanded/src/utils/sessionRestore.ts:138`

Nerya 不应该把 Plan、route planner、TODO、skill selector 混成一个全局 planner。建议：

- `enter_plan_mode`：进入计划态，可读文件和搜索，但默认不改文件。
- `exit_plan_mode`：提交计划并请求用户批准。
- `todo_write`：编码执行时实时更新任务进度。
- `plan artifacts`：长计划可保存到 `.nerya/plans/` 或 docs。
- `todo state`：保存在 transcript tool_use 或 session state 中，可恢复。

---

## 4. Cursor 重构 TODO（必须按顺序执行）

### Phase 0 — 锁定现状与防止继续跑偏

- 在 `Nerya/docs` 添加本文件作为 Cursor 重构入口。
- 在 Cursor 的任务说明里明确：不要继续往 `SKILL.md actions` 里塞 coding tool schema。
- 标记 legacy surfaces：`ActionSpec.agent_action`、`agent_payload_hint`、`agent_query_only`、`mcp.dynamic_tools` 只能迁移使用。
- 新增 architecture guard：新 skill 不允许引入新的 `actions:` 大 schema；coding 能力必须走 `ToolRegistry`。
- 给测试分类：legacy JSON-action tests、new transcript-tool-call tests、skill-loading tests、MCP tests。

验收：

- `rg -n "agent_action|agent_payload_hint|input_schema" Nerya/nerya/skills/builtin` 有迁移清单，不再新增 coding 能力。
- 文档和测试命名能区分 legacy 与 new loop。

### Phase 1 — 引入 provider-native `AgentTranscript`

- 新增 `nerya/agent/transcript.py`，定义 `Message`、`ContentBlock`、`ToolUseBlock`、`ToolResultBlock`、`AssistantTurn`。
- 新增 `TranscriptStore`，记录完整 messages，而不是只记录 compact indexed summary。
- 兼容现有 journals，但 journals 不再替代 transcript。
- 接入 `nerya/agent/transcript_compact.py` 的 `validate_transcript`。
- 所有 tool result 必须有可追踪的 `tool_use_id`。

验收：

- 单测构造 assistant `tool_use` + user `tool_result`，`validate_transcript` 通过。
- 缺失 tool result、孤儿 tool result 会失败。
- session resume 后仍能看到上一轮 tool_use/tool_result pairing。

### Phase 2 — 重写 LLMGateway tool-call 接口

- 保留 `LLMGateway.call(prompt=...)` 给 legacy 路径。
- 新增 `LLMGateway.call_messages(messages, system, tools, tool_choice, stream, signal, ...)`。
- OpenAI/Anthropic/Gemini/OpenRouter adapter 分别实现 messages + tools。
- capability matrix 的 `supports_tool_calling` 必须决定是否可走 new loop。
- 不支持 tool calling 的 provider 只能走兼容 fallback，不能假装是 new loop。

验收：

- mock adapter 收到的是 `messages/tools`，不是单个 prompt 字符串。
- provider 返回 tool call 后，Nerya 能解析成统一 `ToolUseBlock`。
- `Nerya/nerya/llm/streaming.py` 的 tool_use events 接入真实 adapter streaming。

### Phase 3 — 建立独立 `ToolRegistry`

- 新增 `nerya/tools/registry.py`。
- 工具 descriptor 包含：name、description、input_schema、risk、permission、read_only、result_budget、namespace、source。
- 从 operator skill 迁移出 first-class coding tools。
- `read_file/list_dir/grep/glob/edit_file/write_file/run_shell` 默认进入 coding agent toolset。
- `ToolExecutor` 统一执行 local tools、MCP tools、legacy skill action adapters。
- `ToolRunner` 只保留为 legacy skill action executor，不能作为主 coding tool executor。

验收：

- coding agent 不需要 `operator` skill allowlist 也能读文件。
- tool list 中 `read_file` 来源是 `native` 或 `workspace`，不是 `skill:operator`。
- legacy `operator_skill` 可以保留 shim，但不再是主入口。

### Phase 4 — 把 file tools 做成 workspace-native primitives

- 从 `operator_skill/scripts/handlers.py` 提取路径解析、FileStateCache、diff 生成、fresh-read 校验到 `nerya/tools/file_ops.py`。
- `read_file` 支持 offset/limit、line range、大文件提示、binary/image/PDF fallback。
- `edit_file` 必须要求 fresh read。
- `edit_file` 多匹配时必须要求更具体上下文。
- `write_file` 对已存在文件必须要求 fresh read。
- 编辑/写入后更新 read state，并产生 diff event。
- UI/API 可以展示 diff、改动文件、风险说明。

验收：

- 未读文件直接 edit/write 会失败并提示先读。
- 文件被用户或 formatter 改动后，旧 read state 不能继续 edit。
- edit 返回 unified diff。
- 第二次编辑必须基于更新后的 file state。

### Phase 5 — 重构 `SKILL.md + scripts/` 加载

- 新增 `nerya/skills/index.py`：扫描 `SKILL.md` frontmatter，返回 compact rows。
- 新增 `skill_index.list` tool：只返回 name/description/tags/source/activation/linked files 摘要。
- 新增 `skill_view.read` tool：按需返回完整 skill body，剥离 frontmatter。
- 新增 `skill_view.file` tool：读取 `references/`、`templates/`、`scripts/`、`assets/` 下指定文件。
- 新增 `script.inspect`：读取脚本 `--help` 或 README，不提前把 schema 塞进 skill。
- 新增 `script.run`：在 skill directory 下执行明确脚本，使用 JSON/stdin/CLI args。
- 删除或冻结把 `SKILL.md actions` 直接变成 agent actions 的路径。

验收：

- 一个 Anthropic/Hermes 风格的 `SKILL.md + scripts/foo.py` skill 无 `actions:` 也能被发现、阅读、运行脚本。
- `SKILL.md` body 不会每轮自动塞进主 prompt。
- invoked skill 在 compact 后能恢复摘要或重新加载。

### Phase 6 — MCP 改成 `ToolRegistry + Resource/SkillIndex`

- MCP `tools/list` 进入 `ToolRegistry`，保留 server namespace。
- MCP resources 进入 `ResourceIndex`。
- MCP skills 进入 `SkillIndex`，不伪装成 local skill action schema。
- MCP result 统一转换成 `ToolResultBlock`。
- MCP session expired / connection closed 要有 reconnect + retry 策略。
- `mcp.dynamic_tools` 从 `ActionSpec` 投影改成 legacy compatibility。

验收：

- MCP tool 不需要 `ActionSpec` 也能被模型调用。
- MCP skill/resource 能被发现但不会污染 coding tool schema。
- MCP 错误回填给模型时有可恢复信息，而不是只记 error string。

### Phase 7 — 接入 context budget / compact 到主 loop

- 在 new loop 每轮模型调用前执行 tool-result budget。
- 执行 microcompact，折叠低价值 tool results。
- 执行 autocompact，把旧 transcript 压成结构化 summary。
- summary 必须包含：任务目标、已读文件、已改文件、diff 摘要、命令结果、失败/风险、下一步。
- compact 不能拆散 tool_use/tool_result pair。
- invoked skills 和 artifact index 必须被 compact preservation 保留。

验收：

- 长链 30+ tool calls 不再只保留最近 5 条 observation。
- compact 后模型仍能回答“读过哪些文件、改了哪些文件、还有哪些测试没跑”。
- `validate_transcript` 在 compact 前后都通过。

### Phase 8 — 新 coding loop 接管 `AgentKernel`

- 在 `AgentKernel._run_turn_body` 增加 feature flag：`agent.loop_mode = legacy_json | native_tools`。
- coding/chat 默认走 `native_tools`。
- 交易/策略旧场景可暂时走 `legacy_json`。
- `build_context` 不再渲染 full action catalog 给 native loop。
- `parse_decision` 只保留 legacy。
- selected skill allowlist 不能限制 native coding primitives。
- 中断时为已发出的 tool_use 生成 interrupted tool_result。
- 模型错误、max output、tool error 都变成可恢复 transcript event。

验收：

- 同一个用户任务能形成 transcript：user -> assistant tool_use(read_file) -> tool_result -> assistant tool_use(edit_file) -> tool_result -> assistant final。
- 中断后 resume 不会丢失 tool_use/tool_result pairing。
- old strict JSON prompt 不出现在 native loop。

### Phase 9 — Plan/Todo 独立产品化

- `enter_plan_mode` 是 tool，不是 route planner。
- `exit_plan_mode` 提交计划并请求批准。
- `todo_write` 是执行进度 tool，有 `pending/in_progress/completed`。
- todo item 支持 `content` 和 `activeForm`。
- session restore 能从 transcript 或 task state 恢复 todo。
- UI 展示 plan state、todo state、当前 active task。

验收：

- 研究任务不会被强制 exit plan。
- 执行任务能实时更新 todo。
- resume 后 todo 仍在。

### Phase 10 — 权限、Shell、安全和 Diff UI

- `run_shell` 支持 cwd、timeout、env allowlist、danger classifier。
- 写文件、shell、外部网络、MCP mutating tools 走统一 permission policy。
- 权限请求进入 transcript，批准/拒绝也进入 transcript。
- diff event 与 tool result 关联。
- final report 自动引用 artifact index：改了什么、验证了什么、风险是什么。

验收：

- 危险命令不会直接执行。
- 被拒绝的 tool call 会回填可解释结果给模型。
- UI 能展示 tool progress、diff、错误、最终报告。

### Phase 11 — 重写测试和回归评测

- 新增 `tests/test_native_agent_loop.py`。
- 新增 `tests/test_native_tool_registry.py`。
- 新增 `tests/test_skill_index_view_scripts.py`。
- 新增 `tests/test_context_compaction_in_loop.py`。
- 新增 `tests/test_interrupt_transcript_recovery.py`。
- 新增 `tests/evals/coding_agent_scenarios/`。
- 将旧 `<<MOCK_DECISION:{...}>>` 测试标记为 legacy。
- CI 里至少跑一个端到端 mock provider transcript 场景。

验收：

- 测试不再只证明 strict JSON action 能跑通。
- 至少一个 eval 覆盖：读文件 -> 搜索 -> 编辑 -> 跑测试 -> 修复错误 -> final summary。
- 至少一个 eval 覆盖：skill discover -> skill view -> script inspect -> script run。
- 至少一个 eval 覆盖：compact 后继续编辑。

---

## 5. Cursor 不应该做的事

- 不要继续给 `SKILL.md` 增加大段 `actions:` / `input_schema:` 来模拟工具调用。
- 不要把脚本每个参数都提前变成 LLM tool schema。
- 不要用 `<<MOCK_DECISION:{...}>>` 证明新 Agent loop 成功。
- 不要把 coding task 强行走 trading/domain route planner。
- 不要把 context compaction 做成单独模块后不接入主 loop。
- 不要只改 docs，不改 `AgentKernel` / `LLMGateway` / adapter 主路径。
- 不要把 MCP tools 从 skill manifest 反向生成。
- 不要让 Plan Mode 取代 TodoWrite，也不要让 TodoWrite 取代 Plan approval。

---

## 6. 最小可交付切片

如果 Cursor 只能先做一个可验证 MVP，建议顺序是：

- `ToolRegistry` + native `read_file/grep/glob/edit_file/write_file`。
- `LLMGateway.call_messages` mock adapter。
- `WorkspaceNativeAgentLoop` mock provider transcript。
- `tool_use/tool_result` 回填。
- `FileStateCache` fresh-read guard。
- `SkillIndex.list` + `SkillView.read`，不执行脚本。
- 一个端到端测试：mock provider 先 read，再 edit，再 final。

这个 MVP 通过后，再接真实 provider streaming、MCP、compact、script.run。

---

## 7. 定义完成

重构完成不是“所有旧测试通过”，而是以下能力全部成立：

- Nerya 主 coding loop 使用 provider-native `messages + tools`。
- 模型能自由选择读文件、搜索、编辑、跑命令、请求权限。
- 工具结果作为 tool result 回填，不再压成最近 5 条 observation summary。
- 上下文超长时 compact transcript，且不破坏 tool_use/tool_result 配对。
- Skill 是 `SKILL.md` playbook，按需加载；脚本是 supporting file，按需 inspect/run。
- MCP tools/resources/skills 动态发现，但不依赖 `ActionSpec`。
- Plan Mode 与 TodoWrite 分离。
- 文件编辑有 fresh read、diff、stale write 防护。
- 中断、权限拒绝、工具报错、provider 报错都会进入可恢复 transcript。
- 测试覆盖真实 transcript loop，而不是只 mock strict JSON action。

如果只完成了 manifest/action 的重排，而主 loop 仍然是 `prompt=ctx -> parse_decision -> ToolRunner.call -> accumulated_obs[-5:]`，那就没有真正解决用户指出的“Agent 灵活性太低、Coding 能力太差”的问题。