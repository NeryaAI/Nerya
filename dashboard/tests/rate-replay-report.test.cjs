// Offline producer-contract fixtures only; no runtime artifacts or yield claims.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = new Proxy({}, { get: (_, key) => String(key) }); };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const messages = require('../messages');
const { backtestEvidenceKind } = require('../lib/backtestEvidence.ts');
const { extractBacktestResults } = require('../lib/backtestResults.ts');
const { BacktestReplyCards } = require('../components/chat/BacktestReplyCards.tsx');
const { RateSimulationReport, BacktestChart } = require('../components/backtest/BacktestChart.tsx');
const { JsonDetails } = require('../components/JsonView.tsx');

const metrics = {
  backtest_kind: 'rate_policy_replay', execution_mode: 'script', evaluation_mode: 'simulation', performance_evidence: false,
  initial_capital_usd: 1000, final_equity_usd: 1001.25, total_return_pct: 0.125, sharpe_ratio: null,
  start_utc: '2026-10-01T00:00:00Z', end_utc: '2026-10-03T00:00:00Z',
  requested_start_utc: '2026-10-01T00:00:00Z', requested_end_utc: '2026-10-04T00:00:00Z',
  requested_window_days: 3, requested_window_complete: false, coverage_ok: false,
  limitations: ['Fixture: constant unit price.', 'Fixture: unobserved intervals accrue no yield.'],
  gaps: [{ start: '2026-10-02T00:00:00Z', end: '2026-10-03T00:00:00Z', reason: 'fixture_missing_interval' }],
  replay: { scope: 'historical_rate_policy_simulation', strategy_execution_verified: true,
    financial_execution_allowed: false, decisions: 3, simulated_trades: 1, errors: 0 },
  provenance: { data_kind: 'historical_observations', source_revision: 'fixture-rate-source',
    strategy_id: 'rate_policy', proposal_id: 'prp_rate',
    datasets: [{ id: 'rates', sha256: 'fixture-input-hash' }], assumptions: { unit_price_usd: 1, fee_bps: 0 } },
};
const receipt = { ...metrics, metrics, kind: 'rate_policy_replay', result_type: 'backtest_result',
  ok: true, backtest_status: 'completed', strategy_id: 'rate_policy', proposal_id: 'prp_rate',
  backtest_ts: '20261028_120000', engine: 'rates', verdict: 'PASS',
  equity_preview: [{ time: 1790812800, value: 1000 }, { time: 1790899200, value: 1001.25 }],
};
const chart = { schema_version: '1.0', meta: { ...metrics, kind: 'rate_policy_replay', metrics },
  summary_cards: [{ label: 'total_return_pct', value: 0.125 }, { label: 'sharpe_ratio', value: 500 }],
  panels: [{ id: 'equity', type: 'line', title: 'Equity', series: [{ kind: 'line', name: 'equity', data: receipt.equity_preview }] }],
  tables: [
    { id: 'trades', columns: ['ts', 'side', 'size', 'price'], rows: [[1790812800, 'supply', 500, 1]] },
    { id: 'decisions', columns: ['ts', 'action', 'amount_units', 'apy_percent', 'reason'],
      rows: [[1790812800, 'supply', 500, 5, 'fixture_supply'], [1790899200, 'hold', 0, 4, 'fixture_hold'], [1790985600, 'hold', 0, 4, 'fixture_gap']] },
  ],
};
const render = (Component, props, locale = 'en') => renderToStaticMarkup(React.createElement(NextIntlClientProvider,
  { locale, messages: messages[locale], timeZone: 'UTC' }, React.createElement(Component, props)));
const message = (result = receipt) => ({ role: 'assistant', id: 'rate-fixture', ts: 1793145600000, loading: true,
  turn: { blocks: [{ block: { kind: 'tool_result', action: 'strategy_backtest', result } }] } });
const publicMarkup = html => html.replace(/<pre[^>]*>[\s\S]*?<\/pre>/g, '');
const forbidden = /simulated inputs|Agent stubs|模拟输入|Agent 桩|Economic checks passed|经济检查通过|data-execution-metric=|data-metric="(?:total_return_pct|max_drawdown_pct|sharpe_ratio|total_trades)"|Recorded backtest equity|回测净值|data-testid="backtest-factor-library"/;

test('index accounting labels follow producer basis without changing legacy APY labels', () => {
  const indexMetrics = { ...metrics, replay: { ...metrics.replay, accrual_basis: 'aave_normalized_income' },
    limitation_codes: ['normalized_income_counterfactual', 'instantaneous_observation_assumption'],
    limitations: ['Index-accounting fixture', 'Instant-observation fixture'] };
  const indexReceipt = { ...receipt, ...indexMetrics, metrics: indexMetrics };
  const indexChart = { ...chart, meta: { ...indexMetrics, kind: 'rate_policy_replay', metrics: indexMetrics },
    tables: [{ id: 'decisions', columns: ['ts', 'action', 'amount_units', 'base_supply_rate_percent'],
      rows: [[1790812800, 'hold', 0, 5]] }] };
  for (const locale of ['en', 'zh']) {
    const card = render(BacktestReplyCards, { message: message(indexReceipt) }, locale);
    const report = render(RateSimulationReport, { chart: indexChart }, locale);
    for (const html of [card, report]) {
      assert.match(html, locale === 'en' ? /Historical reserve-index policy simulation/ : /历史储备指数策略模拟/);
      assert.match(html, locale === 'en' ? /normalized-income index ratios/ : /归一化指数比值/);
      assert.doesNotMatch(publicMarkup(html), /actual historical APY observations|真实历史 APY 观测数据/);
      assert.doesNotMatch(publicMarkup(html), forbidden);
    }
    assert.match(report, locale === 'en' ? /Observed base supply APR/ : /观测基础供应 APR/);
    assert.match(report, locale === 'en' ? /confirmation, propagation and execution latency/ : /确认、传播或执行延迟/);
    assert.match(render(BacktestReplyCards, { message: message() }, locale),
      locale === 'en' ? /actual historical APY observations/ : /真实历史 APY 观测数据/);
  }
});

test('rate identity precedes script fallbacks but preserves custom/freeform evidence', () => {
  for (const identity of [{ kind: 'rate_policy_replay' }, { backtest_kind: 'rate_policy_replay' },
    { replay: { scope: 'historical_rate_policy_simulation' } }]) {
    assert.equal(backtestEvidenceKind({ execution_mode: 'script', performance_evidence: false, ...identity }), 'rate_simulation');
    assert.equal(backtestEvidenceKind({ ...identity, performance_evidence: true }), 'rate_simulation');
  }
  assert.equal(backtestEvidenceKind({ ...metrics, kind: 'freeform_backtest' }), 'custom_research');
  assert.equal(backtestEvidenceKind({ ...metrics, replay_kind: 'strategy_sdk_freeform' }), 'custom_research');
  assert.equal(backtestEvidenceKind({ ...metrics, replay: { scope: 'isolated_custom_calculation' } }), 'custom_research');
  assert.equal(backtestEvidenceKind({ execution_mode: 'script', performance_evidence: true }), 'trading');
  assert.equal(backtestEvidenceKind({ execution_mode: 'agent', performance_evidence: false }), 'observation');
  assert.equal(backtestEvidenceKind({ execution_mode: 'script', performance_evidence: false }), 'script_simulated');
  assert.equal(backtestEvidenceKind({ execution_mode: 'script', replay: { mock_surface_calls: [{ mode: 'stub' }] } }), 'script_simulated');
  for (const scope of ['sampled_fixed_range_counterfactual', 'cadence_fixed_range_counterfactual']) {
    const lp = { kind: 'freeform_backtest', execution_mode: 'script', performance_evidence: false,
      replay: { scope, strategy_execution_verified: true, accounting_verified: true } };
    assert.equal(backtestEvidenceKind(lp), 'lp_counterfactual');
    for (const field of ['accounting_verified', 'strategy_execution_verified']) for (const value of [false, undefined]) {
      assert.equal(backtestEvidenceKind({ ...lp, replay: { ...lp.replay, [field]: value } }), 'custom_research');
    }
    assert.equal(backtestEvidenceKind({ ...lp, replay: { ...lp.replay, scope: 'isolated_custom_calculation' } }), 'custom_research');
  }
});

test('trusted LP producer freeform shape routes card and report without trading claims', () => {
  for (const [scope, complete] of [['sampled_fixed_range_counterfactual', false],
    ['cadence_fixed_range_counterfactual', true], ['cadence_fixed_range_counterfactual', false],
    ['cadence_fixed_range_counterfactual', undefined]]) {
  const cadence = scope === 'cadence_fixed_range_counterfactual';
  const prefix = cadence ? 'lpCounterfactualCadence' : 'lpCounterfactual';
  const state = complete === true ? 'complete' : complete === false ? 'partial' : 'unknown';
  const lpReplay = { scope, strategy_execution_verified: true,
    accounting_verified: true, entrypoint: 'strategy.py:run', prepared_input_sha256: 'fixture-lp-input',
    rows_sha256: 'fixture-lp-rows', action_counts: { lp_add: 1, lp_remove: 0, hold: 2 },
    leading_gap_seconds: cadence ? 0 : 3600, trailing_gap_seconds: cadence ? 0 : 7200,
    gaps: cadence ? [] : [{ start: 1790812800, end: 1790899200, seconds: 86400, reason: 'no_tick_path_between_samples' }],
    requested_window_complete: cadence ? true : false, performance_evidence: false, financial_execution_allowed: false, funds_sent: false };
  const lpMetrics = { backtest_kind: 'freeform_backtest', performance_evidence: false, execution_mode: 'script',
    requested_window_days: 30, requested_window_complete: complete,
    start_utc: '2026-10-01T00:00:00Z', end_utc: cadence ? '2026-10-31T00:00:00Z' : '2026-10-30T12:28:48Z',
    accounting_currency: 'token1:TOK1', modeled_initial_value: 1, modeled_final_value: 1.01,
    total_return_pct: null, sharpe_ratio: null, total_trades: 0 };
  const lpReceipt = { ...receipt, ...lpMetrics, kind: 'freeform_backtest', engine: 'freeform',
    metrics: lpMetrics, replay: lpReplay, verdict: 'RESEARCH', requested_window_complete: complete };
  const lpChart = { ...chart, meta: { ...lpMetrics, kind: 'freeform_backtest', replay: lpReplay },
    tables: [{ id: 'decisions', columns: ['ts', 'block_number', 'action', 'reason'], rows: [[1790812800, 1, 'lp_add', 'fixture_entry']] }] };
  for (const locale of ['en', 'zh']) {
    const card = render(BacktestReplyCards, { message: message(lpReceipt) }, locale);
    assert.match(card, /data-evaluation="lp_counterfactual"/);
    assert.doesNotMatch(card, /WETH/);
    assert.ok(card.includes(`data-coverage-status="${state}"`));
    assert.ok(card.includes('<strong>30</strong>'));
    assert.ok(card.includes(`<strong>${cadence ? '30' : '29.52'}</strong>`));
    const copy = require(`../messages/${locale}/trading.json`).copy.components_backtest_BacktestChart;
    for (const suffix of ['Title', 'Warning', 'Action']) assert.ok(card.includes(copy[prefix + suffix]), prefix + suffix);
    assert.doesNotMatch(publicMarkup(card), forbidden);
    assert.doesNotMatch(card, /data-testid="backtest-coverage-summary"|data-execution-metric=|data-metric=/);
    const originalUseState = React.useState;
    let supplied = false;
    React.useState = initial => {
      if (initial === null && !supplied) { supplied = true; return originalUseState(lpChart); }
      return originalUseState(initial);
    };
    try {
      const report = render(BacktestChart, { strategyId: 'lp_fixture', ts: 'fixture' }, locale);
      assert.match(report, /data-report-kind="lp_counterfactual"/);
      assert.ok(report.includes(`data-coverage-status="${state}"`));
      assert.ok(report.includes('<strong>30</strong>'));
      assert.ok(report.includes(`<strong>${cadence ? '30' : '29.52'}</strong>`));
      for (const value of ['strategy.py:run', 'fixture-lp-input', 'token1:TOK1', 'lp_add',
        ...(cadence ? [] : ['3600', '7200', 'no_tick_path_between_samples'])]) assert.ok(report.includes(value), value);
      assert.doesNotMatch(report, /WETH/);
      assert.match(report, /data-testid="backtest-chart"/);
      assert.match(report, /fixture_entry/);
      assert.doesNotMatch(report, /fixture-lp-rows/);
      for (const suffix of ['Title', 'Warning']) assert.ok(report.includes(copy[prefix + suffix]), prefix + suffix);
      for (const html of [card, report]) {
        assert.match(html, locale === 'en' ? /quote-token units/ : /报价代币/);
        assert.match(html, locale === 'en' ? /(?:ignores|excludes) dilution and price impact/ : /(?:未计|不包含(?:流动性)?)稀释和价格冲击/);
        assert.match(html, locale === 'en' ? /not.*realized LP yield/ : /不.*(?:已实现|实际) LP 收益/);
        if (cadence) assert.doesNotMatch(publicMarkup(html), /[Ss]ampled|between samples|采样/);
      }
      assert.doesNotMatch(publicMarkup(report), forbidden);
    } finally { React.useState = originalUseState; }
  }
  }
});

for (const locale of ['en', 'zh']) {
  test(`${locale}: compact rate receipt preserves host metrics, modeled curve, coverage and source details`, () => {
    const [result] = extractBacktestResults(receipt);
    assert.deepEqual(result.metrics.limitations, metrics.limitations);
    assert.deepEqual(result.metrics.gaps, metrics.gaps);
    const html = render(BacktestReplyCards, { message: message() }, locale);
    assert.match(html, /data-evaluation="rate_simulation"/);
    assert.match(html, locale === 'en' ? /Historical rate-policy simulation/ : /历史利率策略模拟/);
    assert.match(html, locale === 'en' ? /actual historical APY observations/ : /真实历史 APY 观测数据/);
    assert.match(html, locale === 'en' ? /Aave reserve-index reconstruction, on-chain fills, realized yield/ : /Aave 储备指数重建、链上成交或已实现收益/);
    assert.match(html, locale === 'en' ? /Modeled value \(USD\)/ : /模型计算价值（USD）/);
    assert.match(html, /data-metric="initial_capital_usd">1,000/);
    assert.match(html, /data-metric="final_equity_usd">1,001.25/);
    assert.match(html, /data-metric="decisions">3<|data-metric="decisions">3.0*</);
    assert.match(html, /data-metric="simulated_trades">1<|data-metric="simulated_trades">1.0*</);
    assert.match(html, /data-coverage-status="partial"/);
    assert.match(html, /<svg[^>]*role="img"/);
    assert.doesNotMatch(html, /fixture-input-hash|fixture_missing_interval|financial_execution_allowed/);
    assert.match(html, /data-testid="open-backtest-report"/);
    assert.match(html, /aria-expanded="false"/);
    assert.doesNotMatch(publicMarkup(html), forbidden);
  });

  test(`${locale}: report shows actual record counts, rows, modeled value and expandable evidence`, () => {
    const input = structuredClone(chart);
    input.meta.replay.decisions = 999; // Displayed counts must come from records, not claimed totals.
    const original = JSON.stringify(input);
    const html = render(RateSimulationReport, { chart: input }, locale);
    assert.match(html, /data-report-kind="rate_simulation"/);
    assert.match(html, /data-rate-record-count="equity">2</);
    assert.match(html, /data-rate-record-count="trades">1</);
    assert.match(html, /data-rate-record-count="decisions">3</);
    assert.match(html, /data-modeled-metric="final_equity_usd">1,001.25/);
    assert.match(html, /data-testid="backtest-chart"/);
    assert.match(html, /data-coverage-status="partial"/);
    assert.match(html, /<details><summary[^>]*>[^<]+<\/summary>[\s\S]*fixture_hold/);
    assert.doesNotMatch(html, /<pre|fixture_missing_interval|fixture-input-hash/);
    assert.match(html, /supply/);
    assert.match(html, locale === 'en' ? /Simulated allocation actions/ : /模拟资金配置动作/);
    assert.doesNotMatch(publicMarkup(html), forbidden);
    assert.equal(JSON.stringify(input), original);
  });
}

test('nested chart metrics retain partial coverage, limitations, zero values and unknown counts', () => {
  assert.doesNotThrow(() => render(JsonDetails, { summary: 'Evidence',
    value: { toJSON() { throw new Error('Closed evidence must not be serialized'); } } }));
  const nested = { ...chart, meta: { kind: 'rate_policy_replay', metrics: { ...metrics, initial_capital_usd: 0, final_equity_usd: null } }, panels: [], tables: [] };
  const html = render(RateSimulationReport, { chart: nested });
  assert.match(html, /data-modeled-metric="initial_capital_usd">0/);
  assert.match(html, /data-modeled-metric="final_equity_usd">—</);
  assert.match(html, /data-coverage-status="partial"/);
  assert.doesNotMatch(publicMarkup(html), /Fixture: constant unit price/);
  assert.doesNotMatch(html, /Fixture: constant unit price/);
  for (const count of ['equity', 'trades', 'decisions']) assert.match(html, new RegExp(`data-rate-record-count="${count}">—<`));
  const empty = render(RateSimulationReport, { chart: { ...nested, panels: [{ ...chart.panels[0], series: [{ kind: 'line', data: [] }] }], tables: chart.tables.map(table => ({ ...table, rows: [] })) } });
  for (const count of ['equity', 'trades', 'decisions']) assert.match(empty, new RegExp(`data-rate-record-count="${count}">0<`));
  assert.doesNotMatch(publicMarkup(html), forbidden);
});

test('rate curve does not add or display a trading benchmark', () => {
  const input = structuredClone(chart);
  input.panels[0].series.push({ kind: 'line', name: 'benchmark', data: [{ time: 1790812800, value: 5000 }] });
  input.panels.push({ id: 'price', title: 'Market price', type: 'line', series: [{ kind: 'candles', data: [] }] });
  const html = render(RateSimulationReport, { chart: input });
  assert.match(html, /data-rate-record-count="equity">2</);
  assert.equal((html.match(/data-testid="backtest-chart"/g) || []).length, 1);
  assert.doesNotMatch(publicMarkup(html), /benchmark|Market price|Sharpe/);
});

test('loaded BacktestChart routes a rate receipt to its dedicated report', () => {
  const originalUseState = React.useState;
  let supplied = false;
  React.useState = initial => {
    if (initial === null && !supplied) { supplied = true; return originalUseState(chart); }
    return originalUseState(initial);
  };
  try {
    const html = render(BacktestChart, { strategyId: receipt.strategy_id, ts: receipt.backtest_ts });
    assert.match(html, /data-report-kind="rate_simulation"/);
    assert.doesNotMatch(publicMarkup(html), forbidden);
  } finally { React.useState = originalUseState; }
});

test('failed rates never show modeled success and absent counters are not invented', () => {
  const failed = render(BacktestReplyCards, { message: message({ ...receipt, ok: false, backtest_status: 'failed', message: 'Rate replay failed' }) });
  assert.match(failed, /data-status="failed"/);
  assert.doesNotMatch(failed, /data-metric=|<svg[^>]*role="img"|data-testid="open-backtest-report"|Simulation recorded/);
  const sparse = render(BacktestReplyCards, { message: message({ ...receipt, replay: { scope: metrics.replay.scope }, metrics: {}, equity_preview: [] }) });
  assert.match(sparse, /data-metric="decisions">—</);
  assert.match(sparse, /data-metric="simulated_trades">—</);
  assert.doesNotMatch(sparse, /<svg[^>]*role="img"/);
});

test('all dedicated rate report keys exist in both languages', () => {
  const en = require('../messages/en/trading.json').copy.components_backtest_BacktestChart;
  const zh = require('../messages/zh/trading.json').copy.components_backtest_BacktestChart;
  const keys = Object.keys(en).filter(key => key.startsWith('rateReplay'));
  assert.ok(keys.length >= 37);
  assert.deepEqual(keys, Object.keys(zh).filter(key => key.startsWith('rateReplay')));
  for (const key of keys) { assert.ok(en[key]); assert.ok(zh[key]); assert.notEqual(en[key], zh[key]); }
});

// Reduced fixture from rates_20261028_080218/chart.json. Preserve producer fields
// and row values; tests never depend on or modify the local runtime artifact.
const actualLimitations = [
  'APY observations with previous-rate constant-interval approximation, not Aave reserve-index reconstruction.',
  'Simulated instant supply/withdraw and constant unit price; no chain fills, gas, depeg, latency or settlement proof.',
  'Current catalog metadata identifies the pool; historical identity changes are not independently attested.',
  'Requested window has unobserved intervals or source gaps; no rate accrual was invented for those intervals.',
];
const limitationCodes = ['apy_interval_approximation', 'simulated_allocation_constant_price',
  'historical_pool_identity_unattested', 'observation_coverage_gap'];
const actualCoverage = 'Historical observation gaps remain; partial simulation only.';
const actualShape = { ...chart, meta: { ...chart.meta, limitations: actualLimitations, coverage_message: actualCoverage },
  tables: [
    { id: 'trades', columns: ['ts', 'action', 'amount_units', 'observation_timestamp', 'apy_percent', 'cash_units',
      'supplied_units', 'equity', 'side', 'size', 'price', 'fee_units', 'execution'],
    rows: [[1790636489.583, 'lend_supply', 1000, '2026-09-28T23:01:29.583000Z', 3.64852, 0, 1000, 1000,
      'lend_supply', 1000, 1, 0, 'simulated_rate_allocation']] },
    { id: 'decisions', columns: ['ts', 'action', 'amount_units', 'observation_timestamp', 'apy_percent', 'cash_units', 'supplied_units', 'equity'],
    rows: [[1790636489.583, 'lend_supply', 1000, '2026-09-28T23:01:29.583000Z', 3.64852, 0, 1000, 1000],
      [1790722863.7, 'hold', 0, '2026-09-29T23:01:03.700000Z', 3.63314, 0, 1000.0981545119151, 1000.0981545119151]] },
  ],
};

for (const locale of ['en', 'zh']) for (const coded of [false, true]) {
  test(`${locale}: actual rate table shape localizes ${coded ? 'stable codes' : 'exact legacy text'} without changing evidence`, () => {
    const input = structuredClone(actualShape);
    if (coded) Object.assign(input.meta, { limitation_codes: limitationCodes, coverage_reason: 'observation_coverage_gap' });
    const before = JSON.stringify(input);
    const html = render(RateSimulationReport, { chart: input }, locale), visible = publicMarkup(html);
    const copy = require(`../messages/${locale}/trading.json`).copy.components_backtest_BacktestChart;
    for (const key of ['rateReplayLimitApy', 'rateReplayLimitAllocation', 'rateReplayLimitIdentity', 'rateReplayLimitGap',
      'rateReplayCoverageGap', 'rateReplayFieldAction', 'rateReplayFieldAmount', 'rateReplayFieldObservation', 'rateReplayFieldApy',
      'rateReplayFieldCash', 'rateReplayFieldSupplied', 'rateReplayFieldEquity', 'rateReplayFieldPrice', 'rateReplayFieldFee',
      'rateReplayFieldExecution', 'rateReplaySupply', 'rateReplayHold', 'rateReplaySimulatedExecution']) assert.ok(visible.includes(copy[key]), key);
    assert.doesNotMatch(visible, /成交价格|Avg price|>price<|>action<|amount units|observation timestamp|apy percent|cash units|supplied units|fee units|>execution<|lend_supply|simulated_rate_allocation/);
    assert.match(visible, /2026-09-28T23:01:29.583000Z/);
    assert.match(visible, /data-rate-record-count="trades">1</);
    assert.match(visible, /data-rate-record-count="decisions">2</);
    assert.doesNotMatch(html, /<pre/);
    for (const raw of [...actualLimitations, actualCoverage]) assert.ok(!html.includes(raw));
    assert.equal(JSON.stringify(input), before);

    const reply = { ...receipt, metrics: { ...metrics, limitations: input.meta.limitations, coverage_message: actualCoverage,
      ...(coded ? { limitation_codes: limitationCodes, coverage_reason: 'observation_coverage_gap' } : {}) }, coverage_message: actualCoverage };
    const card = render(BacktestReplyCards, { message: message(reply) }, locale);
    assert.ok(publicMarkup(card).includes(copy.rateReplayCoverageGap));
    assert.ok(!publicMarkup(card).includes(actualCoverage));
    assert.ok(!card.includes(actualCoverage));
  });
}

test('unknown producer notes stay exact in evidence and codes take precedence over legacy prose', () => {
  const unknown = 'Producer note: supply/price unchanged <opaque> & APY 4.321';
  const input = structuredClone(actualShape);
  Object.assign(input.meta, { requested_window_complete: true, limitations: [...actualLimitations, unknown], limitation_codes: ['apy_interval_approximation', 'future_limit'],
    coverage_reason: 'observation_intervals_covered', coverage_message: unknown });
  const html = render(RateSimulationReport, { chart: input }, 'zh'), visible = publicMarkup(html);
  assert.match(visible, /观测区间已覆盖/);
  assert.doesNotMatch(visible, /历史观测存在缺口|未独立核验资金池身份|Producer note/);
  assert.doesNotMatch(html, /<pre|Producer note/);
  const card = render(BacktestReplyCards, { message: message({ ...receipt, requested_window_complete: true, metrics: { ...metrics, coverage_reason: 'observation_intervals_covered' }, coverage_message: unknown }) }, 'zh');
  assert.match(publicMarkup(card), /观测区间已覆盖/);
  assert.doesNotMatch(publicMarkup(card), /Producer note/);
  assert.doesNotMatch(card, /Producer note/);

  const future = render(RateSimulationReport, { chart: { ...input, meta: { ...input.meta, limitation_codes: ['future_limit'], coverage_reason: 'future_reason' } } }, 'zh');
  assert.match(publicMarkup(future), /其他生产端说明保留/);
  assert.doesNotMatch(publicMarkup(future), /future_reason|future_limit|Producer note/);
});

test('modeled interval coverage is localized without claiming raw completeness or realized yield', () => {
  for (const locale of ['en', 'zh']) {
    const copy = require(locale === 'en' ? '../messages/en/trading.json' : '../messages/zh/trading.json').copy.components_backtest_BacktestChart;
    const input = structuredClone(actualShape);
    input.meta.coverage_reason = 'modeled_intervals_covered';
    input.meta.coverage_ok = true;
    input.meta.requested_window_complete = true;
    const report = render(RateSimulationReport, { chart: input }, locale);
    const card = render(BacktestReplyCards, { message: message({
      ...receipt,
      coverage_ok: true, requested_window_complete: true,
      metrics: { ...metrics, coverage_ok: true, requested_window_complete: true, coverage_reason: 'modeled_intervals_covered' },
    }) }, locale);
    for (const html of [report, card]) {
      const visible = publicMarkup(html);
      assert.ok(visible.includes(copy.rateReplayCoverageModeled));
      assert.doesNotMatch(visible, /observation_intervals_covered|Observation intervals covered|观测区间已覆盖/);
      assert.match(visible, /raw observation completeness|原始观测完整/);
      assert.match(visible, /realized yield|已实现收益/);
      assert.match(visible, /APY performance|APY 表现/);
    }
    assert.ok(!report.includes('modeled_intervals_covered'));
    assert.ok(!card.includes('modeled_intervals_covered'));
  }
});

test('top-level producer rate evidence survives extraction without a duplicated metrics object', () => {
  const { metrics: _nestedMetrics, ...topLevelReceipt } = receipt;
  Object.assign(topLevelReceipt, {
    coverage_reason: 'modeled_intervals_covered', coverage_ok: true,
    requested_window_complete: true,
    limitation_codes: ['apy_interval_approximation'],
    limitations: ['APY observations use previous-rate interval approximation.'],
  });
  const [row] = extractBacktestResults(topLevelReceipt, 0, 'strategy_backtest');
  assert.equal(row.metrics.coverage_reason, 'modeled_intervals_covered');
  assert.equal(row.metrics.coverage_ok, true);
  assert.equal(row.metrics.requested_window_complete, true);
  assert.deepEqual(row.metrics.limitation_codes, ['apy_interval_approximation']);

  const rendered = render(BacktestReplyCards, { message: message(topLevelReceipt) }, 'en');
  assert.match(publicMarkup(rendered), /Observation intervals are covered|Source-bracketed modeled intervals are covered/);
});

for (const locale of ['en', 'zh']) {
  test(`${locale}: partial rate coverage requires missing observations without an unchanged retry instruction`, () => {
    const copy = require(`../messages/${locale}/trading.json`).copy;
    const input = structuredClone(actualShape);
    // A stale success code cannot override an explicit incomplete window.
    input.meta.coverage_reason = 'observation_intervals_covered';
    const card = render(BacktestReplyCards, { message: message({ ...receipt, coverage_message: actualCoverage,
      metrics: { ...metrics, coverage_reason: 'observation_intervals_covered' } }) }, locale);
    for (const html of [render(RateSimulationReport, { chart: input }, locale), card]) {
      const visible = publicMarkup(html);
      assert.match(visible, /data-coverage-status="partial"/);
      assert.ok(visible.includes(copy.components_backtest_BacktestChart.rateReplayCoverageGap));
      assert.ok(!visible.includes(copy.components_backtest_BacktestCoverage['008']));
      assert.doesNotMatch(visible, /New downloads|rerun strategy|重新下载|重跑策略|观测区间已覆盖/);
    }
  });
}
