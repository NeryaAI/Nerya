# Notes: Live Trading Account Management Refactor

## Sources

### Nerya current implementation
- `Nerya/AGENTS.md`: Nerya is skill-first; external exchange calls must go through Skills/scripts; live trading requires both `runtime.live_trading_enabled=true` and Approval Gate; do not bypass `RiskGate`/`ApprovalGate`; CEX venues must use `CcxtConnector`; agent-authored changes are `PatchProposal`s.
- `Nerya/nerya/trading/accounts.py`: account registry is a static `accounts.yml` roster with `id`, `exchange`, `mode`, `live_trading_enabled`, `initial_balance_usd`, `status`, `venue`, and `kind`. It is not yet a live account snapshot/permission/capital allocation model.
- `Nerya/nerya/trading/submit.py`: canonical path is `TradeIntent -> RiskGate -> ApprovalGate -> ExecutionEngine -> order/fill journals`. Good single entrypoint, but it returns one-shot order results rather than managing order/executor state over time.
- `Nerya/nerya/trading/risk.py`: risk checks cover kill switch, live flag, strategy/account status, market allowlist, per-order cap, total exposure from virtual ledger, paper cash, confidence, stale data, duplicate intents, and approval threshold.
- `Nerya/nerya/trading/execution.py`: live execution calls connector `place_order` and maps the immediate `OrderAck`; paper execution applies deterministic fills to `VirtualLedger`.
- `Nerya/nerya/trading/virtual_ledger.py`: paper ledger keeps `cash_usd`, positions, fees, trade count, and weighted-average position math in JSON per account.
- `Nerya/nerya/trading/reconciliation.py`: reconciles strategy fills journal against virtual ledger only; it does not compare with exchange balances/open orders/positions.
- `Nerya/nerya/connectors/ccxt_adapter.py`: public ticker/book/klines, private balances/place/cancel/fetch order exist behind `live=True` and credentials. No durable order tracker or streaming updates yet.
- `Nerya/nerya/db/migrations.py`: SQLite already has versioned migrations; new trading tables should extend this registry rather than adding ad hoc JSON-only state.

### Hummingbot reference patterns
- Local clone: `hummingbot` at commit `574e316b1`.
- `hummingbot/strategy_v2/controllers/controller_base.py`: Strategy V2 separates controller config/control loops from execution. Controllers emit `ExecutorAction`s, expose total quote budget, manual kill switch, initial positions, executor filtering, `buy`/`sell`, `cancel`, open orders, and open positions helpers.
- `hummingbot/strategy_v2/executors/executor_orchestrator.py`: `ExecutorOrchestrator` owns active executors, executor persistence, held positions, cached performance, stop/store lifecycle, and per-controller performance reports.
- `hummingbot/strategy_v2/executors/position_executor/data_types.py`: `TripleBarrierConfig` models `stop_loss`, `take_profit`, `time_limit`, `trailing_stop`, and order types for open/TP/SL/time-limit exits.
- `hummingbot/strategy_v2/executors/position_executor/position_executor.py`: `PositionExecutor` is a finite control loop for open order, close order, take-profit limit order, retry/failure, early stop, position hold, stop-loss, time-limit, take-profit, and trailing-stop exits.
- `hummingbot/connector/budget_checker.py` and `hummingbot/core/data_type/order_candidate.py`: sizing is not direct; `OrderCandidate` describes collateral, fees, returns, and resize state, while `BudgetChecker` adjusts and locks hypothetical collateral before orders are submitted.
- `hummingbot/connector/client_order_tracker.py`: active/cached/lost order tracking, order status updates, trade fill updates, not-found/lost-order handling, and event emission are first-class connector concerns.
- `hummingbot/connector/perpetual_trading.py`: perpetual state tracks positions, leverage, position mode, funding info, and per-pair collateral semantics separately from spot balances.
- `hummingbot/client/performance.py`: performance metrics distinguish trade PnL, fees, hold value, current value, return %, derivative position pairing, and fee conversion.

### QuantDinger reference patterns
- Local clone: `QuantDinger` at commit `2e59568`.
- `QuantDinger/README.md`: QuantDinger is a self-hosted AI quant platform covering research, Python strategies, backtesting, live execution, portfolio/notification operations, crypto/IBKR/MT5 workflows.
- `QuantDinger/docs/STRATEGY_DEV_GUIDE.md`: clear authoring split: `IndicatorStrategy` for dataframe signals and engine-managed risk defaults, `ScriptStrategy` for runtime state, `ctx.buy()`, `ctx.sell()`, `ctx.close_position()`, dynamic exits, partial exits, scale-in/out.
- `QuantDinger/backend_api_python/app/services/trading_executor.py`: runtime computes strategy equity from initial capital + realized PnL + unrealized PnL, performs position sizing, max-position/daily-loss guards, signal state-machine filtering, and inserts pending order rows rather than placing directly.
- `QuantDinger/backend_api_python/app/services/pending_order_worker.py`: background worker claims `pending_orders`, reclaims stale processing rows, dispatches signal/live execution, updates local positions, and best-effort syncs positions from exchanges to remove ghost positions.
- `QuantDinger/backend_api_python/app/services/live_trading/execution.py`: normalizes strategy signals into side/position side/reduce-only semantics and routes to exchange/broker clients.
- `QuantDinger/backend_api_python/app/services/live_trading/records.py`: local DB position/trade snapshot is explicitly not source of truth; it normalizes symbols, applies fills, computes close PnL, and updates/deletes local positions.
- `QuantDinger/docs/IBKR_TRADING_GUIDE_EN.md` and `docs/MT5_TRADING_GUIDE_EN.md`: the intended flow is `Strategy Signal -> Pending Order Queue -> Broker Execution -> Position Update`.
- Anti-pattern for Nerya: QuantDinger has many native exchange clients and a very large worker branch; Nerya should adapt the queue/worker/reconciliation concept while preserving `CcxtConnector` and Skill-first boundaries.

## Synthesized Findings

### What Nerya already has
- A good safety spine: one submission entrypoint, RiskGate, ApprovalGate, live flag, kill switch, account/strategy status checks, and CCXT-based CEX policy.
- A basic paper ledger and portfolio read model.
- A connector surface that can place/cancel/fetch live orders, but it is not yet an order lifecycle system.

### Main gaps for real capital
- No durable account snapshot model: balances, margin, available/locked collateral, leverage, venue capability, and account health are not persisted as first-class state.
- No capital reservation/budget checker: risk checks use virtual ledger exposure and paper cash, but do not pre-reserve collateral/fees or resize candidate orders.
- No live position book: paper positions are JSON weighted-average snapshots; live positions are not normalized, reconciled, or treated as source-of-truth snapshots.
- No order/executor state machine: immediate ack is not enough for partial fills, cancel/replace, retries, lost orders, stale opens, TP/SL protection, or crash recovery.
- No protection-rule engine: `stop_price` exists on `TradeIntent`, but stop-loss/take-profit/trailing/time-limit are not modeled as durable position protection policies.
- Backtest/paper/live parity is incomplete: backtest has its own simulation logic; live submission has a separate one-shot path.

### Direction
- Keep Nerya's agent/skill/risk/approval constraints. Rebuild the trading core as an account-aware execution control plane: signal/proposal -> risk sizing -> capital reservation -> order executor -> fill ledger -> position book -> reconciliation -> portfolio risk monitor.

## Verification

- Confirmed local reference commits:
  - `hummingbot`: `574e316b1`
  - `QuantDinger`: `2e59568`
- Confirmed Nerya code anchors exist:
  - `nerya/trading/risk.py:36` `RiskGate`
  - `nerya/trading/approval.py:33` `ApprovalGate`
  - `nerya/trading/execution.py:36` `ExecutionEngine`
  - `nerya/trading/intents.py:14` `TradeIntent`
  - `nerya/connectors/ccxt_adapter.py:62` `CcxtConnector`
  - `nerya/trading/virtual_ledger.py:31` `VirtualLedger`
  - `nerya/trading/reconciliation.py:52` `reconcile_strategy`
- Confirmed Hummingbot code anchors exist:
  - `hummingbot/connector/budget_checker.py:13` `BudgetChecker`
  - `hummingbot/core/data_type/order_candidate.py:17` `OrderCandidate`
  - `hummingbot/connector/client_order_tracker.py:32` `ClientOrderTracker`
  - `hummingbot/strategy_v2/executors/executor_orchestrator.py:136` `ExecutorOrchestrator`
  - `hummingbot/strategy_v2/executors/position_executor/data_types.py:17` `TripleBarrierConfig`
  - `hummingbot/strategy_v2/executors/position_executor/position_executor.py:26` `PositionExecutor`
- Confirmed QuantDinger code/doc anchors exist:
  - `backend_api_python/app/services/trading_executor.py:37` `TradingExecutor`
  - `backend_api_python/app/services/pending_order_worker.py:52` `PendingOrderWorker`
  - `backend_api_python/app/services/live_trading/records.py:5` local DB snapshot is not the exchange source of truth
  - `docs/IBKR_TRADING_GUIDE_EN.md:61` and `docs/MT5_TRADING_GUIDE_EN.md:70` describe signal -> pending order queue -> broker execution -> position update
- Confirmed current external references:
  - Hummingbot Strategy V2 / Executors / Position Executor docs describe controllers emitting executor actions, self-managing executors, and triple-barrier TP/SL/time/trailing-stop management.
  - QuantDinger public repository describes a self-hosted AI quant platform connecting research, Python strategy development, backtesting, live execution, portfolio, and notifications.
