# Nerya 实盘账户与执行体系重构计划

日期: 2026-04-29

范围: 账户管理、资金管理、持仓管理、订单生命周期、止盈止损、实盘开关、对账、策略从 Agent 生成到实盘运行的晋级路径。

本计划只写设计与迁移路径，不修改运行时代码。

## 0. 结论

Nerya 当前交易内核已经有正确的安全骨架: `TradeIntent -> RiskGate -> ApprovalGate -> ExecutionEngine`，并且遵守 live flag、kill switch、账户/策略状态、CCXT 统一 CEX 连接边界。但它现在更接近“安全提交一笔意图”的系统，不是成熟实盘交易系统。

要让 Agent 生成的策略真正可用于实盘，重点不是再加更多策略模板，而是把交易核心重构成一个账户感知的执行控制面:

```text
Agent / Strategy Signal
  -> Strategy Runtime Contract
  -> Risk + Sizing Gate
  -> Capital Reservation
  -> Executor Orchestrator
  -> Order Tracker
  -> Fill Ledger
  -> Position Book
  -> Reconciliation Service
  -> Portfolio Risk Monitor
  -> Operator Approval / Kill Switch / Incident Flow
```

关键原则:

- Agent 只产出策略提案、信号或交易意图，不直接绕过 `RiskGate`、`ApprovalGate`、Skill runtime、vault、connector。
- 实盘资金以交易所/券商为事实源，本地 ledger 是审计、回放、风险和 UI 的投影。
- 下单不是一次函数调用，而是可恢复的 executor 状态机。
- 止盈止损不是 intent 上的两个字段，而是持仓保护规则和执行器生命周期。
- 回测、paper、shadow、canary、live 必须共享同一套策略信号合同和 executor 模拟器。

## 1. 参考对象与取舍

### 1.1 Hummingbot 值得借鉴的结构

本地参考: `hummingbot` commit `574e316b1`。

官方参考:

- Hummingbot Strategy V2 Architecture: https://hummingbot.org/strategies/v2-strategies/
- Hummingbot Controllers: https://hummingbot.org/strategies/v2-strategies/controllers/
- Hummingbot Executors: https://hummingbot.org/strategies/v2-strategies/executors/
- Hummingbot Position Executor: https://hummingbot.org/strategies/v2-strategies/executors/positionexecutor/

Hummingbot 的成熟点不是某个策略，而是执行结构:

| 体系 | Hummingbot 证据 | Nerya 应吸收 |
|---|---|---|
| 策略和执行分离 | `ControllerBase.determine_executor_actions()` 只发 `ExecutorAction` | Agent/策略只发标准信号或 executor action，不直接下裸单 |
| 自管理 executor | `PositionExecutor`、`DCAExecutor`、`GridExecutor` 等自带生命周期 | Nerya 增加 `ExecutorOrchestrator` 和一组有限状态 executor |
| 资金预检 | `BudgetChecker` + `OrderCandidate` 计算 collateral、fee、returns，并锁定假想资金 | Nerya 增加 capital reservation 和 budget checker |
| 订单追踪 | `ClientOrderTracker` 管理 active/cached/lost order、状态更新和 fill update | Nerya 增加 durable order tracker，不再只信 immediate ack |
| 止盈止损 | `TripleBarrierConfig` 包含 stop loss、take profit、time limit、trailing stop | Nerya 将 TP/SL 变成持仓保护规则 |
| 持仓和绩效 | `ExecutorOrchestrator` 汇总 positions held、realized/unrealized PnL、close type | Nerya position book 与 performance attribution 作为一级模型 |

不要照搬的部分:

- 不把 Hummingbot 的 connector 层直接搬进 Nerya。Nerya 本地规则要求 CEX 使用 `CcxtConnector`。
- 不把所有策略逻辑都改成 Hummingbot 语义。Nerya 需要保留 Skill-first 和 Agent proposal 语义。

### 1.2 QuantDinger 值得借鉴的结构

本地参考: `QuantDinger` commit `2e59568`。

公开主仓参考:

- QuantDinger GitHub: https://github.com/brokermr810/QuantDinger

QuantDinger 的成熟点更偏产品闭环和多市场运营:

| 体系 | QuantDinger 证据 | Nerya 应吸收 |
|---|---|---|
| 研究到实盘的一体化路径 | README 描述 Research、Build、Validate、Operate | Nerya 策略从生成到实盘需要明确 promotion pipeline |
| 策略作者模型 | `IndicatorStrategy` vs `ScriptStrategy`，后者有 `ctx.buy()` / `ctx.sell()` / `ctx.close_position()` | Nerya SDK 需要区分信号型策略和运行时持仓型策略 |
| 队列化执行 | `Strategy Signal -> Pending Order Queue -> Broker Execution -> Position Update` | Nerya 增加 pending/executor queue，避免同步下单黑箱 |
| 后台 worker | `PendingOrderWorker` 领取、执行、失败标记、stale reclaim、position sync | Nerya 增加 trading worker，但保持 CCXT/Skill-first |
| 实盘记录 | `records.py` 明确本地 DB 不是事实源，交易所才是事实源 | Nerya ledger 定义为 event/audit projection，定期对账 |
| 权益口径 | `trading_executor._calculate_current_equity()` 用初始资金 + 已实现 + 未实现 | Nerya 需要 NAV/equity/current exposure 的统一口径 |
| 多市场 broker | IBKR/MT5 docs 把 broker execution 纳入平台流程 | Nerya 后续可用 provider spec 扩展，但 CEX 仍走 CCXT |

不要照搬的部分:

- QuantDinger 有大量原生 exchange client 和一个很大的 `PendingOrderWorker` 分支。Nerya 不应该复制这个形态。
- QuantDinger 局部存在重复持仓逻辑和 best-effort DB 快照。Nerya 应从一开始建 event-sourced ledger + reconciliation contract。

## 2. Nerya 现状诊断

### 2.1 已有基础

当前 Nerya 已有这些值得保留的基础:

- `nerya/trading/submit.py`: 唯一提交入口，集中 `TradeIntent -> RiskGate -> ApprovalGate -> ExecutionEngine -> journal`。
- `nerya/trading/risk.py`: kill switch、live flag、策略/账户状态、market allowlist、单笔上限、总 exposure、paper cash、confidence、staleness、dedupe、approval threshold。
- `nerya/trading/approval.py`: escalation 写入 pending approval，阻塞执行。
- `nerya/trading/accounts.py`: 静态账户 roster。
- `nerya/trading/virtual_ledger.py`: paper cash、position、fee、trade count。
- `nerya/trading/execution.py`: paper/live 路由，live 经 connector `place_order`。
- `nerya/connectors/ccxt_adapter.py`: CCXT ticker/order book/klines/balances/place/cancel/fetch order。
- `nerya/trading/reconciliation.py`: fills journal 与 virtual ledger 的本地对账。
- `nerya/db/migrations.py`: SQLite versioned migration 机制。

这些说明 Nerya 不是从零开始，应该做的是把现有安全主干扩展成实盘控制面，而不是推翻重写。

### 2.2 关键缺口

| 缺口 | 当前表现 | 实盘风险 |
|---|---|---|
| 账户模型过薄 | `Account` 只有模式、初始资金、状态、venue/kind/raw | 无法表达余额、可用资金、锁定资金、margin、leverage、subaccount、权限、健康状态 |
| 资金管理缺少 reservation | RiskGate 按 ledger exposure 和 paper cash 判断 | 多策略并发时会重复使用同一资金，无法处理手续费、保证金、挂单占用 |
| 订单不是状态机 | live path 只拿 `OrderAck` 生成一次 `OrderResult` | partial fill、cancel replace、lost order、pending open、超时、重试都不可恢复 |
| live position book 缺失 | paper ledger 有 position，live 没有统一 position snapshot | UI 和风控可能与交易所真实仓位不一致 |
| 对账范围不足 | 只对 fills journal vs virtual ledger | 不能发现交易所手动平仓、外部订单、ghost position、漏记 fill |
| TP/SL 不成体系 | `TradeIntent.stop_price` 存在，但没有 protection executor | 无法保证每个实盘持仓都有保护规则和可审计触发路径 |
| 策略运行和执行耦合不清 | backtest、paper、live 各自有不同逻辑 | Agent 回测通过不代表 live 行为一致 |
| 操作台不可完全验收 | portfolio/orders 页面展示已有，但缺少健康、风险、保护、对账视图 | 操作员无法判断系统是否可以放真资金 |

## 3. 目标架构

### 3.1 新的 Trading Control Plane

建议在 `nerya/trading/` 下引入明确分层:

```text
nerya/trading/
  accounts.py              # 保留 AccountProfile 读取，扩展能力与权限
  account_snapshots.py     # 新增: live/paper balance, margin, health snapshot
  capital.py               # 新增: budget checker, reservation, sizing
  order_intents.py         # 新增: 标准化订单计划/候选/保护意图
  order_tracker.py         # 新增: durable active/cached/lost order tracker
  executors/
    base.py                # 新增: executor 状态机接口
    orchestrator.py        # 新增: 创建/停止/恢复/store executor
    market_order.py        # 新增: 简单市价 executor
    limit_chaser.py        # 新增: 限价追单 executor
    position_protection.py # 新增: TP/SL/trailing/time-limit executor
    rebalance.py           # 后续: 组合调仓 executor
  positions.py             # 从 thin wrapper 升级为 position book
  ledger.py                # 新增: event-sourced fill/order ledger
  reconciliation.py        # 升级: 本地投影 vs 交易所事实源
  portfolio_risk.py        # 新增: NAV/exposure/drawdown/risk attribution
```

不要让策略脚本直接 import connector。策略脚本和 Agent 通过 SDK/Skill 调用:

```text
ctx.trading.signal(...)
ctx.trading.open_position(...)
ctx.trading.close_position(...)
ctx.trading.attach_protection(...)
ctx.trading.cancel_executor(...)
```

这些 SDK 方法只产生标准 intent/action，再进入同一条风控与执行链。

### 3.2 核心对象模型

#### AccountProfile

替代当前薄 `Account` 的目标字段:

```yaml
id: binance_main
mode: paper | shadow | canary | live
venue: binance
kind: cex
provider_spec: binance
base_currency: USDT
subaccount: ""
status: active | read_only | disabled | quarantined
live_trading_enabled: false
permissions:
  read_balances: true
  place_order: false
  cancel_order: false
  withdraw: false
limits:
  max_account_nav_usd: 100000
  max_strategy_allocation_pct: 0.25
  max_order_notional_usd: 500
  max_daily_loss_usd: 250
  max_drawdown_pct: 0.05
  max_leverage: 2
credentials:
  api_key: vault://...
  secret: vault://...
```

说明:

- `mode=shadow`: 读真实账户、模拟执行，不下单。
- `mode=canary`: 小额度真单，强制更严 approval。
- `mode=live`: 正式实盘，但仍必须满足 runtime live flag 和 approval/risk。
- `permissions.withdraw` 固定不支持，Nerya 不应接触提现权限。

#### AccountSnapshot

周期性从 connector 获取并持久化:

- `account_id`
- `ts`
- `source`: exchange / paper / mock
- `nav_usd`
- `cash_by_asset`
- `free_by_asset`
- `locked_by_asset`
- `margin_used_usd`
- `unrealized_pnl_usd`
- `open_order_notional_usd`
- `health`: ok / degraded / stale / auth_error / rate_limited
- `latency_ms`
- `raw_ref`: redacted artifact pointer，不存明文 secret

#### CapitalReservation

资金预留是 Nerya 缺的关键层:

- `reservation_id`
- `account_id`
- `strategy_id`
- `intent_id`
- `executor_id`
- `market`
- `side`
- `notional_usd`
- `estimated_fee_usd`
- `estimated_margin_usd`
- `state`: proposed / reserved / consumed / released / expired / rejected
- `expires_at`
- `risk_decision_id`

RiskGate 之后、Executor 创建之前必须 reserve。失败或 cancel 要 release。fill 完成后 consumed。

#### PositionBook

目标不是仅有 `market -> size/avg_price`，而是可表达:

- account / strategy / market / venue
- spot/perp，one-way/hedge mode
- side: long / short
- size_base
- avg_entry_price
- mark_price
- liquidation_price
- realized_pnl_usd
- unrealized_pnl_usd
- fees_usd
- funding_usd
- opened_at / updated_at
- source: exchange / paper / reconciled / manual_import
- attached protection rules
- owning executor ids

#### OrderTracker

Hummingbot 的 `ClientOrderTracker` 思路应转为 Nerya 版本:

- active orders: 已提交但未终态
- cached orders: 终态后短期保留，用于迟到 fill/status update
- lost orders: 多次 fetch not found，但本地未终态
- exchange order id 和 client order id 双索引
- every status/fill/cancel/error 都落 `order_events`
- crash/restart 从 SQLite 恢复 active/lost orders

#### Executor

Executor 是 Nerya 实盘专业化的核心:

```text
created -> reserving -> ready -> submitted -> working -> closing -> done
                                   |           |          |
                                   |           |          -> failed
                                   |           -> canceling -> canceled
                                   -> rejected
```

基础 executor 类型:

- `MarketOrderExecutor`: 单笔市价或近似市价执行。
- `LimitOrderExecutor`: 限价、post-only、IOC/FOK、超时撤单。
- `LimitChaserExecutor`: 跟随 bid/ask，避免无脑 crossing spread。
- `TWAPExecutor`: 大单拆分。
- `PositionProtectionExecutor`: TP/SL/trailing/time-limit。
- `RebalanceExecutor`: 组合权重调仓。

Executor 必须持久化:

- config snapshot
- state
- last heartbeat
- retry count
- order ids
- reservation ids
- close type
- realized/unrealized result
- operator action history

## 4. 资金管理体系

### 4.1 BudgetChecker 目标

参考 Hummingbot `BudgetChecker`，Nerya 应增加:

```python
class OrderCandidate:
    account_id: str
    strategy_id: str
    market: str
    side: str
    order_type: str
    size_base: Decimal | None
    notional_usd: Decimal
    price: Decimal | None
    leverage: Decimal
    reduce_only: bool
    estimated_fee_usd: Decimal
    required_collateral: dict[str, Decimal]
    expected_returns: dict[str, Decimal]
    resized: bool
    resize_reason: str | None
```

BudgetChecker 输出:

- `allow`: 原样通过
- `resize`: 根据资金、最小下单量、手续费、保证金、安全缓冲缩小
- `reject`: 不足资金、超限、违反策略或账户政策
- `escalate`: 超审批阈值或首次 live/canary

### 4.2 SizingPolicy

策略不能随意把金额解释成不同单位。统一支持:

- `fixed_usd`: 固定 USDT/USD 名义金额。
- `pct_nav`: 占当前 NAV 百分比。
- `risk_to_stop`: 按止损距离反推仓位，例如最多亏 NAV 0.5%。
- `volatility_target`: 按 ATR/波动率调整仓位。
- `target_weight`: 组合权重调仓。
- `reduce_pct`: 减仓当前仓位比例。
- `close_all`: 明确全平。

所有 sizing 都要输出同一种 `OrderCandidate`，由 BudgetChecker 再裁剪和量化。

### 4.3 多策略资金隔离

新增 `StrategyAllocation`:

- account-level allocation cap
- strategy-level allocation cap
- per-market concentration cap
- gross exposure / net exposure
- realized daily loss
- max drawdown
- open order reservation

同一个账户跑多个 Agent 策略时，必须先做 allocation，再做 reservation，避免重复占用同一余额。

## 5. 持仓管理体系

### 5.1 Position lifecycle

统一持仓事件:

- `position.opened`
- `position.increased`
- `position.reduced`
- `position.closed`
- `position.reversed`
- `position.protection_attached`
- `position.protection_triggered`
- `position.reconciled`
- `position.external_change_detected`

每次 fill 进入 `FillLedger` 后，PositionBook 通过事件投影更新，不能让多个模块各自算一遍平均价格。

### 5.2 交易所仓位为事实源

本地状态分三层:

| 层 | 用途 | 事实级别 |
|---|---|---|
| `ledger_events` | 审计、回放、归因 | 本地不可变事实 |
| `position_book` | UI、风控、策略上下文 | 本地投影 |
| `account_snapshots` / exchange fetch | 余额、仓位、open orders | 实盘事实源 |

对账发现交易所与本地不一致时:

- 轻微差异: 写 reconcile report，更新 mark/fee/funding。
- 本地有仓位但交易所已平: 标记 `external_closed`，触发策略上下文刷新。
- 交易所有仓位但本地没有: 标记 `external_position_detected`，进入 attach/import approval。
- open order 丢失: 标记 lost order，按策略 policy 决定 cancel/recover/escalate。

## 6. 下单与订单生命周期

### 6.1 不再以 immediate ack 为完成

当前 `_execute_live()` 拿到 `OrderAck` 后直接返回 `OrderResult`。新体系中:

- `place_order` 只代表 submit 成功，不代表成交完成。
- `fetch_order`/user stream/轮询更新才决定状态。
- partial fill 必须立即写 fill event。
- cancel request 与 cancel confirmed 是两个状态。
- exchange not found 不能立刻当取消，必须进入 lost order 策略。

### 6.2 标准状态

订单状态建议:

- `created`
- `reserved`
- `submitted`
- `accepted`
- `open`
- `partially_filled`
- `filled`
- `cancel_requested`
- `canceled`
- `rejected`
- `expired`
- `lost`
- `failed`

Executor close type:

- `take_profit`
- `stop_loss`
- `trailing_stop`
- `time_limit`
- `manual_cancel`
- `operator_flatten`
- `risk_kill_switch`
- `insufficient_balance`
- `lost_order_recovered`
- `external_position_change`
- `failed`

### 6.3 Client order id

所有订单必须使用稳定 `client_order_id`:

```text
nerya:{strategy_id}:{executor_id}:{leg}:{seq}
```

好处:

- 重试幂等。
- 对账可从交易所回查。
- 日志、订单、fill、approval 可关联。

## 7. 止盈止损和保护规则

### 7.1 ProtectionRule

新增模型:

```yaml
protection_id: prot_...
position_id: pos_...
executor_id: exec_...
mode: hard_exchange | soft_runtime | hybrid
stop_loss:
  type: pct | price | atr | pnl_usd
  value: 0.02
take_profit:
  type: pct | price | r_multiple | pnl_usd
  value: 0.04
time_limit_sec: 3600
trailing_stop:
  activation_pct: 0.02
  trail_pct: 0.01
partial_exits:
  - trigger_pct: 0.03
    close_pct: 0.5
```

### 7.2 hard 与 soft

- `hard_exchange`: 交易所原生 stop/oco/tp/sl 支持时优先放交易所，系统 crash 时仍有保护。
- `soft_runtime`: 交易所不支持或 CCXT 不统一时，由 Nerya runtime 监控并触发 close executor。
- `hybrid`: 交易所放硬止损，Nerya 负责 trailing、time limit、partial take-profit。

必须记录:

- 保护规则创建时间。
- 是否已成功放置交易所保护订单。
- 当前保护状态。
- 触发依据: mark price、bid/ask、last、candle close。
- 触发后创建的 close executor。

### 7.3 Agent 策略约束

Agent 生成策略要进入实盘，必须满足:

- 每个 open intent 必须有 exit plan，除非 operator 明确批准裸仓。
- `risk_to_stop` sizing 必须能找到 stop price，否则降级为 fixed max notional 或 reject。
- `ctx.close_position()` 是明确全平语义，不能依赖 `sell`/`buy` 的隐式反向解释。
- TP/SL 参数要被 schema 验证，不允许自然语言自由字段直接进入执行。

## 8. 策略从 Agent 到实盘的晋级路径

### 8.1 Promotion pipeline

```text
draft
  -> static_review
  -> deterministic_backtest
  -> paper_executor_replay
  -> shadow_live_market_data
  -> canary_live_capital
  -> live
```

每一级 gate:

| 阶段 | 必须通过 |
|---|---|
| `draft` | 通过 structured output schema，生成 `PatchProposal`，不得直接写 workspace |
| `static_review` | 禁止 direct connector import、secret access、network/file/subprocess 越权 |
| `deterministic_backtest` | 使用统一 signal contract，输出 trades/equity/drawdown |
| `paper_executor_replay` | 用真实 executor 模拟订单、partial fill、TP/SL、fees、slippage |
| `shadow_live_market_data` | 连接真实行情和账户快照，但不下单，比较信号与风险决策 |
| `canary_live_capital` | 小额度真单，强制每笔 approval 和更低限额 |
| `live` | 可自动执行，但保留 kill switch、risk/approval、对账、incident policy |

### 8.2 Agent 输出合同

Agent 不能输出“现在买 BTC 100U”这种自由文本直达交易。目标 schema:

```json
{
  "action": "open_position",
  "strategy_id": "btc_momentum",
  "account_id": "paper_main",
  "market": "BINANCE:BTCUSDT",
  "side": "long",
  "sizing": {
    "method": "risk_to_stop",
    "risk_pct_nav": 0.005
  },
  "entry": {
    "order_type": "market",
    "max_slippage_bps": 15
  },
  "protection": {
    "stop_loss": {"type": "pct", "value": 0.02},
    "take_profit": {"type": "pct", "value": 0.04},
    "time_limit_sec": 14400
  },
  "confidence": 0.72,
  "reasoning_ref": "artifact://..."
}
```

然后由 Nerya:

1. 解析为 `TradePlan`。
2. 通过 `RiskGate.preview()`.
3. 通过 `BudgetChecker.adjust()`.
4. 创建 capital reservation。
5. 创建 executor。
6. 写入 approval 或执行队列。

## 9. API 与 Dashboard 目标

### 9.1 后端 API

建议新增或扩展:

- `GET /accounts`: account profiles + health。
- `GET /accounts/{id}/snapshot`: balances, margin, permissions, stale status。
- `GET /portfolio/health`: NAV、cash、exposure、drawdown、risk alerts。
- `GET /portfolio/positions`: exchange/paper/reconciled position book。
- `GET /orders`: active/cached/lost/filterable orders。
- `GET /executors`: active/done/failed executors。
- `POST /executors/{id}/cancel`: 取消 executor，不直接裸 cancel order。
- `POST /positions/{id}/flatten`: 创建受控 close executor。
- `POST /positions/{id}/protection`: attach/update protection rule，过 RiskGate/ApprovalGate。
- `GET /reconciliation/reports`: 对账报告。
- `POST /reconciliation/run`: 手动触发对账。
- `POST /strategies/{id}/promote`: draft -> paper/shadow/canary/live 晋级，写 approval。

### 9.2 Dashboard

Portfolio 页面不能只显示 raw positions，需要升级成操作员面板:

- Account health: live flag、account status、connector auth、last snapshot age。
- NAV: cash/free/locked/margin/equity。
- Exposure: gross/net、per market、per strategy、reserved capital。
- Drawdown: day/session/all-time。
- Positions: side、size、avg entry、mark、unrealized、realized、fees、funding、attached TP/SL。
- Open orders: age、state、filled、remaining、executor、cancel action。
- Executors: state、close type、heartbeat、retry、linked orders。
- Risk alerts: loss cap、stale market data、lost order、reconcile drift。
- Pending approvals: exact side effect preview。
- Kill switches: global/account/strategy/executor scoped。

## 10. 数据迁移和存储

### 10.1 用 SQLite migration 扩展

当前 `nerya/db/migrations.py` 已有 versioned migration registry。新表应走 migration，不应继续新增互不关联的 JSON 状态。

建议新增表:

- `account_snapshots`
- `balance_snapshots`
- `capital_reservations`
- `order_intents`
- `orders`
- `order_events`
- `fills`
- `executor_runs`
- `positions`
- `position_events`
- `protection_rules`
- `reconciliation_reports`
- `portfolio_equity_points`
- `risk_evaluations`
- `trading_incidents`

### 10.2 保留 JSONL 的位置

JSONL 可以继续用于:

- human-readable journal
- strategy history artifact
- debug replay
- append-only incident log

但执行事实源和恢复状态要进入 SQLite，否则 worker/executor crash recovery 很难做严谨。

## 11. 分阶段实施计划

### P0: 明确合同与验收基线

目标: 不改业务行为，先把合同写清楚。

任务:

- 新增 `docs/trading-control-plane.md`。
- 新增 `docs/live-trading-promotion-gates.md`。
- 为当前 `submit_trade_intent`、`RiskGate`、`ExecutionEngine` 写现状测试清单。
- 定义 `TradePlan`、`OrderCandidate`、`ProtectionRule` 的 Pydantic/dataclass schema。
- 定义 live/account/canary/shadow 模式语义。

验收:

- `python -m pytest tests/test_trading_submit.py tests/test_risk_gate.py -q`
- 新 schema 的 roundtrip/unit tests 通过。
- 文档明确“不绕过 RiskGate/ApprovalGate，不新增 native CEX connector”。

### P1: AccountSnapshot + BudgetChecker + CapitalReservation

目标: 先解决资金口径和并发占用问题。

任务:

- 扩展 `Account` 为 `AccountProfile`，兼容旧 `accounts.yml`。
- 新增 `account_snapshots.py`，支持 paper snapshot 和 CCXT live balance snapshot。
- 新增 `capital.py`:
  - `OrderCandidate`
  - `BudgetChecker`
  - `CapitalReservationStore`
  - `SizingPolicy`
- RiskGate 输出 `risk_evaluation_id` 和完整 notional/collateral/fee preview。

验收场景:

1. 同一账户两个策略同时想买入，各自只能使用未 reservation 的余额。
2. fee buffer 不足时 resize 或 reject。
3. live disabled 时即使 account live 也不可 place order。
4. paper account 的 snapshot 与 virtual ledger 一致。

测试:

- `python -m pytest tests/test_trading_capital.py -q`
- `python -m pytest tests/test_trading_accounts.py -q`

### P2: Durable OrderTracker + ExecutorOrchestrator

目标: 把一次性 `ExecutionEngine.execute()` 改成可恢复 executor。

任务:

- 新增 `executors/base.py`、`executors/orchestrator.py`。
- 新增 `order_tracker.py` 和 `orders` / `order_events` / `fills` tables。
- `ExecutionEngine` 保留兼容入口，但内部改为创建 executor 或 delegated execution。
- paper executor 先支持 market/limit、partial fill 模拟、cancel。
- live executor 先走 CCXT place/fetch/cancel 轮询，不做 websocket。

验收场景:

1. order accepted 但未 filled 时，系统重启后仍恢复 active order。
2. partial fill 生成多个 fill event，position book 逐步变化。
3. cancel request 与 cancel confirmed 状态分开。
4. fetch_order 连续 not found 后进入 lost order，不直接当作取消。

测试:

- `python -m pytest tests/test_order_tracker.py tests/test_executor_orchestrator.py -q`

### P3: PositionBook + ProtectionRule

目标: 把止盈止损和持仓管理做成专业核心能力。

任务:

- 将 `positions.py` 从 re-export 改为 `PositionBook`。
- 新增 `ProtectionRule` schema 和 store。
- 新增 `PositionProtectionExecutor`:
  - stop loss
  - take profit
  - time limit
  - trailing stop
  - partial exits
- paper/backtest executor simulator 支持同一套 protection logic。

验收场景:

1. 开多后自动 attach 2% SL、4% TP。
2. 价格先触 TP，则 close type 是 `take_profit`。
3. 价格先触 SL，则 close type 是 `stop_loss`。
4. 价格先上行激活 trailing，再回撤触发，则 close type 是 `trailing_stop`。
5. strategy/user 手动 flat 时，所有保护订单和 runtime rules 一起关闭。

测试:

- `python -m pytest tests/test_position_book.py tests/test_position_protection_executor.py -q`

### P4: Live Reconciliation

目标: 让 Nerya 知道真实账户现在到底是什么状态。

任务:

- `reconciliation.py` 升级为:
  - local ledger vs order/fill tables
  - local position book vs exchange positions/balances
  - active orders vs exchange open/fetch order
- 增加 `ReconciliationReport` severity:
  - `info`
  - `warning`
  - `action_required`
  - `trading_halted`
- 异常动作:
  - ghost position
  - external close
  - local missing fill
  - exchange missing order
  - stale snapshot
  - auth failure
- 默认策略: 只报告和 halt，不自动修复真实仓位，除非 operator approval。

验收场景:

1. 交易所返回仓位已平，本地仍有仓位，报告 `external_closed`。
2. 交易所有仓位，本地无仓位，报告 `external_position_detected`。
3. active order 在交易所查不到，进入 lost order。
4. snapshot 超过阈值，RiskGate 拒绝新开仓。

测试:

- `python -m pytest tests/test_trading_reconciliation.py -q`

### P5: Shadow / Canary / Live Promotion Gates

目标: Agent 策略不能直接从草稿跳实盘。

任务:

- 增加 strategy promotion state machine:
  - draft
  - static_review
  - backtested
  - paper
  - shadow
  - canary
  - live
  - paused
  - quarantined
- 每次晋级写 approval 和 evidence artifact。
- canary 强制:
  - max order notional 更小
  - 每笔 approval
  - 强制 protection rule
  - 更高频 reconciliation
- live 允许自动执行，但必须保留 account/strategy kill switch。

验收:

1. 未 backtest 的 Agent 策略不能绑定 live account。
2. 无 protection rule 的 open intent 在 canary/live 被 reject 或 escalate。
3. shadow 模式读取真实 account snapshot，但不会下单。
4. canary 单笔超过上限必须 pending approval。

测试:

- `python -m pytest tests/test_strategy_promotion.py -q`

### P6: SDK / Skill / Agent Runtime 合同升级

目标: 让 Agent 策略表达专业交易意图，而不是直接拼下单 dict。

任务:

- `nerya/sdk/trading_api.py` 增加:
  - `signal()`
  - `open_position()`
  - `close_position()`
  - `reduce_position()`
  - `attach_protection()`
  - `cancel_executor()`
  - `portfolio_snapshot()`
  - `risk_preview()`
- trading skill 的 `SKILL.md` 更新，脚本放 `scripts/`，不新增 `skill.yml`/`actions.py`。
- Agent structured output schema 增加 `TradePlan`。
- 静态检查禁止策略代码 import connector / vault / network。

验收:

1. Agent 生成策略只能产生 proposal 或 SDK intent。
2. `ctx.trading.open_position(...)` 经同一条 RiskGate/ApprovalGate。
3. `ctx.trading.close_position()` 明确全平。
4. SDK 与 direct `submit_trade_intent` 行为一致。

测试:

- `python -m pytest tests/test_trading_sdk.py tests/test_trading_skill.py -q`

### P7: Dashboard 和运维闭环

目标: 让操作员能判断系统是否可放真资金。

任务:

- Portfolio 页面升级为 account health + NAV + exposure + drawdown + positions + protections。
- Orders 页面展示 active/cached/lost、executor、age、filled/remaining、cancel/flatten。
- Strategy detail 展示 promotion state、last risk decision、last reconciliation、current allocations。
- Approvals 页面展示 side effect preview。
- 增加 incident center:
  - lost order
  - stale data
  - auth failure
  - reconcile drift
  - max loss breach
- 增加 global/account/strategy/executor kill switch UI。

验收:

- dashboard typecheck:
  - `cd Nerya/dashboard`
  - `npx tsc --noEmit`
- API smoke:
  - `GET /portfolio/health`
  - `GET /orders`
  - `GET /executors`
  - `GET /reconciliation/reports`

## 12. 最小可交付版本

不要一口气重写全部。建议第一个可交付版本只覆盖:

1. Paper + shadow + canary 模式。
2. AccountSnapshot。
3. BudgetChecker + reservation。
4. MarketOrderExecutor。
5. OrderTracker durable state。
6. PositionBook。
7. 固定止损、固定止盈、time limit。
8. 对账 report。
9. Dashboard 能看见 account health、orders、positions、protections、reconciliation。

这个版本完成后，Nerya 就可以支持:

- Agent 生成策略。
- operator approve。
- paper 回放。
- shadow 读取真实账户不下单。
- canary 小额实盘。
- 每个持仓有可审计 TP/SL。
- 任何 drift 都有 report 和 halt 机制。

## 13. 验收剧本

### 剧本 A: Paper 完整闭环

配置:

- paper account 初始资金 10000 USDT。
- 策略发出 open long BTCUSDT。
- sizing 为 `risk_to_stop`，risk 0.5% NAV，stop loss 2%，take profit 4%。

预期:

1. RiskGate 生成风险评估。
2. BudgetChecker 计算 notional，生成 reservation。
3. MarketOrderExecutor 创建 order。
4. paper fill 写入 `fills`。
5. PositionBook 出现 long position。
6. ProtectionRule attach。
7. 行情路径触发 SL 或 TP。
8. close executor 平仓。
9. realized PnL、fee、equity curve 正确。
10. reservation released/consumed，无悬挂资金。

### 剧本 B: Crash recovery

步骤:

1. 创建 limit order，保持 open。
2. kill runtime。
3. restart runtime。
4. Orchestrator 恢复 executor 和 active order。
5. fetch_order 更新状态。

预期:

- 不重复下单。
- 不丢 reservation。
- UI 展示 order age 和 executor heartbeat。

### 剧本 C: Live disabled

配置:

- account mode live。
- account `live_trading_enabled=true`。
- runtime `live_trading_enabled=false`。

预期:

- RiskGate reject。
- ExecutionEngine 不调用 connector private write。
- journal 写明 `live_trading_disabled_runtime`。

### 剧本 D: Shadow

配置:

- mode shadow。
- connector 可以读取 balances/positions。

预期:

- account snapshot 来自真实交易所。
- signal/risk/executor preview 运行。
- 不 place_order。
- dashboard 显示 hypothetical order 和真实资金快照。

### 剧本 E: External close

步骤:

1. Nerya 本地有 live position。
2. 交易所手动平仓。
3. reconciliation 运行。

预期:

- report 标记 `external_closed`。
- strategy context 下一次不再认为有仓位。
- 不自动重新开仓，除非策略重新发 signal 且通过风险。

## 14. 风险和开放问题

### 技术风险

- CCXT 各交易所 stop order / OCO / reduce-only / position mode 差异很大。第一版不要承诺所有交易所硬止盈止损，只做 hard/soft/hybrid capability。
- 没有 websocket user stream 时，轮询会增加延迟。第一版可以接受，但需要 stale/lost order policy。
- paper fill simulator 过于理想会误导 promotion。需要显式模拟 slippage、partial fill、fee、min size。
- SQLite 可以支撑本地运行，但高频订单或多账户可能需要后续换更强的 event store。

### 产品风险

- Agent 生成策略容易过度自信。promotion gate 必须要求证据，不允许一句自然语言“看起来不错”就实盘。
- UI 若只展示 PnL，不展示 protection/reconcile/health，会诱导误判安全性。
- Canary 阶段必须默认保守，不要为了“自动化”绕过审批。

### 开放问题

- Nerya 第一批 live venue 是否只支持 Binance/OKX/Bybit 这类 CCXT 能较好覆盖的 CEX。
- 是否把 perpetual hedge/one-way mode 纳入第一版，还是先只做 spot + one-way perp。
- soft runtime stop 的行情源使用 last、mid、best bid/ask 还是 candle close，需要按策略类型明确。
- 是否需要支持 exchange-native OCO，还是第一版统一 soft protection。

## 15. 文件级落点

第一批建议新增/修改:

| 文件 | 动作 |
|---|---|
| `nerya/trading/accounts.py` | 扩展为 `AccountProfile`，兼容旧格式 |
| `nerya/trading/account_snapshots.py` | 新增 |
| `nerya/trading/capital.py` | 新增 BudgetChecker / reservations / sizing |
| `nerya/trading/order_intents.py` | 新增 TradePlan / OrderCandidate / ProtectionRule schema |
| `nerya/trading/order_tracker.py` | 新增 durable order tracker |
| `nerya/trading/executors/base.py` | 新增 executor 抽象 |
| `nerya/trading/executors/orchestrator.py` | 新增 orchestrator |
| `nerya/trading/executors/market_order.py` | 新增 |
| `nerya/trading/executors/position_protection.py` | 新增 |
| `nerya/trading/positions.py` | 从 wrapper 升级为 PositionBook |
| `nerya/trading/reconciliation.py` | 扩展到 exchange/live 对账 |
| `nerya/trading/submit.py` | 兼容旧入口，内部接入 TradePlan/executor |
| `nerya/trading/risk.py` | 加入 account snapshot、reservation、protection 要求 |
| `nerya/db/migrations.py` | 新增 trading control-plane tables |
| `nerya/sdk/trading_api.py` | 增加 position/executor/protection API |
| `nerya/skills/builtin/trading/SKILL.md` | 更新工作流和约束 |
| `dashboard/app/portfolio/page.tsx` | 增加 account health/positions/protections |
| `dashboard/app/orders/page.tsx` | 增加 executor/order state |

## 16. Definition of Done

这一块可以认为“够专业，可以开始小额实盘”的最低标准:

- 任意 live 下单都能追溯到 strategy、intent、risk decision、approval、reservation、executor、order、fill。
- 任意 open position 都能看到 entry、mark、PnL、fees、attached protection、owning executor。
- 任意 active order 在重启后可恢复。
- 任意 lost/stale/reconcile drift 会进入 incident/report，而不是静默忽略。
- Agent 生成策略不能直接跳过 review/backtest/paper/shadow/canary。
- 所有实盘写操作都仍通过 `runtime.live_trading_enabled`、account live flag、RiskGate、ApprovalGate。
- CEX 仍通过 `CcxtConnector`/`ExchangeProviderSpec`，不新增 native CEX connector。
- Dashboard 能给 operator 一个明确答案: 现在是否安全、哪里不安全、下一步能做什么。
