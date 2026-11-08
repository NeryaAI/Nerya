// Offline component fixtures only; these are not live Aave completion evidence.
// Run: node --test tests/chat-message-delivery-warning.test.cjs
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
const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
const translations = Object.fromEntries(['en', 'zh'].map(locale => [locale, Object.assign({},
  ...fs.readdirSync(path.join(__dirname, '../messages', locale)).filter(file => file.endsWith('.json'))
    .map(file => require('../messages/' + locale + '/' + file)))]));
const claim = 'Aave USDC 收益策略已完成。';
const delivery = (allowed = false, status = 'incomplete') => ({
  protocol: 'nerya.task_delivery.v1', delivery_id: 'fixture-delivery', command_id: 'fixture-command',
  skill_id: 'strategy_author', workflow_id: 'fixture-workflow', delivery_revision: 1,
  stages: [], artifacts: [], checks: [], completion: { allowed, status },
});
const message = value => ({
  id: 'fixture-turn', role: 'assistant', ts: 1790000000000, execution_status: 'succeeded',
  turn: { reply_text: claim, delivery: value },
});
function render(msg, locale) {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, {
    locale, messages: translations[locale], timeZone: 'UTC',
  }, React.createElement(AssistantBubble, { msg })));
}
function replySection(html) {
  const section = html.match(/<section[^>]*data-turn-section="reply"[^>]*>([\s\S]*?)<\/section>/)?.[1];
  assert.ok(section, 'the actual reply section must be rendered without a task header');
  return section;
}

for (const locale of ['en', 'zh']) {
  test(locale + ': incomplete delivery warns locally before unchanged final Markdown', () => {
    const msg = message(delivery());
    const before = JSON.stringify(msg);
    const section = replySection(render(msg, locale));
    const warning = section.match(/<p[^>]*role="status"[^>]*>([^<]*)<\/p>/)?.[1];
    const expected = renderToStaticMarkup(React.createElement('p', null, translations[locale].assistants.deliveryUnconfirmed)).slice(3, -4);
    assert.equal(warning, expected);
    assert.ok(section.indexOf(warning) < section.indexOf(claim));
    assert.ok(section.includes('>' + claim + '</p>'), 'model prose stays in Markdown verbatim');
    assert.equal(section.split(warning).length - 1, 1);
    assert.equal(JSON.stringify(msg), before, 'rendering must preserve the original message and evidence');
  });

  for (const [name, value] of [
    ['allowed partial', delivery(true, 'partial')],
    ['complete', delivery(true, 'complete')],
    ['absent delivery', undefined],
    ['null delivery', null],
  ]) {
    test(locale + ': ' + name + ' keeps the ordinary final reply unchanged', () => {
      const html = render(message(value), locale);
      assert.equal(html, render(message(undefined), locale));
      assert.ok(replySection(html).includes('>' + claim + '</p>'));
      assert.doesNotMatch(html, /data-testid="task-delivery-warning"/);
    });
  }

  test(locale + ': loading with an incomplete delivery does not show a final warning', () => {
    const msg = { ...message(delivery()), loading: true, execution_status: 'running',
      live_events: [{ seq: 1, kind: 'message.delta', text: 'Still working on Aave.' }],
    };
    const html = render(msg, locale);
    assert.equal(html, render({ ...msg, turn: { ...msg.turn, delivery: undefined } }, locale));
    assert.ok(replySection(html).includes('Still working on Aave.'));
    assert.match(html, /aria-busy="true"/);
    assert.doesNotMatch(html, /data-testid="task-delivery-warning"/);
  });
}
