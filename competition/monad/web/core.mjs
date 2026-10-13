// Public Monad/Perpl observations only. No accounts, signing or order endpoints.
export const CONTEXT_URL = 'https://app.perpl.xyz/api/v1/pub/context';
export const RPC_URL = 'https://rpc.monad.xyz';

async function request(url, options = {}) {
  const response = await fetch(url, {...options, signal: AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error(`Public source unavailable (${response.status})`);
  return response.json();
}

export async function snapshot(symbol = 'MON') {
  if (!['MON', 'BTC', 'ETH'].includes(symbol)) throw new Error('Unsupported market');
  const [context, identity] = await Promise.all([
    request(CONTEXT_URL),
    request(RPC_URL, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({jsonrpc: '2.0', id: 1, method: 'eth_chainId', params: []})}),
  ]);
  if (context.chain?.chain_id !== 143 || Number.parseInt(identity.result, 16) !== 143) throw new Error('Monad mainnet identity mismatch');
  const market = context.markets.find(row => row.name === symbol);
  if (!market || !market.config?.is_open || !market.state?.at?.t) throw new Error('Market is closed or missing a timestamp');
  const now = Date.now(), until = Math.floor(now / 3600000) * 3600000;
  const candles = await request(`https://app.perpl.xyz/api/v1/market-data/${market.id}/candles/3600/${until - 72 * 3600000}-${until - 1}`);
  const decimals = market.config.price_decimals;
  const price = value => value / 10 ** decimals;
  const close = (candles.d || []).filter(row => row.t < until).map(row => ({time: row.t, open: price(row.o), high: price(row.h), low: price(row.l), close: price(row.c), trades: row.n}));
  const bid = price(market.state.bid), ask = price(market.state.ask), mark = price(market.state.mrk);
  if (![bid, ask, mark].every(value => Number.isFinite(value) && value > 0) || ask < bid) throw new Error('Invalid market prices');
  return {
    chainId: 143, network: 'Monad mainnet', venue: 'Perpl', symbol, marketId: market.id,
    fetchedAt: new Date(now).toISOString(), observedAt: new Date(market.state.at.t).toISOString(),
    blockNumber: market.state.at.b, sourceAgeSeconds: (now - market.state.at.t) / 1000,
    mark, bid, ask, spreadBps: (ask - bid) / mark * 10000,
    makerFeeBps: market.config.maker_fee / 100, takerFeeBps: market.config.taker_fee / 100,
    fundingRaw: market.funding, protocolLimitsRaw: {initialMargin: market.config.initial_margin, maintenanceMargin: market.config.maintenance_margin},
    closedHourlyCandles: close, latestClosedHour: close.at(-1)?.time || null,
    provenance: {context: CONTEXT_URL, rpc: RPC_URL, priceDecimals: decimals, feeScale: 'Micros / 100 = basis points'},
    scope: 'Public market observation; no authenticated account or executed trade',
  };
}

export function scenario(data, inputs = {}) {
  const capital = Number(inputs.capital ?? 1000), notional = Number(inputs.notional ?? 1000);
  const adversePct = Number(inputs.adversePct ?? 5), maxLossPct = Number(inputs.maxLossPct ?? 2);
  const slippageBps = Number(inputs.slippageBps ?? 10);
  if (![capital, notional, adversePct, maxLossPct, slippageBps].every(Number.isFinite) || capital <= 0 || notional < 0 || adversePct < 0 || adversePct > 100 || maxLossPct <= 0 || maxLossPct > 100 || slippageBps < 0) throw new Error('Invalid scenario inputs');
  if (!Number.isFinite(data.sourceAgeSeconds) || !Number.isFinite(data.takerFeeBps) || data.takerFeeBps < 0) throw new Error('Invalid source');
  const lossBudget = capital * maxLossPct / 100;
  const moveLoss = notional * adversePct / 100;
  const roundTripCosts = notional * (2 * data.takerFeeBps + 2 * slippageBps) / 10000;
  const totalLoss = moveLoss + roundTripCosts;
  const reasons = [];
  if (data.sourceAgeSeconds > 120 || data.sourceAgeSeconds < -10) reasons.push('Market observation is stale');
  if (totalLoss > lossBudget) reasons.push('Stress loss exceeds the operator loss budget');
  const unitLoss = adversePct / 100 + (2 * data.takerFeeBps + 2 * slippageBps) / 10000;
  return {inputs: {capital, notional, adversePct, maxLossPct, slippageBps}, lossBudget, moveLoss, roundTripCosts, totalLoss,
    maxNotionalForBudget: unitLoss > 0 ? lossBudget / unitLoss : null,
    status: reasons.length ? 'Review required' : 'Within scenario budget', reasons,
    assumptions: 'Hypothetical position, flat account, two taker fees and assumed slippage. Funding, liquidation, gas, and actual fills are excluded. This calculation does not authorize an order.'};
}
