# Nerya 策略 Trigger 驱动 Agent Session 执行任务方案

## 1. 核心语义修正

这份方案按新的目标重写：

**Trigger 的职责不是直接触发策略下单，也不是触发策略 tick 后让策略调用 Agent。Trigger 的职责是唤醒一个策略绑定的 Agent Session，让 Agent 执行一次策略任务。**

策略脚本在这条链路里的职责是：

1. 根据策略定义自行获取数据，例如 K line、成交量、盘口、新闻、链上数据、资金费率。
2. 自行计算指标、因子、标签、额外字段，例如 MACD、RSI、EMA、vol_zscore、trend_score、risk_flag。
3. 自行决定数据表达方式，例如 CSV、Markdown table、JSONL 摘要、自然语言说明。
4. 最终组装成一个字符串 prompt，放入指定 Agent Session。
5. Agent 在该 Session 中复用长期下单规则、策略约束、工具权限和历史上下文，依据 prompt 执行任务。
6. 如果 Agent 判断需要交易，可以直接调用受控交易工具提交订单意图。

这里的“直接下单”必须按 Nerya 的安全边界理解：

- Agent 可以在策略任务 Session 内直接调用 `trade_intent_submit` 这类交易工具。
- 交易工具仍然必须经过 `RiskGate`、`ApprovalGate`、live trading 开关和 ExecutionEngine。
- 不能绕过 `RiskGate` / `ApprovalGate` 去直接调用交易所或钱包。

所以目标链路是：

```text
Cron / price watcher / webhook / manual replay
  -> TriggerEvent
  -> TriggerRouter route / dedup / cooldown / rate limit
  -> StrategyAgentTaskExecutor
  -> strategy script builds prompt string
  -> resolve stable Agent Session
  -> load session profile: order rules + risk rules + allowed tools
  -> AgentKernel.run_turn(payload.text = strategy prompt)
  -> Agent uses prior session context and tools
  -> trade_intent_submit when needed
  -> RiskGate -> ApprovalGate -> ExecutionEngine / VirtualLedger
  -> journals + session transcript + strategy task history + dashboard replay
```

## 2. 当前代码事实

### 2.1 Agent prompt 入口已经适合“字符串 prompt”

`nerya/agent/kernel.py` 中 `AgentKernel.run_turn(...)` 接收 `trigger`，实际传给 Agent loop 的用户文本来自：

```text
trigger.payload.text
trigger.payload.message
trigger.payload.prompt
trigger.raw
trigger.text
```

因此新方案不需要让 runtime 猜测哪些字段是 K line 或指标。策略脚本只要把最终 prompt 放到 `payload.text` 或 `payload.prompt`，Agent 就能收到。

### 2.2 Agent Session 已经能复用上下文

`SessionStore` 会持久化：

- `session_id`
- `strategy_id`
- `turn_ids`
- `invoked_skills`
- `skill_state`
- `last_action`
- `meta`

`AgentKernel.run_turn(...)` 在传入相同 `session_id` 时会加载 prior messages，把前几轮 user/assistant 重新放回上下文。实测同一 session 第二轮可以回忆第一轮 prompt 中的指标值。

现有缺口不是“Agent 不能复用 session”，而是“策略触发场景还没有稳定、可配置、可审计的 strategy-agent session policy 和 profile”。

### 2.3 scheduled agent session 已经存在，但每次都是 fresh

`ScheduledSessionRunner.run_once(...)` 当前会生成：

```text
session_id = sched:<schedule_id>:<timestamp>
```

这适合一次性定时 Agent 任务，但不适合高频交易策略，因为每次都会新建 Session，无法自然复用下单规则、历史判断和工具状态。

### 2.4 TriggerRuntime 当前只 route，不执行 Agent 任务

`TriggerRuntime.emit(...)` 当前只调用 `TriggerRouter.route(...)`。`POST /triggers/emit` 对 `target=skill:strategy.run_tick` 的实测结果是 `routed`，不会产生策略 run，也不会启动 Agent turn。

新方案应补的是：route 后面增加执行层，把策略 trigger 变成一次 Agent Session 任务，而不是继续停留在 routed 记录。

### 2.5 Agent 的策略下单权限已有基础，但需要产品化约束

`AgentKernel` 里已有 `_strategy_triggered_order_turn(...)` 判断。如果 trigger 来自 strategy / schedule / price / news / social / onchain 等来源，并且带 `strategy_id`，会给当前 turn 增加 `trade_intent_submit` 的 session permission rule。

这和新目标是匹配的：策略触发的 Agent Session 可以直接调用交易工具。

但还需要补：

- 明确 session profile 里的下单规则。
- 明确每个策略 Session 可用哪些工具。
- 明确 `strategy_id`、`session_id`、`trigger_event_id` 由 runtime 注入，不能让 Agent 自由伪造。
- 明确直接下单仍要经过 `RiskGate` / `ApprovalGate`。

## 3. 目标设计

## 3.1 新增核心概念：Strategy Agent Task

新增一个策略脚本输出契约：

```python
@dataclass
class StrategyAgentTask:
    status: Literal["dispatch", "skip", "error"]
    prompt: str
    session_key: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    attached_skills: list[str] = field(default_factory=list)
    reason: str = ""
```

策略脚本只需要返回：

- `dispatch`: 需要把 prompt 投递给 Agent Session。
- `skip`: 本轮没有信号，不唤醒 Agent。
- `error`: 数据获取或指标计算失败，记录错误但不投递 Agent。

重点：`prompt` 是策略脚本最终组装好的字符串。runtime 不再替策略脚本自动拼 K line / indicators。

## 3.2 新 target：`skill:strategy.agent_task`

推荐新增 trigger target：

```text
skill:strategy.agent_task
```

它的语义是：

1. route trigger。
2. 执行策略脚本的 prompt builder。
3. 拿到 `StrategyAgentTask.prompt`。
4. 按 session policy 解析目标 session。
5. 调用 `AgentKernel.run_turn(...)`。

保留旧 target：

```text
skill:strategy.run_tick
```

旧 target 继续表示“运行策略 tick，策略脚本自己通过 `ctx.trading.submit_intent(...)` 返回策略结果”。这和新方案区分清楚，避免概念混乱。

## 3.3 策略脚本 API

策略脚本可以提供以下入口之一：

```python
def build_agent_task(ctx) -> StrategyAgentTask:
    ...
```

或兼容旧入口：

```python
def run(ctx):
    ...
```

推荐新策略使用 `build_agent_task(ctx)`，因为它明确表达“我是给 Agent Session 生成任务 prompt”。

示例：

```python
from nerya.strategies.agent_task import StrategyAgentTask


def build_agent_task(ctx):
    market = "BINANCE:BTCUSDT"
    timeframe = "1m"

    candles = ctx.market.candles(market, timeframe=timeframe, limit=120)
    rows = add_indicators_and_factors(candles)

    if not macd_golden_cross(rows):
        return StrategyAgentTask.skip("no macd golden cross")

    csv_text = ctx.prompt.csv(
        rows,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "macd",
            "macd_signal",
            "macd_hist",
            "rsi",
            "ema_fast",
            "ema_slow",
            "volume_zscore",
            "trend_score",
        ],
    )

    prompt = f"""
You are executing strategy task: macd_golden_cross.

Market: {market}
Timeframe: {timeframe}
Mode: {ctx.mode}

Task:
- Review the latest signal data below.
- Reuse this session's order rules and position sizing rules.
- Check portfolio/risk state before trading.
- If the signal is strong enough, submit a trade intent with trade_intent_submit.
- If confidence is insufficient, do not trade and explain why.

Signal data CSV:
```csv
{csv_text}
```

Extra fields:
- trigger_event_id: {ctx.trigger.event_id}
- strategy_id: {ctx.strategy_id}
- signal: macd_golden_cross
"""

    return StrategyAgentTask.dispatch(
        prompt=prompt,
        session_key={
            "market": market,
            "timeframe": timeframe,
            "signal_family": "macd",
        },
        metadata={
            "market": market,
            "timeframe": timeframe,
            "signal": "macd_golden_cross",
            "latest_macd_hist": rows[-1]["macd_hist"],
        },
        attached_skills=["strategy_author", "trading"],
    )
```

## 3.4 Runtime 不再自动理解指标字段

旧方案里提过 `SignalContext` 和自动 prompt builder。新语义下应改成：

- Runtime 只提供 helper，帮助策略脚本安全生成 CSV / Markdown / artifact。
- Runtime 不负责判断哪些字段是指标、哪些字段是因子。
- Runtime 不把 `payload.klines` / `payload.indicators` 自动塞进 prompt。
- Runtime 只检查 prompt 大小、artifact 引用、敏感信息、session policy、工具权限。

推荐新增轻量 helper：

```text
nerya/strategies/prompt_io.py
```

能力：

- `ctx.prompt.csv(rows, columns=...)`
- `ctx.prompt.markdown_table(rows, columns=...)`
- `ctx.prompt.json_block(obj)`
- `ctx.prompt.artifact(name, content, content_type)`
- `ctx.prompt.truncate_csv(rows, max_rows=120)`

这些只是格式化工具，不改变策略作者的数据语义。

## 4. Session 设计

## 4.1 Agent Session 是策略任务执行主体

每个策略应有明确的 Agent Session 策略：

```yaml
agent_session:
  policy: per_strategy_market_timeframe
  ttl_seconds: 86400
  max_turns: 500
  compact_every_turns: 20
  include_prior_messages: true
  refresh_profile_on_change: true
```

枚举：

| policy | 语义 |
|---|---|
| `per_signal` | 每次触发新 Session，适合低频独立事件 |
| `per_strategy` | 一个策略共用一个 Agent Session |
| `per_strategy_market` | 一个策略 + 一个市场共用一个 Agent Session |
| `per_strategy_market_timeframe` | 高频交易默认推荐 |
| `custom` | 策略脚本通过 `session_key` 自定义 |

稳定 Session ID：

```python
def resolve_strategy_agent_session_id(strategy_id, session_key, policy):
    key = canonical_json({
        "policy": policy,
        "strategy_id": strategy_id,
        **session_key,
    })
    return "strat_agent_" + sha256(key.encode()).hexdigest()[:16]
```

例如：

```text
strat_agent_9f8a1e6b3d4c2a10
```

## 4.2 Session Profile：下单规则和工具权限的复用载体

仅靠 prior messages 复用规则不够稳定。应给策略 Agent Session 增加 profile：

```yaml
agent_profile:
  title: "BTCUSDT 1m MACD execution agent"
  role: "Execute strategy-generated trading tasks for this strategy only."
  order_rules:
    - "Always call portfolio_summary before opening a new position."
    - "Always call risk_check before trade_intent_submit."
    - "Never exceed max_single_order_usd from strategy policy."
    - "Do not pyramid unless current position is profitable and risk_check allows it."
    - "If MACD hist is positive but weakening for two rows, prefer hold."
  allowed_tools:
    - portfolio_summary
    - strategy_history
    - risk_check
    - trade_intent_submit
  default_trade_source: strategy_agent
  min_confidence_to_trade: 0.68
```

首次创建 Session 时，runtime 将 profile 作为 pinned context 写入 session。后续同一 Session 复用这些规则。

当策略 manifest 或 profile hash 变化时：

- 写 `session.profile.updated` journal。
- 下一个 turn 重新注入 profile。
- 保留历史 turn，但明确 profile 版本。

## 4.3 Prompt 与 Profile 的边界

Profile 放长期规则：

- 策略身份。
- 下单规则。
- 风控约束。
- 允许工具。
- 交易模式。
- 账户和 venue 边界。

Prompt 放本轮数据：

- 本轮 trigger。
- 本轮 CSV / 指标 / 因子。
- 本轮策略脚本结论。
- 本轮要 Agent 执行的任务。

这样避免每次 prompt 重复大量下单规则，也避免 Agent Session 失去策略上下文。

## 5. Agent 直接下单的安全模型

## 5.1 “直接下单”的定义

允许：

```text
Agent Session -> native tool trade_intent_submit -> RiskGate -> ApprovalGate -> ExecutionEngine
```

禁止：

```text
Agent Session -> raw exchange SDK / wallet private call
Agent Session -> bypass RiskGate
Agent Session -> bypass ApprovalGate
Agent Session -> forge another strategy_id
```

## 5.2 Runtime 注入不可伪造字段

策略 Agent Session 调用 `trade_intent_submit` 时，runtime 应强制注入或校验：

```json
{
  "strategy_id": "<current_strategy_id>",
  "session_id": "<current_session_id>",
  "trigger_event_id": "<current_trigger_event_id>",
  "source": "strategy_agent"
}
```

Agent 可以提出：

- market
- side
- size / size_usd
- order_type
- limit_price
- confidence
- reasoning

但不能覆盖：

- `strategy_id`
- `session_id`
- `source`
- `account_id`，除非 profile 允许
- live/paper mode

## 5.3 Permission 与 Risk 是两层不同的门

策略触发的 Agent turn 可以自动授予 `trade_intent_submit` 工具权限，但这只代表“允许调用工具”，不代表“订单一定执行”。

订单仍要经过：

1. `TradeIntent` schema validation。
2. `RiskGate.evaluate(...)`。
3. `ApprovalGate.require(...)` 或 strategy auto approval。
4. `ExecutionEngine.execute(...)`。
5. ledger / order / fill 记录。

## 6. 实施阶段

## Phase 0: 固化新语义测试

新增测试：

```text
tests/test_strategy_agent_task_executor.py
tests/test_strategy_agent_task_prompt_contract.py
tests/test_strategy_agent_session_profile.py
tests/test_strategy_agent_trade_guard.py
```

关键测试：

1. `test_trigger_executes_agent_task_prompt`
   - 输入 `target=skill:strategy.agent_task`。
   - 策略脚本返回 `StrategyAgentTask.dispatch(prompt="...csv...")`。
   - 期望调用 `AgentKernel.run_turn(...)`。
   - 期望 `trigger.payload.text == prompt`。

2. `test_strategy_script_owns_indicators`
   - 策略脚本在 CSV 中添加 `macd_hist`、`trend_score`、自定义字段。
   - runtime 不重排、不解释、不丢字段。
   - Agent 收到的 prompt 包含这些字段。

3. `test_same_strategy_market_timeframe_reuses_session`
   - 同一策略、市场、timeframe 连续触发两次。
   - 期望同一个 `session_id`。
   - session file 的 `turn_ids` 增加。

4. `test_agent_session_profile_is_pinned`
   - 首次触发创建 session profile。
   - 第二次触发不重复塞完整规则，但 session 可恢复规则上下文。

5. `test_agent_trade_intent_is_guarded`
   - Agent 调用 `trade_intent_submit` 时尝试传入其他 `strategy_id`。
   - runtime 覆盖或拒绝。
   - RiskGate 仍被调用。

## Phase 1: 新增 StrategyAgentTask 契约

新增：

```text
nerya/strategies/agent_task.py
nerya/strategies/prompt_io.py
```

`StrategyAgentTask` 提供：

```python
StrategyAgentTask.dispatch(prompt=..., session_key=..., metadata=...)
StrategyAgentTask.skip(reason=...)
StrategyAgentTask.error(reason=..., metadata=...)
```

`build_strategy_context(...)` 增加：

```python
ctx.prompt
ctx.trigger
ctx.strategy_id
ctx.mode
```

其中 `ctx.trigger` 仍然需要补齐，因为策略脚本要知道本轮是哪个 trigger 唤醒的。

## Phase 2: 新增 StrategyAgentTaskExecutor

新增：

```text
nerya/triggers/strategy_agent_task_executor.py
```

核心流程：

```python
class StrategyAgentTaskExecutor:
    def execute(self, event: TriggerEvent, route_result: RouterResult):
        strategy_id = resolve_strategy_id(event, route_result)
        ctx = build_strategy_context(..., trigger=event)
        task = run_strategy_agent_task(strategy_id, ctx)

        if task.status == "skip":
            return skipped_result(...)

        session_id = resolve_strategy_agent_session_id(
            strategy_id=strategy_id,
            session_key=task.session_key,
            policy=strategy_manifest.agent_session.policy,
        )

        ensure_session_profile(
            session_id=session_id,
            strategy_id=strategy_id,
            profile=strategy_manifest.agent_profile,
        )

        trigger_for_agent = {
            "id": event.event_id,
            "event_id": event.event_id,
            "source": "strategy",
            "kind": "strategy.agent_task",
            "strategy_id": strategy_id,
            "strategy_triggered": True,
            "payload": {
                "text": task.prompt,
                "metadata": task.metadata,
                "artifacts": task.artifacts,
                "session_id": session_id,
                "strategy_agent_task": True,
            },
        }

        return kernel.run_turn(
            trigger=trigger_for_agent,
            strategy_id=strategy_id,
            session_id=session_id,
            attached_skills=task.attached_skills or profile.attached_skills,
        )
```

集成点：

```text
nerya/triggers/runtime.py
nerya/triggers/cron.py
nerya/api/routes_triggers.py
nerya/sdk/trigger_api.py
```

## Phase 3: Session Profile 持久化

新增：

```text
nerya/agent/session_profile.py
```

或扩展 `SessionStore.meta`：

```json
{
  "profile": {
    "strategy_id": "...",
    "profile_hash": "...",
    "order_rules": [],
    "allowed_tools": [],
    "default_trade_source": "strategy_agent",
    "created_at": "...",
    "updated_at": "..."
  }
}
```

Agent system prompt 需要读取 profile 并注入：

- 当前是 strategy agent task session。
- 当前策略 ID。
- 允许工具。
- 下单规则。
- 风控规则。
- trade_intent_submit 使用要求。

注意：profile 是长期上下文，prompt 是本轮信号数据。不要把 CSV 长期写入 profile。

## Phase 4: Trading Tool Guard

新增或扩展 native tool 执行 guard：

```text
nerya/agent/trading_tool_guard.py
```

职责：

- 当前 turn 是 `strategy_agent_task` 时，给 `trade_intent_submit` 注入固定字段。
- 校验工具调用是否符合 profile。
- 自动调用或要求 Agent 先调用 `risk_check`。
- 对超过 profile 约束的参数直接拒绝。
- 所有拒绝写入 agent/tool journal。

最小实现可以先放在 `AgentKernel` / native tool executor 周边，但长期应独立成 guard，避免把交易策略规则散落在 kernel。

## Phase 5: Prompt/Artifact 管理

新增 prompt 产物记录：

```text
strategies/<id>/agent_tasks/<task_id>.prompt.md
strategies/<id>/agent_tasks/<task_id>.metadata.json
strategies/<id>/agent_tasks/<task_id>.artifacts/
```

如果 prompt 太大：

- prompt 中放摘要和最近 N 行 CSV。
- 全量 CSV 写 artifact。
- prompt 中引用 artifact path。

建议默认限制：

| 类型 | 默认 |
|---|---:|
| prompt soft cap | 24k chars |
| CSV inline rows | 120 |
| full artifact max | 按 workspace 配置 |
| session compact | 每 20 turns |

Agent 当前 journal 对 user text 有截断记录 cap，但实际 LLM 上下文仍可能被 prompt 撑爆，所以需要策略层和 runtime 层都有限制。

## Phase 6: Dashboard / API 可观测性

新增 history：

```text
strategies/<id>/history/agent_tasks.jsonl
```

字段：

```json
{
  "kind": "strategy.agent_task",
  "task_id": "...",
  "strategy_id": "...",
  "trigger_event_id": "...",
  "session_id": "...",
  "turn_id": "...",
  "status": "dispatch|skip|error",
  "prompt_artifact": "...",
  "metadata": {
    "market": "BINANCE:BTCUSDT",
    "timeframe": "1m",
    "signal": "macd_golden_cross",
    "latest_macd_hist": 2.2
  },
  "actions": [],
  "tool_trace": [],
  "trade_intents": [],
  "risk_decisions": []
}
```

新增 API：

```text
GET /strategies/runtime/agent_tasks?strategy_id=...
GET /strategies/runtime/agent_task?strategy_id=...&task_id=...
GET /strategies/runtime/agent_session?strategy_id=...&session_id=...
GET /triggers/executions?strategy_id=...
```

Dashboard strategy run / task detail 展示：

- trigger event
- strategy prompt
- CSV preview
- metadata
- session profile
- Agent final answer
- tool calls
- trade intent
- risk decision
- approval/order/fill

## 7. 推荐配置形态

策略 manifest 示例：

```yaml
id: btc_macd_agent
mode: paper

trigger:
  target: skill:strategy.agent_task
  kind: schedule.strategy_agent_task

agent_session:
  policy: per_strategy_market_timeframe
  ttl_seconds: 86400
  max_turns: 500
  compact_every_turns: 20
  include_prior_messages: true
  refresh_profile_on_change: true

agent_profile:
  title: BTCUSDT 1m MACD execution agent
  attached_skills:
    - trading
    - strategy_author
  allowed_tools:
    - portfolio_summary
    - strategy_history
    - risk_check
    - trade_intent_submit
  order_rules:
    - Always inspect current position before opening a new one.
    - Never exceed 200 USDT notional for a single order.
    - Do not trade if confidence is below 0.68.
    - Prefer hold when MACD hist is positive but decreasing.
  risk_limits:
    max_single_order_usd: 200
    max_daily_notional_usd: 1000
    max_open_positions: 3
```

Trigger emit 示例：

```json
{
  "source": "schedule",
  "kind": "schedule.strategy_agent_task",
  "target": "skill:strategy.agent_task",
  "strategy_id": "btc_macd_agent",
  "payload": {
    "reason": "cron",
    "market": "BINANCE:BTCUSDT",
    "timeframe": "1m"
  }
}
```

注意：payload 只需要提供唤醒信息。K line、指标、因子不要求从 trigger payload 传入，它们由策略脚本自己获取和组装。

## 8. 验收流程

## 8.1 单元测试

```powershell
python -m pytest tests/test_strategy_agent_task_executor.py -q
python -m pytest tests/test_strategy_agent_task_prompt_contract.py -q
python -m pytest tests/test_strategy_agent_session_profile.py -q
python -m pytest tests/test_strategy_agent_trade_guard.py -q
```

通过标准：

- trigger 能启动 `skill:strategy.agent_task`。
- 策略脚本生成的 prompt 原样进入 `AgentKernel.run_turn`。
- CSV 中自定义指标和因子字段不被 runtime 丢失。
- 同一策略/市场/timeframe 复用同一个 session。
- session profile 被注入并可更新。
- Agent 交易工具调用被 strategy/session guard 约束。
- `RiskGate` / `ApprovalGate` 仍被调用。

## 8.2 HTTP 实测

启动本地 API：

```powershell
pwsh -File .\scripts\windows\start-local.ps1 -ApiOnly
```

触发策略 Agent 任务：

```powershell
$body = @{
  source = "schedule"
  kind = "schedule.strategy_agent_task"
  target = "skill:strategy.agent_task"
  strategy_id = "btc_macd_agent"
  payload = @{
    reason = "manual_probe"
    market = "BINANCE:BTCUSDT"
    timeframe = "1m"
  }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Uri http://127.0.0.1:18317/triggers/emit `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

验收：

- response 包含 `status=executed`。
- response 包含 `task_id`、`session_id`、`turn_id`。
- `workspace/sessions/<session_id>.json` 存在。
- session 的 `turn_ids` 增加。
- agent journal 里的 `user_text` 包含策略脚本生成的 CSV 字段，例如 `macd_hist`、`trend_score`。
- 如果 Agent 调用 `trade_intent_submit`，trading journal 中能看到 risk decision。

## 8.3 Session 复用验证

连续两次触发同一策略、市场、timeframe：

期望：

- 两次 `session_id` 相同。
- 第二次 turn 能看到同一 session 的 prior context。
- session profile 没有重复膨胀。
- prompt 中只包含本轮数据，不把历史 CSV 无限累积。

## 8.4 交易安全验证

构造测试 Agent 输出或 mock tool call：

```json
{
  "tool": "trade_intent_submit",
  "arguments": {
    "strategy_id": "other_strategy",
    "market": "BINANCE:BTCUSDT",
    "side": "buy",
    "size_usd": 1000000
  }
}
```

期望：

- `strategy_id` 被拒绝或覆盖为当前 strategy。
- `size_usd` 触发 profile / RiskGate 拒绝或升级审批。
- 不会绕过 ApprovalGate。
- 拒绝原因可在 Agent task detail 和 trading journal 中看到。

## 9. 文件改动清单

### P0 必改

```text
nerya/strategies/agent_task.py
nerya/strategies/prompt_io.py
nerya/strategies/context.py
nerya/triggers/strategy_agent_task_executor.py
nerya/triggers/runtime.py
nerya/triggers/cron.py
nerya/sdk/trigger_api.py
nerya/api/routes_triggers.py
tests/test_strategy_agent_task_executor.py
tests/test_strategy_agent_task_prompt_contract.py
```

### P1 Session / Profile

```text
nerya/agent/session_profile.py
nerya/agent/session.py
nerya/agent/kernel.py
nerya/strategies/package.py
tests/test_strategy_agent_session_profile.py
```

### P2 Trading Guard

```text
nerya/agent/trading_tool_guard.py
nerya/trading/submit.py
tests/test_strategy_agent_trade_guard.py
```

### P3 可观测性

```text
nerya/api/routes_strategies_runtime.py
dashboard/app/strategies/...
dashboard/lib/clientApi.ts
tests/test_routes_strategies_runtime.py
```

### 文档与示例

```text
nerya/skills/builtin/strategy_author/SKILL.md
nerya/skills/builtin/strategy_author/ref/scalping_cron.md
docs/...
```

## 10. 最小可行版本

MVP 只做以下内容：

1. 新增 `StrategyAgentTask`。
2. 新增 `skill:strategy.agent_task` executor。
3. 策略脚本可以返回 prompt 字符串。
4. runtime 把 prompt 放进 `AgentKernel.run_turn(trigger.payload.text)`。
5. 同一策略/市场/timeframe 复用稳定 `session_id`。
6. Session profile 注入基本下单规则。
7. Agent 可以调用 `trade_intent_submit`，但仍经过 `RiskGate` / `ApprovalGate`。
8. 增加一个 MACD 金叉策略 fixture，验证 CSV 自定义字段能进入 Agent prompt。

完成后，理想使用方式是：

```python
def build_agent_task(ctx):
    candles = ctx.market.candles("BINANCE:BTCUSDT", timeframe="1m", limit=120)
    rows = add_macd_rsi_custom_factors(candles)

    if not macd_golden_cross(rows):
        return StrategyAgentTask.skip("no signal")

    prompt = build_my_strategy_prompt(rows)

    return StrategyAgentTask.dispatch(
        prompt=prompt,
        session_key={
            "market": "BINANCE:BTCUSDT",
            "timeframe": "1m",
        },
        metadata={
            "signal": "macd_golden_cross",
            "latest_hist": rows[-1]["macd_hist"],
        },
    )
```

Agent Session 收到 prompt 后，按该 Session 的 profile 复用下单规则和工具权限，自行完成分析、风险检查和受控交易提交。

这才是更优雅的链路：策略脚本负责“数据和信号表达”，Agent Session 负责“任务执行和交易决策”，runtime 负责“调度、权限、风控和审计”。

## 11. 当前实现状态

已完成 MVP 到可验收闭环：

- `StrategyAgentTask` 契约已落地，支持 `dispatch` / `skip` / `error`。
- 策略脚本可通过 `ctx.prompt.csv()` / `markdown_table()` / `json_block()` / `artifact()` 自行组装最终 prompt。
- `ctx.trigger` 已暴露 trigger envelope；runtime 不再替策略猜测 K line / indicator / factor 字段语义。
- `skill:strategy.agent_task` 已有执行层，route 成功后会加载策略包、运行 prompt builder、解析稳定 session、写 prompt artifact，并调用 `AgentKernel.run_turn(payload.text=prompt)`。
- session id 默认按 `strategy_id + session_key + agent_session.policy` 生成，同一策略/市场/timeframe 会复用同一个 Agent Session。
- session profile 已持久化到 `workspace/sessions/<session_id>.json::meta.strategy_agent_profile`，并在 Agent system prompt 中注入长期规则、allowed tools 和 risk limits。
- `trade_intent_submit` native wrapper 已在策略触发 Agent turn 中强制绑定当前 `strategy_id`、`source=strategy_agent`、`trigger_event_id`、`agent_session_id`，并按 profile 预校验工具、账户、市场、confidence 和 `max_single_order_usd`。
- 交易仍走 `TradeIntent -> RiskGate -> ApprovalGate -> ExecutionEngine / VirtualLedger`；没有新增绕过风控的路径。
- agent task 记录已写入 `strategies/<id>/history/agent_tasks.jsonl` 和 `journals/strategy_agent_tasks.jsonl`，prompt artifact 写入 `strategies/<id>/agent_tasks/<task_id>/prompt.md`。
- `StrategyAPI.history()` 已包含 `agent_tasks` ledger；新增 `StrategyAPI.agent_tasks()` / `agent_task()` 读取任务、prompt artifact 和 session profile。
- HTTP runtime routes 已新增 `GET /strategies/runtime/agent_tasks` 与 `GET /strategies/runtime/agent_task`。

本轮已跑验收：

```powershell
python -m pytest tests/test_strategy_agent_task_chain.py -q
python -m pytest tests/test_strategy_order_auto_approval.py -q
python -m compileall nerya tests/test_strategy_agent_task_chain.py -q
python -m ruff check <changed python files>
python -m pytest -q
python -m pytest -m "" -q
```

结果：

```text
tests/test_strategy_agent_task_chain.py: 4 passed
tests/test_strategy_order_auto_approval.py: 5 passed
compileall: passed
ruff changed files: passed
smoke pytest: 51 passed
full local pytest with empty marker: 51 passed
```
