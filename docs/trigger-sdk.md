# Trigger SDK

The Trigger SDK is the only way for outside code (scripts, cron, external
webhooks, user commands) to wake up Nerya. It never calls an agent loop
directly.

## Event shape

```python
TriggerEvent(
    event_id: str,                  # uuid, auto-assigned if absent
    idempotency_key: str | None,    # optional dedupe key
    source: Literal["script", "schedule", "price", "user_command", "webhook"],
    kind: str,                      # e.g. "price.breakout", "news.alpha"
    occurred_at: datetime,
    payload: dict,                  # arbitrary JSON-serialisable
    target: str,                    # see below
    strategy_id: str | None,        # if the trigger is scoped to a strategy
    dry_run: bool = False,
)
```

### `target`

- `main` — wake main agent
- `subagent:<name>` — route directly to a subagent, e.g. `subagent:market_analyst`
- `skill:<skill_id>.<action>` — skip reasoning, call a skill action directly (still passes Risk Gate / Approval Gate if the skill requires them)

Unknown targets are **dead-lettered** to `workspace/journals/errors.jsonl`
and `inbox/triggers/_dead_letter/`. They are never broadcast.

## Routes

`workspace/triggers/routes.yml`:

```yaml
version: 1
routes:
  - id: btc_breakout_to_market_analyst
    match:
      kind: price.breakout
      payload.symbol: BTC
    target: subagent:market_analyst
    strategy_id: btc_momentum
    cooldown_seconds: 60
  - id: news_alpha_to_main
    match:
      kind: news.alpha
    target: main
    cooldown_seconds: 15
```

Routes are additive. If none match, the default target inside the event is
used. If that is `main` and the operator has disabled `main` via policy,
the event is dead-lettered.

## Two transport modes

| Mode | Entry point | Use case |
|---|---|---|
| File | Drop JSON in `workspace/inbox/triggers/` | Scripts, offline jobs, tests. Used by the default demos. |
| Local HTTP | `POST /triggers/emit` on the local API | External webhooks, local CLI. Not reachable from the public internet. |

Both modes end up in the same router (`triggers/router.py`).

## Guarantees

- **Dedupe** — when `idempotency_key` is set, the router stores it in SQLite and rejects duplicates within a configurable window (default 24 h).
- **Cooldown** — per-route minimum spacing.
- **Dead letter** — unknown target / malformed payload → `_dead_letter/` + `journals/errors.jsonl`, never silently dropped.
- **Dry run** — `dry_run=True` returns the resolved target chain (route id → target → strategy → expected skill actions) without executing.

## Example (Python SDK)

```python
from nerya_sdk import NeryaClient, TriggerEvent

client = NeryaClient(workspace="~/.nerya")

event = TriggerEvent(
    source="script",
    kind="price.breakout",
    payload={"symbol": "BTC", "price": 80000, "change_24h": 6.2},
    target="subagent:market_analyst",
    strategy_id="btc_momentum",
    idempotency_key="btc-breakout-2026-04-21",
)

result = client.triggers.emit(event)
print(result.route_id, result.target, result.status)
```
