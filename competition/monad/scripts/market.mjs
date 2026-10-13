import {snapshot, scenario} from '../core.mjs';
try {
  const inputs = process.argv[2] ? JSON.parse(process.argv[2]) : {};
  const data = await snapshot(inputs.symbol || 'MON');
  const candles = data.closedHourlyCandles;
  const coverage = {count: candles.length, firstOpen: candles[0] ? new Date(candles[0].time).toISOString() : null, lastOpen: candles.at(-1) ? new Date(candles.at(-1).time).toISOString() : null};
  const {closedHourlyCandles, ...summary} = data;
  console.log(JSON.stringify({risk: scenario(data, inputs), originalRisk: scenario(data, {...inputs, notional: 1000}), candleCoverage: coverage, market: summary, closedHourlyCandles}));
} catch (error) {
  console.error(JSON.stringify({ok: false, error: error.message, noOrderSubmitted: true}));
  process.exitCode = 1;
}
