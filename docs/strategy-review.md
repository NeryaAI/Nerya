# Strategy review

`strategy_review_skill` turns raw ledgers into human-readable reviews and
machine-readable learning proposals.

## Review stages

| Stage | When | LLM tier | Action |
|---|---|---|---|
| Immediate | Right after decision / execution | medium | `review_trade` — did the Risk Gate make sense, was the intent consistent with context? |
| Delayed | 15 m / 1 h / 4 h after entry | medium | `review_trade_after_delay` — PnL snapshot, slippage, market regime check |
| Close | When order fully closes (fill or cancel) | medium, escalating to high if `loss_usd > loss_review_threshold` or `slippage_bps > slippage_review_threshold` | `review_trade_after_delay(stage="close")` |
| Daily | 00:05 local | medium | `review_strategy_history(range="24h")` |
| Weekly | Sunday 00:15 local | **high** | `review_strategy_history(range="7d")` — candidate triggers for evolution |

## Artifacts

Every review writes both:

- `strategies/<id>/history/reviews.jsonl` — structured record
- `strategies/<id>/sessions/<sid>/review.md` — human markdown

Evolution-worthy reviews also enqueue
`evolution/proposals/<id>/proposal.yml` with a draft `learning_update`,
`prompt_patch`, `trigger_route_patch` or `strategy_config_patch`.

## Explain trade

`nerya strategy explain-trade <strategy_id> <order_id>` emits a single
markdown that walks through:

1. The `TriggerEvent` that opened the session
2. The `context_summary.md` the agent/subagent saw
3. The `decision.json` (what the agent chose)
4. The Risk Gate verdict
5. The execution path (paper/live, fills, slippage)
6. The message(s) sent
7. The review verdict

This is the primary debugging surface when something goes wrong.
