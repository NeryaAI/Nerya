import {scenario} from './core.mjs';

export function parseInputs(values) {
  const bounds = {capital: [0.01, 100000000], notional: [0, 1000], adversePct: [0, 100], maxLossPct: [0.01, 100], slippageBps: [0, 10000]};
  const inputs = {};
  for (const [key, [min, max]] of Object.entries(bounds)) {
    const raw = String(values[key] ?? '').trim();
    const value = raw === '' ? NaN : Number(raw);
    if (!Number.isFinite(value) || value < min || value > max) throw new Error(`${key}: enter a number from ${min} to ${max}.`);
    inputs[key] = value;
  }
  return inputs;
}

export function observationAt(data, now = Date.now()) {
  const sourceAgeSeconds = (now - Date.parse(data.observedAt)) / 1000;
  return {...data, sourceAgeSeconds};
}

export function compare(data, inputs, now = Date.now()) {
  const current = observationAt(data, now);
  const baseline = scenario(current, {...inputs, notional: 1000});
  const candidate = scenario(current, inputs);
  return {baseline, candidate, sourceAgeSeconds: current.sourceAgeSeconds, reduction: baseline.totalLoss - candidate.totalLoss};
}
