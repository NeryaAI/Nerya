# Trading SDK

The Trading SDK lets **approved** scripts and external callers submit a
`TradeIntent` without going through the agent loop. The SDK is not a
bypass — it merely moves the decision *before* the trading kernel, which
still runs every Risk Gate and Approval Gate check.

## Flow

```
script  ─▶  Trading SDK (client.trading.submit_intent)
                   │ workspace/inbox/sdk_orders/*.json   (file mode)
                   │ POST /trading/intent                (local HTTP)
                   ▼
            trading_skill.submit_trade_intent
                   │ schema validation
                   ▼
                Risk Gate
                   │ approve / reject / escalate
                   ▼
              Approval Gate
                   │ pending.jsonl or inline approval
                   ▼
       Paper Execution  (default)
    OR Live Execution  (gated by live_trading_enabled + signer policy)
                   ▼
        Strategy History write
                   ▼
          Message Skill emit
                   ▼
   Optional Strategy Review (async)
```

## Core types

```python
@dataclass
class TradeIntent:
    intent_id: str
    strategy_id: str
    account_id: str
    market: str                # "BINANCE:BTCUSDT"
    side: Literal["buy", "sell"]
    size: Decimal
    size_unit: Literal["base", "quote", "usd"]
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    limit_price: Decimal | None
    stop_price: Decimal | None
    time_in_force: Literal["gtc", "ioc", "fok", "post_only"]
    confidence: float          # 0..1 — used by Risk Gate
    reasoning: str             # human text, redacted in messages
    source: Literal["agent", "subagent", "script", "cron"]
    trigger_event_id: str | None
```

```python
@dataclass
class RiskDecision:
    intent_id: str
    decision: Literal["allow", "reject", "escalate"]
    reasons: list[str]
    limits_snapshot: dict
    virtual_ledger_snapshot: dict
```

```python
@dataclass
class OrderRequest: ...
@dataclass
class OrderResult: ...
@dataclass
class PaperExecution: ...
```

See `nerya/trading/intents.py` and `nerya/trading/orders.py` for the
concrete Pydantic models.

## What the SDK refuses to do

- Submit to an unknown `account_id`.
- Target a `market` the strategy is not allowed on (matched against `limits.yml`).
- Use `size > max_single_order` (rejected before even reaching Risk Gate).
- Bypass the Risk Gate via a side-channel flag — there is no such flag.
- Hit the exchange private API. Only connectors can, and connectors only
  run inside the trading kernel.

## Demo

`sdk/python/examples/direct_order_strategy.py` submits an intent without
ever invoking the LLM, and still ends up journalled in
`strategies/btc_grid_script/history/*.jsonl` with a Risk Gate verdict, a
paper fill and a post-execution review.
