// Offline fixtures from the reported metadata, not runtime/data verification.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
const translations = Object.fromEntries(['en', 'zh'].map(locale => [locale, Object.assign({},
  ...fs.readdirSync(path.join(__dirname, '../messages', locale)).filter(file => file.endsWith('.json'))
    .map(file => require('../messages/' + locale + '/' + file)))]));
const notices = {
  zh: '本轮未记录工具执行；以下为助手文字回复，不是执行或数据核验收据。',
  en: 'No tool execution was recorded for this turn; the text below is an assistant reply, not a receipt of execution or data verification.',
};
const claim = '已执行查询，返回 72 行。';
const fixture = (turn = {}, extra = {}) => ({
  id: 'fixture-turn', command_id: '8f722d9b-90bd-4363-91cb-257b9de4f48e',
  role: 'assistant', ts: 1790000000000, execution_status: 'succeeded',
  turn: {
    context_snapshot: { run_settings: { work_mode: 'execute' } },
    verifier_outcome: { model_done: true, hard_status: 'missing', trusted: false, has_hard_evidence: false, has_validation_attempt: false },
    tool_trace: [], actions: [{ action: 'send_message' }],
    blocks: [{ block: { kind: 'text', text: claim } }], reply_text: claim, delivery: null, ...turn,
  }, ...extra,
});
const delivery = allowed => ({
  protocol: 'nerya.task_delivery.v1', delivery_id: 'fixture-delivery', command_id: 'fixture-command',
  skill_id: 'strategy_author', workflow_id: 'fixture-workflow', delivery_revision: 1,
  stages: [], artifacts: [], checks: [], completion: { allowed, status: allowed ? 'complete' : 'incomplete' },
});
const render = (msg, locale) => renderToStaticMarkup(React.createElement(NextIntlClientProvider, {
  locale, messages: translations[locale], timeZone: 'UTC',
}, React.createElement(AssistantBubble, { msg })));
const shouldNotice = msg => require('../lib/turnExecutionEvidence.ts').shouldShowTurnExecutionNotice(msg, msg.turn?.reply_text || '');

for (const locale of ['en', 'zh']) {
  test(locale + ': replies remain unchanged without an inline execution notice', () => {
    for (const body of ['收到', 'Hello.', claim]) {
      const msg = fixture({ reply_text: body, blocks: [{ block: { kind: 'text', text: body } }] });
      const before = JSON.stringify(msg);
      const html = render(msg, locale);
      const section = html.match(/<section[^>]*data-turn-section="reply"[^>]*>([\s\S]*?)<\/section>/)?.[1];
      assert.ok(section);
      assert.ok(!section.includes(notices[locale]));
      assert.doesNotMatch(section, /data-testid="turn-execution-notice"/);
      assert.doesNotMatch(section, /text-warn|text-danger|task-delivery-warning/);
      assert.ok(section.includes('>' + body + '</p>'));
      assert.equal(JSON.stringify(msg), before);
      assert.equal(translations[locale].chat.turnExecutionNotice, notices[locale]);
    }
  });
}

test('notice uses complete explicit metadata, never command success or model prose', () => {
  assert.equal(shouldNotice(fixture()), true);
  for (const body of ['Hello.', 'I did not execute anything.', 'data_api and execute_code returned 72 rows.', 'tool.start tool_result send_message']) {
    assert.equal(shouldNotice(fixture({ reply_text: body })), true);
  }
  assert.equal(shouldNotice(fixture({}, { execution_status: undefined })), true);
  assert.equal(shouldNotice(fixture({ verifier_outcome: undefined })), false);
  assert.equal(shouldNotice(fixture({ tool_trace: undefined })), false);
  assert.equal(shouldNotice(fixture({ tool_trace: [{}] })), false);
  for (const key of Object.keys(fixture().turn.verifier_outcome)) {
    const verifier = { ...fixture().turn.verifier_outcome };
    delete verifier[key];
    assert.equal(shouldNotice(fixture({ verifier_outcome: verifier })), false, key);
  }
  for (const override of [{ model_done: false }, { model_done: 'true' }, { hard_status: 'passed' }, { hard_status: 'failed' },
    { trusted: true }, { trusted: 'false' }, { has_hard_evidence: true }, { has_validation_attempt: true }]) {
    assert.equal(shouldNotice(fixture({ verifier_outcome: { ...fixture().turn.verifier_outcome, ...override } })), false);
  }
});

const excluded = [
  ...['chat', 'research', 'plan', 'goal', 'discussion', undefined].map(work_mode => [String(work_mode) + ' mode', fixture({ context_snapshot: { run_settings: { work_mode } } })]),
  ['missing context', fixture({ context_snapshot: undefined })],
  ['misplaced mode', fixture({ context_snapshot: { work_mode: 'execute' } })],
  ['loading', fixture({}, { loading: true })],
  ['no final reply', fixture({ reply_text: '' })],
  ['valid delivery', fixture({ delivery: delivery(true) })],
  ['existing delivery warning', fixture({ delivery: delivery(false) })],
  ['tool trace', fixture({ tool_trace: [{ action: 'data_api', ok: true }] })],
  ['failed tool trace', fixture({ tool_trace: [{ action: 'data_api', ok: false }] })],
  ['action', fixture({ actions: [{ action: 'execute_code' }] })],
  ['decision action', fixture({ decision: { action: 'execute_code' } })],
  ['decision actions', fixture({ decision: { actions: [{ action: 'execute_code' }] } })],
  ...['tool_use', 'tool_result'].flatMap(kind => [
    ['wrapped ' + kind, fixture({ blocks: [{ block: { kind, action: 'data_api' } }] })],
    ['flat ' + kind, fixture({ blocks: [{ kind, action: 'data_api' }] })],
    ['unnamed ' + kind, fixture({ blocks: [{ block: { kind } }] })],
  ]),
  ...['tool.start', 'tool.complete', 'tool.progress', 'tool.output'].flatMap(kind => [
    ['live ' + kind, fixture({}, { live_events: [{ seq: 1, kind, action: 'data_api' }] })],
    ['persisted ' + kind, fixture({ activity_events: [{ seq: 1, kind, action: 'data_api' }] })],
    ['unnamed ' + kind, fixture({}, { live_events: [{ seq: 1, kind }] })],
  ]),
];
for (const [name, msg] of excluded) {
  test(name + ': no tool-less notice in helper or rendered component', () => {
    assert.equal(shouldNotice(msg), false);
    for (const locale of ['en', 'zh']) assert.doesNotMatch(render(msg, locale), /data-testid="turn-execution-notice"/);
  });
}

test('send_message records only communicate text and do not count as execution', () => {
  const msg = fixture({
    tool_trace: [{ action: 'send_message' }], decision: { action: 'send_message', actions: [{ action: 'send_message' }] },
    blocks: [{ block: { kind: 'tool_use', action: 'send_message' } }, { kind: 'tool_result', action: 'send_message' }],
    activity_events: [{ seq: 1, kind: 'tool.complete', action: 'send_message' }],
  }, { live_events: [{ seq: 2, kind: 'tool.start', action: 'send_message' }] });
  assert.equal(shouldNotice(msg), true);
  assert.doesNotMatch(render(msg, 'zh'), /data-testid="turn-execution-notice"/);
});

test('existing delivery warning retains precedence and original reply', () => {
  for (const locale of ['en', 'zh']) {
    const html = render(fixture({ delivery: delivery(false) }), locale);
    assert.match(html, /data-testid="task-delivery-warning"/);
    assert.doesNotMatch(html, /data-testid="turn-execution-notice"/);
    assert.ok(html.includes('>' + claim + '</p>'));
  }
});
