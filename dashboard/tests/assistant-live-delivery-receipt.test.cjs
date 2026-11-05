// Offline SSR fixtures; these are not live CEX acceptance evidence.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = {}; };
const { AssistantReplyBubble } = require('../components/chat/AssistantReplyBubble.tsx');

const limitations = ['Paper execution only.', 'Limited historical coverage.', 'No live account evidence.', 'Draft awaits approval.'];
const prose = 'CEX findings.\n\n' + limitations.join('\n\n');
const task = {task_id: 'awork_cex', task_session_id: 'taskchat_cex', title: 'CEX',
  kind: 'background', command_id: 'atmsg_current', state: 'succeeded', task_complete: false,
  delivery_completion: {allowed: true, status: 'partial'}, unresolved_deliveries: [], limitations};
const stale = {...task, delivery_completion: {allowed: false, status: 'partial'}};
function result(command = task.command_id) {
  return {id: 'assistant_result:' + command, role: 'assistant', ts: 123,
    assistant_task: {...stale}, turn: {reply_text: prose}};
}
const binding = {command_id: 'parent_followup', session_id: 'parent', turn_id: 'turn',
  actor_id: 'local', input_digest: 'digest'};
function admission(operation = 'task_follow_up', tool = false) {
  const receipt = {...stale, ok: true, verified: true, source: 'assistant_work+agent_commands',
    protocol: operation === 'task_create' ? 'nerya.assistant_task.v2' : 'nerya.assistant_receipt.v1',
    publication_protocol: 'nerya.assistant_request.v1', operation, admission_confirmed: true,
    parent_session_id: 'parent', binding, url: '/chat/taskchat_cex'};
  return {id: 'parent-reply', role: 'assistant', ts: 123, command_id: binding.command_id,
    turn: {reply_text: prose, command_id: binding.command_id, session_id: 'parent', turn_id: 'turn',
      ...(tool ? {blocks: [{block: {kind: 'tool_result', ok: true,
        action: operation === 'task_create' ? 'assistant_task_create' : 'assistant_task_message', result: receipt}}]}
        : {assistant_receipt: receipt, publication: {source: 'host', receipt_verified: true}})}};
}
function render(message, tasks = [task], locale = 'en') {
  const before = JSON.stringify({message, tasks});
  const html = renderToStaticMarkup(React.createElement(NextIntlClientProvider, {locale, timeZone: 'UTC',
    messages: require('../messages/' + locale + '/assistants.json'),
  }, React.createElement(AssistantReplyBubble, {message, tasks})));
  assert.equal(JSON.stringify({message, tasks}), before, 'SSR must preserve history and live data');
  return html;
}
function status(html, expected) {
  assert.ok(html.includes('data-delivery-state="' + expected + '"'), html);
  assert.equal((html.match(/data-testid="assistant-task-receipt"/g) || []).length, 1);
}

test('current result uses exact live allowed partial, retaining all authored limitations', () => {
  for (const locale of ['en', 'zh']) {
    const html = render(result(), [task], locale);
    status(html, 'partial');
    assert.doesNotMatch(html, /assistant-delivery-warning|data-delivery-state="complete"/);
    for (const text of limitations) assert.ok(html.includes(text));
    assert.ok(html.includes(require('../messages/' + locale + '/assistants.json').assistants.deliveryStates.partial));
  }
});

test('hydrated backend identity and explicit current receipt use the same live projection', () => {
  const value = result();
  value.backend_message_id = value.id; value.id = 'local-id';
  status(render(value), 'partial');
  status(render({...result(), id: 'explicit', command_id: task.command_id}), 'partial');
});

test('host admissions and successful tool publications bind the child command, not the parent', () => {
  for (const operation of ['task_create', 'task_follow_up']) for (const tool of [false, true]) {
    const html = render(admission(operation, tool));
    status(html, 'partial');
    assert.doesNotMatch(html, /assistant-delivery-warning/);
  }
});

test('newer follow-up cannot bless a historical command on the same task', () => {
  for (const message of [result('atmsg_earlier'), admission()]) {
    if (message.turn.assistant_receipt) message.turn.assistant_receipt.command_id = 'atmsg_earlier';
    const html = render(message);
    status(html, 'incomplete');
    assert.match(html, /assistant-delivery-warning/);
  }
});

test('streaming host tool receipt has the same exact binding as its saved version', () => {
  const value = admission('task_follow_up', true);
  const receipt = value.turn.blocks[0].block.result;
  value.turn.blocks = [];
  value.loading = true;
  value.live_events = [{seq: 1, kind: 'tool.complete', action: 'assistant_task_message', ok: true,
    display_result: {content: [{type: 'json', data: receipt}]}}];
  status(render(value), 'partial');
});

test('a valid older tool receipt cannot lend authority to forged current metadata', () => {
  const value = admission('task_follow_up', true);
  value.turn.assistant_receipt = {...value.turn.blocks[0].block.result, verified: false};
  value.turn.blocks[0].block.result = {...value.turn.blocks[0].block.result, command_id: 'atmsg_earlier'};
  status(render(value), 'incomplete');
});

test('missing or foreign task, session and command identities cannot supply delivery authority', () => {
  for (const override of [{task_id: 'awork_other'}, {task_session_id: 'taskchat_other'},
    {task_session_id: undefined}, {command_id: 'atmsg_other'}, {command_id: undefined}]) {
    status(render(result(), [{...task, ...override}]), 'incomplete');
    status(render(admission(), [{...task, ...override}]), 'incomplete');
  }
  status(render({...result(), id: 'ordinary'}), 'incomplete');
  status(render({...result(), command_id: 'atmsg_other'}), 'incomplete');
});

test('model delivery fields and claimed host receipts cannot override persisted delivery', () => {
  const value = result();
  Object.assign(value.turn, {task_complete: true, delivery_completion: {allowed: true, status: 'complete'},
    delivery: {command_id: task.command_id, completion: {allowed: true, status: 'complete'}}});
  status(render(value, []), 'incomplete');
  for (const change of [{source: 'model'}, {verified: false}, {binding: {...binding, command_id: 'wrong'}}]) {
    const msg = admission();
    Object.assign(msg.turn.assistant_receipt, change);
    const html = render(msg);
    assert.doesNotMatch(html, /assistant-task-receipt|data-delivery-state/);
    assert.ok(html.includes('CEX findings.'));
  }
});

test('current unresolved evidence replaces stale blockers and preserves warning details', () => {
  const unresolved = [{command_id: 'atmsg_earlier', delivery_id: 'delivery_earlier', delivery_revision: 3,
    reasons: ['check_pending:coverage']}];
  const html = render(result(), [{...task, unresolved_deliveries: unresolved}]);
  status(html, 'incomplete');
  assert.match(html, /assistant-delivery-warning/);
  assert.ok(html.includes('check_pending:coverage'));
  const historical = result('atmsg_earlier');
  historical.assistant_task.unresolved_deliveries = unresolved;
  assert.match(render(historical), /check_pending:coverage/);
  const current = result(); current.assistant_task.unresolved_deliveries = unresolved;
  const refreshed = render(current);
  status(refreshed, 'partial');
  assert.doesNotMatch(refreshed, /assistant-delivery-warning|check_pending:coverage/);
});

test('pending, recovery, approval and failed states never become allowed partial', () => {
  for (const state of ['queued', 'running', 'stopping', 'unconfirmed', 'awaiting_approval', 'blocked', 'failed']) {
    const html = render(result(), [{...task, state}]);
    status(html, 'incomplete');
    assert.match(html, /assistant-delivery-warning/);
    assert.doesNotMatch(html, /data-delivery-state="partial"|data-delivery-state="complete"/);
  }
  const allowed = result(); allowed.assistant_task.delivery_completion = task.delivery_completion;
  status(render(allowed, [{...task, state: 'unconfirmed', delivery_completion: undefined}]), 'incomplete');
});

test('missing or denied evidence stays unconfirmed or incomplete; full completion is separate', () => {
  for (const [projection, expected] of [
    [{delivery_completion: {allowed: false, status: 'partial'}}, 'incomplete'],
    [{delivery_completion: {allowed: false, status: 'unavailable'}}, 'unconfirmed'],
    [{task_complete: true, delivery_completion: {allowed: true, status: 'complete'}}, 'complete'],
  ]) status(render(result(), [{...task, ...projection}]), expected);
  status(render(result(), [{...task, task_complete: undefined, delivery_completion: undefined, unresolved_deliveries: undefined}]), 'incomplete');
  status(render(result(), []), 'incomplete');
});
