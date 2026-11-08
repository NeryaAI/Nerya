// Offline regression: real shared reply renderer and projection, no runtime or browser I/O.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
const { projectMessageBlocks } = require('../lib/streamTranscript.ts');
const { executionSteps } = require('../lib/executionTimeline.ts');

const localeMessages = require('../messages/index.ts');
const commentary = 'Checking the next tool result.';
const live = { role:'assistant', id:'acceptance-streaming', ts:1, loading:true, execution_status:'running',
  live_events:[{ kind:'message.delta', seq:1, text:commentary, completed:true, stream_id:'tool-step-1' }] };
function render(message, locale) {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, {
    locale, messages:localeMessages[locale], timeZone:'UTC',
  }, React.createElement(AssistantBubble, { msg:message })));
}
for (const locale of ['zh', 'en']) {
  const finalLabel = localeMessages[locale].copy.components_chat_ChatMessage['008'];
  test(locale + ': completed commentary stream stays visible without a final-result label while command runs', () => {
    const output = render(live, locale);
    assert.ok(output.includes(commentary), 'user-facing commentary must remain visible');
    assert.ok(output.includes('aria-busy="false"'));
    assert.equal(output.includes('aria-label="' + finalLabel + '"'), false,
      'running commentary must not be labelled as a final result');
    assert.ok(output.includes('aria-label="' + localeMessages[locale].copy.components_chat_ChatMessage['026'] + '"'));
    assert.equal(output.includes(localeMessages[locale].copy.components_chat_ChatMessage['025']), false);
    assert.equal(output.includes('data-testid="result-actions"'), false);
  });
  test(locale + ': tool wait retains commentary quietly and new message deltas resume the reply indicator', () => {
    const generating = localeMessages[locale].copy.components_chat_ChatMessage['025'];
    const call = { kind:'tool.start', seq:2, call_id:'waiting-task', skill_id:'native',
      action:'task_output', payload:{task_id:'background-thread', wait_for_completion:true} };
    for (const completed of [true, false]) {
      const waiting = { ...live, live_events:[{ ...live.live_events[0], completed }, call] };
      const output = render(waiting, locale);
      const section = output.match(/<section[^>]*data-turn-section="reply"[\s\S]*?<\/section>/)?.[0];
      assert.ok(section?.includes(commentary), 'tool wait must preserve the existing commentary');
      assert.ok(section.includes('aria-busy="false"'));
      assert.equal(output.includes(generating), false, 'tool execution must not claim body generation');
      assert.doesNotMatch(section, /role="status"/, 'tool status already belongs to the execution trace');
      assert.equal((output.match(/data-turn-section="trace"/g) || []).length, 1);
      const resumed = render({ ...waiting, live_events:[...waiting.live_events,
        { kind:'tool.complete', seq:3, call_id:call.call_id, action:call.action, ok:true, result:{} },
        { kind:'message.delta', seq:4, stream_id:'resumed-reply', text:'The result is ready.', completed:false },
      ] }, locale);
      assert.ok(resumed.includes('The result is ready.'));
      assert.ok(resumed.includes('aria-busy="true"'));
      assert.ok(resumed.includes('aria-label="' + generating + '"'));
      assert.ok(resumed.includes('>' + generating + '</p>'));
    }
  });
  test(locale + ': stopped commentary without a final reply keeps the retained-fragment label', () => {
    const output = render({ ...live, loading:false, execution_status:'cancelled' }, locale);
    assert.ok(output.includes(commentary));
    assert.ok(output.includes('aria-label="' + localeMessages[locale].copy.components_chat_ChatMessage['026'] + '"'));
    assert.equal(output.includes('aria-label="' + finalLabel + '"'), false);
    assert.equal(output.includes('data-testid="result-actions"'), false);
  });
  test(locale + ': a terminal explicit reply keeps the final-result label and actions', () => {
    const output = render({ ...live, loading:false, execution_status:'succeeded',
      turn:{ reply_text:'Completed answer.' } }, locale);
    assert.ok(output.includes('Completed answer.'));
    assert.ok(output.includes('aria-label="' + finalLabel + '"'));
    assert.ok(output.includes('data-testid="result-actions"'));
  });
}
test('24 completed calls retain their distinct histories across later commentary and parameter previews', () => {
  const events = [];
  const push = event => events.push({ session_id:'fixture-session', turn_id:'fixture-turn',
    epoch:'fixture-epoch', seq:events.length + 1, ...event });
  for (let i = 0; i < 24; i++) {
    const call_id = 'fixture-call-' + i;
    push({ kind:'model.tool_input_delta', call_id, name:'read_file', stream_id:'step-' + i, partial_json:'{}' });
    push({ kind:'tool.start', call_id, action:'read_file', payload:{path:'fixture-' + i} });
    push({ kind:'tool.complete', call_id, action:'read_file', ok:true, result:{text:'receipt-' + i} });
    push({ kind:'message.delta', stream_id:'commentary-' + i, text:commentary, completed:true });
  }
  const steps = executionSteps(projectMessageBlocks({ ...live, live_events:events }));
  assert.equal(steps.length, 24);
  assert.equal(new Set(steps.map(step => step.data.call_id)).size, 24);
  for (let i = 0; i < 24; i++) assert.equal(steps[i].data.result.text, 'receipt-' + i);
});
