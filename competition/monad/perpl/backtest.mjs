// MON replay using the same live Monad/Perpl source as core.mjs.
import {snapshot} from './public.mjs';
import {mkdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

export function backtest(data, {capital = 1000, notional = 300, fast = 6, slow = 18, slippageBps = 10} = {}) {
  if (![143,10143].includes(data.chainId) || data.symbol !== 'MON' || !Array.isArray(data.closedHourlyCandles)) throw new Error('Monad MON closed candles required');
  if (![capital, notional, fast, slow, slippageBps, data.takerFeeBps].every(Number.isFinite) || capital <= 0 || notional <= 0 || notional > capital || !Number.isInteger(fast) || !Number.isInteger(slow) || fast < 2 || slow <= fast || slippageBps < 0 || data.takerFeeBps < 0) throw new Error('Invalid strategy parameters');
  const candles = data.closedHourlyCandles;
  if (candles.length < slow + 2) throw new Error('Insufficient closed candle history');
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    if (![c.time, c.open, c.high, c.low, c.close].every(Number.isFinite) || Math.min(c.open, c.high, c.low, c.close) <= 0 || c.high < Math.max(c.open, c.close) || c.low > Math.min(c.open, c.close) || c.time % 3600000 || (i && c.time !== candles[i-1].time + 3600000)) throw new Error('Invalid or non-contiguous closed candles');
    if (c.time + 3600000 > Date.parse(data.fetchedAt)) throw new Error('Incomplete candle rejected');
  }
  const average = (end, count) => candles.slice(end-count+1, end+1).reduce((sum, c) => sum+c.close, 0)/count;
  const fee = data.takerFeeBps/10000, slip = slippageBps/10000;
  let cash = capital, position = null, fees = 0, slippage = 0, grossPnl = 0, peak = capital, drawdown = 0;
  const orders = [], trades = [], equity = [];
  const exit = (price, time, reason, signalTime) => {
    const fill = price*(1-slip), exitFee = position.size*fill*fee;
    const gross = position.size*(price-position.reference), slipCost = position.size*(price-fill);
    const net = position.size*(fill-position.entry)-exitFee-position.entryFee;
    cash += position.size*(fill-position.entry)-exitFee;
    fees += exitFee; slippage += slipCost; grossPnl += gross;
    orders.push({side: 'close_long', signalTime, fillTime: time, referencePrice: price, fillPrice: fill, size: position.size, fee: exitFee, slippageCost: slipCost, reason});
    trades.push({entryTime: position.time, exitTime: time, entryPrice: position.entry, exitPrice: fill, size: position.size, grossPnl: gross, feeCost: position.entryFee+exitFee, slippageCost: position.entrySlippage+slipCost, netPnl: net, reason});
    position = null;
  };
  for (let i = slow; i < candles.length; i++) {
    const c = candles[i];
    // Signal uses only fully closed previous bars, fills at next observed open.
    const signal = average(i-1, fast) > average(i-1, slow);
    if (position && !signal) exit(c.open, c.time, 'fast_sma_below_slow_sma', c.time);
    if (!position && signal && cash > 0) {
      const fill = c.open*(1+slip), size = Math.floor(Math.min(notional, cash/(1+fee))/fill); // MON lot=1
      if (size > 0) {
        const entryFee = size*fill*fee, entrySlippage = size*(fill-c.open);
        cash -= entryFee; fees += entryFee; slippage += entrySlippage;
        position = {time: c.time, reference: c.open, entry: fill, size, entryFee, entrySlippage};
        orders.push({side: 'open_long', signalTime: c.time, fillTime: c.time, referencePrice: c.open, fillPrice: fill, size, fee: entryFee, slippageCost: entrySlippage, reason: 'fast_sma_above_slow_sma'});
      }
    }
    if (position && i === candles.length-1) exit(c.close, c.time+3600000, 'end_of_sample', c.time+3600000);
    const nav = cash + (position ? position.size*(c.close-position.entry) : 0);
    peak = Math.max(peak, nav); drawdown = Math.max(drawdown, (peak-nav)/peak);
    equity.push({time: c.time+3600000, equity: nav, cash, positionBase: position?.size ?? 0, mark: c.close});
  }
  return {strategy: 'MON hourly SMA trend: long/flat', mode: 'historical replay', chainId: data.chainId, venue: 'Perpl', symbol: 'MON',
    parameters: {capital, notional, fast, slow, slippageBps, takerFeeBps: data.takerFeeBps},
    sample: {candles: candles.length, firstOpen: candles[0].time, lastClosed: candles.at(-1).time+3600000, fetchedAt: data.fetchedAt},
    summary: {initialEquity: capital, finalEquity: cash, netPnl: cash-capital, returnPct: (cash/capital-1)*100, maxDrawdownPct: drawdown*100, closedTrades: trades.length, grossPnl, feeCost: fees, slippageCost: slippage},
    equity, trades, orders, source: data.provenance,
    assumptions: ['Signals use previous closed hourly candles; hypothetical execution at next open.', 'Long/flat, 1x; MON integer lot; fixed capped notional.', 'Taker fee uses current published tier-0 rate, not historical rate; specified slippage each side.', 'Final position closed at last closed candle close for accounting.', 'Funding, gas, liquidation, spread beyond assumed slippage and historical liquidity are excluded.', 'Short sample, no out-of-sample validation; replay is not a real order or proof of profitability.']};
}

export async function saveReplay(directory, inputs = {}) {
  const {chainId=10143, ...parameters}=inputs;
  const data = await snapshot(chainId, 'MON'), result = backtest(data, parameters);
  await mkdir(directory, {recursive: true});
  const json = value => JSON.stringify(value, null, 2)+'\n';
  for (const [name, value] of [['snapshot.json', data], ['backtest.json', result], ['equity.json', result.equity], ['trades.json', result.trades], ['orders.json', result.orders]]) await writeFile(resolve(directory, name), json(value));
  await writeFile(resolve(directory, 'equity.csv'), 'time,equity,cash,positionBase,mark\n'+result.equity.map(r => [new Date(r.time).toISOString(),r.equity,r.cash,r.positionBase,r.mark].join(',')).join('\n')+'\n');
  await writeFile(resolve(directory, 'trades.csv'), 'entryTime,exitTime,size,entryPrice,exitPrice,grossPnl,feeCost,slippageCost,netPnl,reason\n'+result.trades.map(r => [new Date(r.entryTime).toISOString(),new Date(r.exitTime).toISOString(),r.size,r.entryPrice,r.exitPrice,r.grossPnl,r.feeCost,r.slippageCost,r.netPnl,r.reason].join(',')).join('\n')+'\n');
  return result;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = await saveReplay(resolve(process.argv[2] || 'perpl-replay'), process.argv[3] ? JSON.parse(process.argv[3]) : {});
  console.log(JSON.stringify({summary: result.summary, sample: result.sample, output: resolve(process.argv[2] || 'perpl-replay'), mode: result.mode}));
}
