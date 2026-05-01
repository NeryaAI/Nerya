# Risk Gate

The Risk Gate is a pure function of `(TradeIntent, strategy state, account
state, limits, virtual ledger)`. It cannot be bypassed, turned off at
runtime from agent context, or negotiated with.

## Checks (in order)

1. **Kill switch** — if `risk.kill_switch == on`, immediate `reject`.
2. **Live flag** — if intent targets a live account but `live_trading_enabled=false` for that account or strategy, `reject`.
3. **Strategy status** — `paused`, `archived`, `draft` block submission.
4. **Account status** — `disabled`, `read_only` block submission.
5. **Market allow-list** — `limits.yml > allowed_markets`.
6. **Per-single-order cap** — `max_single_order_usd`.
7. **Total exposure cap** — current positions + intent notional ≤ `max_total_exposure_usd`.
8. **Virtual ledger balance** — paper mode refuses when ledger balance < required margin.
9. **Daily loss cap** — realized + unrealized day PnL vs `daily_loss_usd`.
10. **Drawdown cap** — equity curve vs `max_drawdown_pct`.
11. **Confidence floor** — `confidence ≥ min_confidence` (strategy + action specific).
12. **Slippage guard** — for market orders, reject if current mid vs `limit_hint` exceeds `max_slippage_bps`.
13. **Stale data guard** — reject if `market_snapshot.age_s > max_stale_s`.
14. **Duplicate / dedupe** — reject if an equivalent intent exists within the dedupe window.
15. **Conflicting order** — reject if an opposing live/open order exists that would cross.
16. **Approval threshold** — if intent notional ≥ `approval_threshold_usd` or breaks soft caps, `escalate`.

## Outputs

```python
RiskDecision(
    intent_id=...,
    decision="allow" | "reject" | "escalate",
    reasons=["ok"] | ["max_single_order_exceeded", "slippage_too_high"],
    limits_snapshot={...},
    virtual_ledger_snapshot={...},
)
```

Every decision — including `allow` — is journalled to
`journals/trading.jsonl` and `strategies/{id}/history/risk.jsonl`.

## Approval Gate

Downstream of the Risk Gate, `trading/approval.py`:

- On `escalate`, writes `workspace/approvals/pending.jsonl` entry with a
  hash, intent snapshot, Risk Gate reasons and expected side effects.
- Blocks execution until operator approves via CLI:
  `nerya approvals approve <approval_id>`.
- Expires after a configurable window (default 10 minutes).
- On expire, writes `rejected.jsonl` with reason `expired`.

## Operator kill switch

- `risk.kill_switch on` can only be turned on from the CLI, API (with
  admin token) or an explicit operator-signed policy.
- It can be turned off only via an approved proposal, which is itself
  journalled.
