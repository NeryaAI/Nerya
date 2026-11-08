// Isolated receipt fixtures only; no runtime/session writes or acceptance evidence.
// Run: node --test tests/backtest-coverage-localization.test.cjs
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
const { BacktestReplyCards } = require('../components/chat/BacktestReplyCards.tsx');

const providerMessage = 'All requested closed candles and warmup rows are present in the verified local datasets.';
const receipt = {
  result_type: 'backtest_result', ok: true, backtest_status: 'completed',
  strategy_id: 'cex_ema_fixture', proposal_id: 'prp_coverage_fixture', backtest_ts: '20261028_234610',
  title: 'CEX coverage unit fixture', verdict: 'WARN', execution_mode: 'script', evaluation_mode: 'trading',
  provenance: { data_kind: 'historical', source_revision: 'fixture-revision' },
  start_utc: '2026-09-28T23:00:00Z', end_utc: '2026-10-28T22:00:00Z', tf: '1h',
  requested_window_days: 30, requested_window_complete: true, coverage_ok: true,
  data_manifest: { requested_window_complete: true, end_exclusive: true,
    requested_start_utc: '2026-09-28T23:00:00Z', requested_end_utc: '2026-10-28T23:00:00Z' },
  metrics_display: { total_return_pct: '0.25%', max_drawdown_pct: '0.12%', total_trades: '4' },
  replay: { order_attempts: 4, orders_submitted: 4, orders_filled: 4, orders_rejected: 0 },
  coverage_message: providerMessage,
};
const labels = {
  en: { complete: 'Window verified', partial: 'Incomplete coverage', unknown: 'Completeness not recorded',
    notes: 'Producer notes are preserved in the expandable details.' },
  zh: { complete: '区间已核对', partial: '覆盖不足', unknown: '完整性未记录',
    notes: '生产端说明保留在可展开的详情中。' },
};
function render(value, locale) {
  const message = { role: 'assistant', id: 'coverage-fixture', ts: 1793231170000,
    turn: { blocks: [{ block: { kind: 'tool_result', action: 'strategy_backtest', result: value } }] } };
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale, messages: {}, timeZone: 'UTC' },
    React.createElement(BacktestReplyCards, { message })));
}
const summary = html => html.match(/<p[^>]*data-testid="backtest-coverage-summary"[^>]*>(.*?)<\/p>/)?.[1];
const collapsed = html => html.replace(/<details\b[\s\S]*?<\/details>/g, '');

for (const locale of ['zh', 'en']) {
  test(`${locale}: complete CEX receipt uses localized typed coverage and preserves raw prose in closed details`, () => {
    const before = JSON.stringify(receipt);
    const html = render(receipt, locale);
    assert.equal(summary(html), labels[locale].complete);
    assert.match(html, /data-coverage-status="complete"/);
    assert.ok(collapsed(html).includes(labels[locale].complete));
    assert.ok(!collapsed(html).includes(providerMessage));
    assert.ok(html.includes(`<pre>prp_coverage_fixture\ncex_ema_fixture\n20261028_234610\nfixture-revision\n${providerMessage}</pre>`));
    assert.doesNotMatch(html, /<details[^>]*\bopen(?:=|[ >])/);
    assert.match(html, /aria-expanded="false"/);
    assert.equal(JSON.stringify(receipt), before);
    // Provider wording is not an input to the coverage verdict.
    assert.equal(summary(render({ ...receipt, coverage_message: 'Different provider wording.' }, locale)), labels[locale].complete);
  });

  test(`${locale}: explicit gaps and missing candles override full endpoints`, () => {
    for (const fields of [
      { requested_window_complete: false },
      { data_manifest: { ...receipt.data_manifest, requested_window_complete: false } },
      { requested_window_complete: undefined, data_manifest: {}, start_utc: '2026-10-27T23:00:00Z' },
    ]) {
      const html = render({ ...receipt, ...fields }, locale);
      assert.equal(summary(html), labels[locale].partial);
      assert.match(html, /data-coverage-status="partial"/);
      assert.ok(!collapsed(html).includes(labels[locale].complete));
      assert.ok(!collapsed(html).includes(providerMessage));
    }
  });

  test(`${locale}: legacy coverage_ok and missing completeness cannot certify a window`, () => {
    for (const completeness of [null, undefined, 'true']) {
      const html = render({ ...receipt, requested_window_complete: completeness, data_manifest: {} }, locale);
      assert.equal(summary(html), labels[locale].unknown);
      assert.match(html, /data-coverage-status="unknown"/);
      assert.ok(!collapsed(html).includes(labels[locale].complete));
    }
    const missing = render({ ...receipt, requested_window_complete: undefined, requested_window_days: undefined,
      data_manifest: {}, start_utc: '', end_utc: '' }, locale);
    assert.equal(summary(missing), labels[locale].unknown);
    assert.doesNotMatch(missing, /data-testid="backtest-coverage"/);
  });

  test(`${locale}: arbitrary custom diagnostics stay notes and raw text is escaped`, () => {
    const message = '<script>verified = true</script>';
    const html = render({ ...receipt, kind: 'freeform_backtest', performance_evidence: false,
      requested_window_complete: null, data_manifest: {}, coverage_message: message,
      replay: { scope: 'isolated_custom_calculation' },
      metrics: { diagnostics: { requested_window_complete: true, coverage_ok: true } } }, locale);
    assert.equal(summary(html), labels[locale].notes);
    assert.match(html, /data-evaluation="custom_research"/);
    assert.match(html, /data-coverage-status="unknown"/);
    assert.ok(!collapsed(html).includes(labels[locale].complete));
    assert.ok(!collapsed(html).includes('verified = true'));
    assert.match(html, /&lt;script&gt;verified = true&lt;\/script&gt;/);
    assert.doesNotMatch(html, /<script>/);
  });

  test(`${locale}: failed and blocked receipts never show a completed coverage summary`, () => {
    for (const status of ['failed', 'blocked']) {
      const html = render({ ...receipt, ok: false, backtest_status: status,
        reason: 'request_scope_unverified', message: 'request_scope_unverified: invalid_binding' }, locale);
      assert.ok(html.includes(`data-status="${status}"`));
      assert.equal(summary(html), undefined);
      assert.doesNotMatch(html, /data-coverage-status="complete"|data-testid="open-backtest-report"/);
      assert.match(html, /role="status"/);
    }
  });
}
