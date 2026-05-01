# @nerya/sdk

TypeScript / Node.js client for a locally-running Nerya daemon.

```bash
npm install @nerya/sdk
```

```ts
import { connect } from "@nerya/sdk";

const nerya = connect({ baseUrl: "http://127.0.0.1:8787", caller: "script:my_bot" });

// Probe a route without firing it
const dry = await nerya.triggers.dryRun({
  source: "script",
  kind: "price.breakout",
  payload: { symbol: "BTC", price: 82_000 },
  target: "subagent:market_analyst",
  strategy_id: "btc_momentum",
});
console.log(dry);   // { status: "dry_run", target, route_id, ... }

// Actually emit
await nerya.triggers.emit({
  source: "script",
  kind: "price.breakout",
  payload: { symbol: "BTC", price: 82_000 },
  target: "subagent:market_analyst",
  strategy_id: "btc_momentum",
  idempotency_key: "btc-" + Date.now(),
});

// Direct trade intent (still passes through the Risk Gate server-side)
await nerya.trading.submitIntent({
  strategy_id: "btc_momentum",
  account_id: "paper_main",
  market: "PAPER:BTCUSDT",
  side: "buy",
  size: 0.01,
  size_unit: "base",
  order_type: "market",
  confidence: 0.6,
  reasoning: "ts-sdk demo",
});
```

The SDK never accesses secrets, exchanges, or RPCs directly. Every call is
forwarded to the local Nerya daemon which enforces skill permissions,
`RiskSkill`, `ApprovalSkill`, and `StrategyHistory`.