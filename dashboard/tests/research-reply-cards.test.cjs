// Isolated unit fixtures only; never used by live acceptance or screenshots.
// Run: node --test tests/research-reply-cards.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = new Proxy({}, { get: (_, key) => String(key) }); };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { ResearchReplyCards } = require('../components/chat/ResearchReplyCards.tsx');
const { ResearchInstrumentContext } = require('../components/chat/ResearchVisualContext.tsx');
const { collectResearchVisuals } = require('../lib/researchVisuals.ts');
const messages = require('../messages/en/research-workspace.json');
function reply(id, market, closes) {
  const instrument = { market, venue: 'binance', name: market.split('/')[0] + ' UNIT FIXTURE', interval: '1h' };
  const block = { kind: 'chart', chart_id: id, chart_kind: 'candlestick', path: 'inline', title: id + ' UNIT FIXTURE', instrument,
    source: { skill: 'research', action: 'unit_fixture', as_of: '2026-09-20T01:00:00Z' },
    series: [{ name: market, type: 'candlestick', data: closes.map((value, i) => ({ time: 1790000000 + i * 3600, open: value, high: value + 1, low: value - 1, close: value })) }] };
  return { role: 'assistant', id, ts: 1790000000000, turn: { reply_text: 'Unit test', blocks: [{ block }] } };
}
function html(message) { return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale: 'en', messages, timeZone: 'UTC' },
  React.createElement(ResearchInstrumentContext.Provider, { value: () => {} }, React.createElement(ResearchReplyCards, { message })))); }
test('each reply retains only its own instruments and observation values', () => {
  const first = reply('first', 'BTC/USDT', [100, 102]);
  const second = reply('second', 'ETH/USDT', [20, 19]);
  assert.equal(collectResearchVisuals({ messages: [first, second] }).instruments.length, 2);
  assert.equal(collectResearchVisuals({ messages: [first] }).instruments.length, 1);
  const rendered = html(first);
  assert.match(rendered, /research-instrument-card/); assert.match(rendered, /data-testid="research-card-price">102<\/strong>/);
  assert.match(rendered, /\+2%/); assert.match(rendered, /period/); assert.match(rendered, /<svg/);
  assert.doesNotMatch(rendered, /ETH|24h|disabled=""/);
  assert.match(html(second), /data-testid="research-card-price">19<\/strong>/); assert.doesNotMatch(html(second), /BTC/);
});
test('absent observations produce no invented quote or curve', () => {
  const message = reply('empty', 'BTC/USDT', []);
  const rendered = html(message);
  assert.match(rendered, /No price data published/); assert.doesNotMatch(rendered, /<svg[^>]+role="img"/); // Decorative navigation glyph is not an invented price curve.
  assert.equal(html({ role: 'assistant', id: 'plain', ts: 1, turn: { reply_text: 'BTC text is not evidence' } }), '');
});
test('card is mounted after the reply Markdown, not below the composer', () => {
  const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
  const translations = Object.assign({}, ...fs.readdirSync(path.join(__dirname, '../messages/en'))
    .filter(file => file.endsWith('.json')).map(file => require('../messages/en/' + file)));
  const msg = reply('mounted', 'BTC/USDT', [100, 102]);
  const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider,
    { locale: 'en', messages: translations, timeZone: 'UTC' },
    React.createElement(ResearchInstrumentContext.Provider, { value: () => {} },
      React.createElement(AssistantBubble, { msg }))));
  assert.ok(rendered.indexOf('Unit test') >= 0);
  assert.ok(rendered.indexOf('research-instrument-card') > rendered.indexOf('Unit test'));
  assert.equal((rendered.match(/data-testid="research-instrument-card"/g) || []).length, 1);
  const view = fs.readFileSync(path.join(__dirname, '../components/chat/ChatView.tsx'), 'utf8');
  assert.doesNotMatch(view, /<ResearchInstrumentBar/);
});
