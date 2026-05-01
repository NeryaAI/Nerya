# Strategy history

Every strategy (`workspace/strategies/<strategy_id>/`) owns two write-only
views of its past: the **history ledgers** (append-only jsonl per event
kind) and the **sessions** (one directory per complete decision cycle).

## Ledgers

`workspace/strategies/<strategy_id>/history/`:

| File | What lands here |
|---|---|
| `triggers.jsonl` | Every TriggerEvent that routed to this strategy |
| `skill_calls.jsonl` | Every skill action invoked in this strategy's context |
| `subagents.jsonl` | Subagent invocations and their outputs |
| `decisions.jsonl` | The final `decision` payload (hold, submit, cancel, escalate) |
| `intents.jsonl` | Emitted `TradeIntent`s |
| `risk.jsonl` | Risk Gate verdicts |
| `orders.jsonl` | `OrderRequest` / `OrderResult` pairs |
| `fills.jsonl` | Fill events (paper or live) |
| `pnl.jsonl` | Mark-to-market snapshots and realized PnL updates |
| `messages.jsonl` | Outbound messages |
| `reviews.jsonl` | Every review (immediate / delayed / close / daily) |

All entries carry `session_id`, `strategy_id`, `trigger_event_id` and
`ts` so the replay tool (`workspace/replay.py`) can reconstruct a full
session.

## Sessions

`workspace/strategies/<strategy_id>/sessions/<session_id>/`:

```
trigger.json
context_summary.md
market_snapshot.json
subagent_outputs.json
decision.json
trade_intent.json
risk_decision.json
execution_result.json
messages.jsonl
outcome.json
reflection.md
review.md
```

A session is *opened* when a TriggerEvent routes to this strategy, and
*closed* when the trade outcome is known (order filled, position closed,
timeout, or explicit review close). Multiple sessions can be open
concurrently.

## Where it's written

- `nerya/strategy_history/store.py` owns the ledger write.
- `nerya/strategy_history/session_writer.py` owns the session directory
  write.
- `nerya/workspace/journal.py` provides the atomic-append primitive so a
  crash mid-write cannot corrupt the jsonl.

## Read surface

- CLI: `nerya strategy history <id>` prints the ledgers in order, and
  `nerya strategy explain-trade <strategy_id> <order_id>` reconstructs a
  single trade (trigger → decision → risk → execution → message).
- API: `GET /strategy/<id>/history`, `GET /strategy/<id>/session/<sid>`.
- Skill: `trading_skill.get_strategy_history`,
  `strategy_review_skill.explain_trade`.
