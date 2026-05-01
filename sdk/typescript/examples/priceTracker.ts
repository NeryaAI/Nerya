import { connect } from "@nerya/sdk";

async function main() {
  const client = connect({ caller: "script:price_tracker_ts" });
  console.log("routes:", (await client.triggers.listRoutes()).length);

  const tick = {
    symbol: "BTC",
    price: 82_150,
    change_pct: 0.035,
    market: "PAPER:BTCUSDT",
  };

  const dry = await client.triggers.dryRun({
    source: "script",
    kind: "price.breakout",
    payload: tick,
    target: "subagent:market_analyst",
    strategy_id: "btc_momentum",
    idempotency_key: `btc-bo-${Date.now()}`,
  });
  console.log("dry-run route:", dry);

  const res = await client.triggers.emit({
    source: "script",
    kind: "price.breakout",
    payload: tick,
    target: "subagent:market_analyst",
    strategy_id: "btc_momentum",
    idempotency_key: `btc-bo-${Date.now()}`,
  });
  console.log("emit:", res);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
